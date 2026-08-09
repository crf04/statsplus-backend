# Architecture

This document records the interfaces and seams that are expensive to rediscover from code. The endpoint catalog lives in [API_DOCUMENTATION.md](API_DOCUMENTATION.md), and natural-language parsing details live in [NLP_SYSTEM.md](NLP_SYSTEM.md).

## Runtime shape

`app.create_app()` constructs the Flask application, initializes optional dependencies, registers JSON error handlers, and attaches blueprints. `run.py` is the local entry point; Gunicorn imports `wsgi:app` in production.

Blueprints are intentionally thin HTTP adapters:

| Seam | Modules | Responsibility |
| --- | --- | --- |
| HTTP | `app/routes/` | Parse requests, apply auth, call services, shape status codes |
| Business logic | `app/services/` | Fetch, combine, filter, and serialize NBA data |
| Natural language | `app/services/nl_query/`, `nl_service.py`, `llm_service.py` | Deterministic parsing first, optional LLM fallback |
| Infrastructure | `app/utils/` | Database engine, Firebase, Redis, provider HTTP configuration |
| Persistence | `app/models/`, `app/migrations.py` | SQLAlchemy models, sessions, and application-schema migrations |

Application errors cross the HTTP boundary through `app.errors`. Routes and
services may raise `InvalidInputError`, `ResourceNotFoundError`,
`ProviderUnavailableError`, or `InvalidConfigurationError`; the app factory
registers handlers that return the documented `{ "error": { "code", "message"
} }` shape. Optional `detail` values are logged for operators and never sent
to clients.

`app.dependencies.build_dependencies()` is the single production assembly
point for the database engine, cache client, providers, services, durable job
coordinator, and provider-health service. The app factory constructs that graph
once and stores it in `app.extensions["dependencies"]`. Route modules contain
read-only handles that resolve objects from the active app; importing a route
never selects a database, connects to Redis, initializes Firebase, or loads a
parser. Tests can provide a complete replacement graph through the
`DEPENDENCIES` app-factory override without patching module globals.

## Data-source seams

The app reads from three distinct sources:

| Source | Access path | Expected behavior |
| --- | --- | --- |
| Bundled SQLite demo data | `app.utils.db.get_engine()` | Default, offline-capable read path |
| NBA Stats | `app.providers.nba_stats.NBAStatsAdapter` → `nba_api` → `stats.nba.com` | All live NBA calls use one injected, instrumented adapter with schema validation and a process-shared bound; bounded by `NBA_STATS_TIMEOUT_SECONDS` |
| PBP Stats | `app.providers.pbp_stats.PBPStatsAdapter` → shared `requests.Session` → `api.pbpstats.com` | Normalized play-by-play aggregates, refreshes, retries, telemetry, and the separate PBP health probe |

Redis is an optional cache. Connection failure disables caching without blocking startup. OpenAI is an optional fallback for low-confidence natural-language parsing. Firebase is optional for local development but should be configured in production.

The default database URL is `sqlite:///nba_play_types.db`, relative to the current working directory. Run commands from the repository root or set an absolute `DATABASE_URL`.

Runtime configuration is loaded and validated once by
`app.config.settings.load_settings()`. The resulting typed `RuntimeSettings`
object is attached to the app and passed into request services; see
[SETTINGS.md](SETTINGS.md) for the field and environment-variable contract.
The app factory configures Flask-CORS from `RuntimeSettings.cors`; it never
falls back to a wildcard origin. Local development uses the explicit
`http://localhost:3000` default, while production requires
`CORS_ALLOWED_ORIGINS`.

## Provider telemetry and correlation IDs

Every request is correlated with one safe ID. `app.utils.request_id` accepts an
inbound `X-Request-ID` only when it matches `^[A-Za-z0-9._:-]{1,128}$` and
otherwise generates a fresh UUID; the app binds it to `flask.g.request_id` in a
`before_request` and echoes it on the `X-Request-ID` response header. The same
ID flows into provider telemetry events, so a log, a provider event, and a
response header share one correlation key.

External provider invocations and explicit local provider-normalization seams
are wrapped in one structured event (`app.utils.telemetry.ProviderEvent`).
`provider_call` identifies upstream work; `provider_normalization_call` is an
adapter-owned normalization/empty-result decision.  The latter is emitted
only when the local retrieval outcome says no upstream malformed or timeout
event already represents the terminal failure, so one underlying defect is
not counted twice:

| Provider | Seam | Operations |
| --- | --- | --- |
| NBA Stats | `NBAStatsAdapter` (via `nba_api`) | The closed `NBA_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `health_probe`, `player_game_logs`, `player_game_logs_recorded`, `player_roster`, `player_roster_recorded`, `league_opponent_team_stats`, `league_opponent_shot_chart`, `league_opponent_shooting_zone`, `synergy_team_play_types`, `synergy_player_play_types`, `player_per36_stats`, `player_shooting_zone`, `player_shot_chart`, `player_gamelogs_against` |
| PBP Stats | `PBPTotalsAdapter` (shared retrying session) | The closed `PBP_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `get_totals_player`, `get_totals_opponent`, `health_probe` |
| Dabble | `DabbleAdapter` (shared DFS snapshot contract) | Competition discovery, fixture fan-out, and fixture details are upstream invocation events (`competition_lookup`, `competition_fixtures`, `fixture_details`); the bounded snapshot normalization/empty-result decision is an explicit local seam (`snapshot_normalization`). Production requests use a thread-local session factory; explicitly injected sessions serialize only their `get` call. The shared DFS transport owns one safe-GET retry. |
| PrizePicks | `PrizePicksAdapter` (shared DFS snapshot contract) | Projection pagination remains inside the adapter; the closed telemetry operation is `get_snapshot`. No retry strategy is configured. |
| Underdog | `UnderdogAdapter` (shared DFS snapshot contract) | Appearance, player, and game joins remain inside the adapter; the closed telemetry operation is `get_snapshot`. No retry strategy is configured. |

The DFS provider seam is `ProviderSnapshotProvider.get_snapshot(query,
context)`. Dabble, PrizePicks, and Underdog accept the same pregame NBA query,
eligible market statuses, and absolute retrieval deadline and return immutable,
market-centric provider snapshots. Canonical athlete, event, and statistic
filters remain central because provider IDs are only evidence until later
resolution. The shared model retains nullable provider identity and typed
source evidence, exact decimal thresholds and modifiers, original labels,
coverage, and complete/partial status. Adapters exclude ineligible offerings
without guessing missing facts; they expose no provider-specific public routes.

`DFSBoardService.get_board(query, context)` is the internal collector seam. Its
provider registry is injected explicitly; it never discovers or constructs
providers while collecting. Enabled providers run concurrently behind one
absolute `RetrievalContext` deadline (15 seconds by default), with at most
three concurrent provider workers. A complete empty snapshot is usable, while
partial snapshots remain one coherent provider observation with their original
`CoverageEvidence`; failed providers cannot remove usable snapshots from other
providers. Expected upstream failures become sanitized `ProviderOutcome`
reason codes (`timeout`, `deadline_exceeded`, `rate_limited`,
`access_denied`, `upstream_error`, or `malformed_response`), while unexpected
implementation defects propagate. Results and disabled-provider metadata are
sorted deterministically. The collector is intentionally not a route, cache,
identity resolver, comparison builder, or statistic catalog.

DFS provider requests use connection/read caps of 3/8 seconds (or the
remaining absolute budget), and safe GET transport retries at most once for a
timeout, 429, or retryable 5xx response. Access-denied, ordinary 4xx, and
malformed responses are not retried. Dabble fixture-detail fan-out remains
bounded at three workers. Late daemon work is ignored after the board deadline
and cannot alter the returned board.

An event records provider, operation, outcome (success/timeout/http_error/
malformed/error), duration, retry count (updated only when a configured
provider retry hook reports a retry), cache status (hit/miss/disabled), HTTP
status, and the request ID. Events are written as one structured log line and
retained in a bounded, thread-safe buffer (capacity 5000); credentials,
authorization headers, URLs, raw bodies, and exception messages are never
captured.

Provider failures are counted once at the seam that owns the failure. The
central error handler in `app.errors` counts only *application* failures
(actual `AppError` codes and HTTP >= 500 responses) and skips
`provider_unavailable`; adapters also avoid emitting a local normalization
failure when an upstream malformed or timeout event already represents the
same terminal retrieval outcome. Operators read the bounded counters and
recent events from the admin-only `GET /api/data/telemetry` endpoint.

## Request flows

Game logs:

```text
GET /api/games/game_logs
  → Firebase auth (or explicit local-only bypass)
  → GameService.get_filtered_logs
  → cached NBAStatsAdapter call (telemetry event per call)
  → local/database and request filters
  → serialized logs and averages
```

### NBA Stats game-log adapter

`app.providers.nba_stats.NBAStatsProvider` is the injectable interface for
live game logs:

```python
class NBAStatsProvider(Protocol):
    def get_player_game_logs(
        self, *, player_id: int, season: str,
        season_type: str = "Regular Season",
    ) -> pandas.DataFrame: ...

    def get_archetype_game_logs(
        self, *, player_ids: Sequence[int], opponent_team_id: int,
        season: str, season_type: str = "Regular Season",
    ) -> pandas.DataFrame: ...
```

The production adapter owns endpoint construction, timeout, concurrency,
telemetry, response normalization, and provider error translation. Tests inject
the protocol into `GameService` or `PlayerService` rather than patching
`nba_api`.

Natural-language query:

```text
POST /api/nl-query
  → deterministic BaseQueryParser
  → confidence decision
  → optional OpenAI fallback
  → frontend-compatible structured filters
```

Data refresh:

```text
POST /api/data/update_database (or a PBP/PUT refresh)
  → schedule durable DataRefreshJob (HTTP 202, job_id)
  → DataRefreshJobService runs the refresh in the background
  → AtomicTablePublisher stages frames → single transaction name swap
  → GET /api/data/jobs/<job_id> reports queued/running/succeeded/failed
```

Every mutating refresh is recorded in the application-owned
`data_refresh_jobs` table before the request returns `202 Accepted`.
`DataRefreshJobService` writes queued/running/succeeded/failed transitions and
a sanitized `failure_summary` (exception/provider text is never stored), and
its partial unique index enforces at most one queued or running job per
operation. Migration 003 adds the request ID plus an owner, expiry, heartbeat,
and attempt count. A process claims queued or expired rows with a conditional
SQL update, renews its lease while the registered handler runs, and clears the
lease on completion/failure. The app factory creates one coordinator per app
with the closed registry for `update_database`, both PBP refreshes,
`fetch_players_with_teams`, and `fetch_players`; operation names, not callbacks,
are persisted. A bounded dispatcher wakes immediately after enqueue and polls
for restart recovery. The service takes an injectable executor and clock;
tests use a `SynchronousExecutor` and `dispatch_once()` so job completion is
deterministic.

Execution is at-least-once: a process crash or an expired lease can cause a
refresh handler to run again, so handlers must tolerate repeated provider
work. Each claim increments `attempt_count`, which is also the fencing token.
Before a handler swaps its staged tables, `AtomicTablePublisher` renews the
claim through a conditional update on the *same* `engine.begin()` connection.
That update and all table swaps commit atomically; a stale attempt therefore
fails before changing a live table and cannot overwrite a newer attempt. This
protects publication and durable job state, but it cannot cancel provider work
that was already in flight before a lease expired.

The `DataService` refresh callables first collect every provider frame in
memory (failures stop the job before any write), then hand the whole related
set to `AtomicTablePublisher`, which stages each `DataFrame` under a unique
name and swaps every name inside one `engine.begin()` transaction. Readers
never observe a mixed old/new set, and a failed swap rolls back to the
previous tables.

### Canonical athlete catalog

`AthleteCatalogService` owns the application tables `athlete_catalog` and
`athlete_catalog_freshness`. An operator invokes
`scripts/refresh_athlete_catalog.py` with one or more explicit `YYYY-YY`
seasons; there is no wall-clock season default and no background timer. The
NBA Stats `player_roster` seam requests `CommonAllPlayers` for each season and
normalizes stable NBA player IDs, official display names, roster status,
season, and team identity. A `(season, player_id)` key lets a later official
display-name update replace that season's label without rewriting prior
history. Active, inactive, and historical rows remain distinguishable.

Provider collection happens before publication. Each requested season's
catalog rows and success metadata are written in one transaction, so an
interrupted publication rolls back to the prior catalog. Failure metadata is
recorded independently afterward; it never clears the last successful
catalog and is not coupled to a future Event Catalog. A multi-season refresh
returns and prints one sanitized success/failure result per season; the
operator command exits nonzero if any season fails while retaining successful
seasons. `get_catalog()` and
`get_freshness()` read the persisted rows, with freshness controlled by
`ATHLETE_CATALOG_FRESHNESS_DAYS` (default seven days). The tracked demo
database is rejected as a migration or catalog target.

PBP Stats:

```text
GET /api/health/pbp-stats or PUT /api/data/*_PBP
  → injected PBPStatsProvider
  → shared requests.Session with PBP-specific connect/read timeouts and retries
  → validate multi_row_table_data and normalize to a dataframe
  → ProviderUnavailableError on timeout, unavailable, or malformed responses
```

`DataService` and `ProviderHealthService` receive the same app-owned provider
instances from `ApplicationDependencies`; they do not create duplicate
providers or perform provider calls in route modules.

## Schema maintenance

Application-owned tables are versioned by `app.migrations.run_migrations` and
the `scripts/migrate.py` command. Migration 004 adds the canonical
`athlete_catalog` and `athlete_catalog_freshness` tables. A fresh or existing
application database can
be created or upgraded with an explicit `--database-url` argument or
`DATABASE_URL`; the CLI has no database-file fallback and fails if neither is
provided. Rerunning the command is idempotent because applied versions are
recorded in `schema_migrations`. Status output masks database passwords.

The tracked `nba_play_types.db` file is a public read-only fixture. Run
`scripts/validate_demo_db.py` to check its required tables and columns without
opening it for writes. Migration tests must use a temporary database, and the
validator must not be used to repair the fixture.

## Test seams

- App and route behavior: use the `app` and `client` fixtures in `tests/conftest.py`.
- Route/service interaction: replace methods on the dependency graph supplied
  through the `DEPENDENCIES` app-factory override.
- Provider failures: raise the relevant `requests` timeout/error from a patched service or endpoint constructor.
- Provider response contracts: run the recorded fixtures in `tests/fixtures/nba_stats` and `tests/fixtures/pbp_stats` through the production parse seams (`parse_recorded_game_logs`, `parse_recorded_player_roster`, `PBPTotalsAdapter.parse_totals`) with no network.
- DFS provider contracts: run each Dabble, PrizePicks, and Underdog adapter
  against its recorded fixtures through `get_snapshot`; the shared compliance
  suite verifies the same immutable `ProviderSnapshot` boundary for all three.
- `PBPTotalsAdapter.parse_totals` validates the operation-specific columns consumed by the PBP publication/assist transforms. A nonempty row set missing a required column is a malformed provider response; an empty result is materialized with that declared schema so refresh publication cannot replace a valid table with a schema-less frame.
- Live provider contracts: `tests/live/test_provider_contracts.py` hits the real providers and is excluded from the default gate by the registered `live` marker (`addopts = -m "not live"`). Opt in with `LIVE_CONTRACT_TESTS=true` plus `-m live`.
- Parser behavior: use the bundled SQLite data and patch static NBA lookups when the parser needs a deterministic team list.
- LLM behavior: inject or mock the OpenAI client; the default suite must not require an API key.

The authoritative local and CI gate is `./scripts/check.sh`.

## Known seams to improve incrementally

- The app-scoped dependency graph keeps app-factory isolation explicit while
  avoiding database, Redis, and parser initialization during route imports.
- The game-log request path is fully synchronous and bounded: the route
  parses query parameters into one typed `GameLogQuery`, and the service runs
  under Flask's threaded gunicorn model (`--workers 4 --threads 2`). NBA Stats
  provider calls go through `NBAStatsAdapter`, which applies a
  process-shared `threading.BoundedSemaphore` sized by
  `NBA_STATS_MAX_CONCURRENCY` (default 10) and shares the provider timeout from
  `NBA_STATS_TIMEOUT_SECONDS`.  All adapter instances using the same configured
  limit in one worker reuse that gate.
- The `NBA_STATS_MAX_CONCURRENCY` bound is per worker **process**, not global:
  all adapters in each worker share one gate, so worst-case provider
  concurrency is `workers × NBA_STATS_MAX_CONCURRENCY` (4 × 10 = 40 with the
  Procfile defaults). A true cross-process bound would need shared locking
  (e.g. Redis) and is intentionally out of scope; operators scale the bound and
  worker count together.
- Several services catch broad exceptions and return sentinel values, which can hide provider-specific failures.
- The bundled provider-generated tables are validated as a public fixture; they
  are not application migration targets.

Keep these constraints visible when changing nearby code. Improve them behind tests in small slices instead of combining them with unrelated feature work.
