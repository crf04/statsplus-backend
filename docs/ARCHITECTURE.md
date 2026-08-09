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
| NBA Stats | `NBAStatsAdapter` (via `nba_api`) | The closed `NBA_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `health_probe`, `player_game_logs`, `player_game_logs_recorded`, `player_roster`, `player_roster_recorded`, `league_opponent_team_stats`, `league_opponent_shot_chart`, `league_opponent_shooting_zone`, `synergy_team_play_types`, `synergy_player_play_types`, `player_per36_stats`, `player_shooting_zone`, `player_shot_chart`, `player_gamelogs_against`, `schedule_whole_season` |
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

`ProviderSnapshotCache` is an injected decorator around that seam. It stores
only complete normalized snapshots in Redis under a provider/query key that
includes the adapter-contract version; it never serializes a `DFSBoard`. The
cache sits below Statistic Catalog resolution, so a cached market carries
provider evidence only: a resolved market is refused on the way in, and a wire
value with a `statistic_match` is rejected as corrupt rather than decoded.
Fresh hits retain the snapshot's `retrieved_at` and expose bounded age metadata.
Partial refreshes are returned to the current caller but never written over a
complete value. A complete value past its fresh window is used only as a
stale-if-error fallback after a later expected total refresh failure. Redis
failure bypasses the cache without an in-process stale copy, and
`ProviderSnapshotCacheCoordinator` suppresses duplicate refreshes only within
one worker (there is no distributed lock). One flight shares the whole cache
decision: a follower adopts the owner's result or failure verbatim, including
its cache status, age, and sanitized refresh-failure provenance, and never
substitutes a stale value from its own Redis read. When the owner's deadline
elapses before an uncancellable refresh finishes, that deadline failure becomes
the flight's decision immediately: the flight stays active, and every follower
receives the owner's failure verbatim however much later its own deadline is.
The late result then only drains and validates the abandoned work and retires
the key — it publishes nothing, and never turns the shared failure into a
success carrying cache provenance the abandoned request never had. A follower's
own deadline still applies to itself alone; it never abandons the refresh.

Publication is decided at a single instant, before the write: work that
finished at or after the deadline is never written, and a value written before
that instant is never retracted, so no concurrent reader can observe a value
that a later cleanup would remove again. A write that itself outlives the
budget still stands, but its caller is not served past its own deadline. The
absolute deadline is also enforced after the Redis read — including after a
read that fails slowly, which calls no provider at all — and before any value
is returned. Only an unusable payload is cleaned up, comparing and deleting
atomically so a newer concurrent value survives.

A cached payload is used only when its bytes are already exactly the canonical
document this codec writes, compared against the stored text rather than a
normalizing re-dump, so surrounding whitespace, reordered keys, and alternate
escapes are all rejected. A duplicate key at any nesting level is rejected
before decoding, because JSON's last-value-wins would otherwise let one payload
carry a second, conflicting document. Anything a constructor would normalize,
drop, canonicalize from an alias, or deduplicate is corrupt too, as is a value
no domain constructor can represent — a wire number whose conversion raises
`OverflowError`, for example. Every such failure is contained at this seam: the
key is deleted and the request becomes a miss.

Cache decisions are recorded once per request as bounded cache counters only;
the cache never emits a provider-operation event, and provider events never
increment the cache counters. A retrieval that makes many upstream calls (the
Dabble fixture-detail fan-out) therefore records many provider events and still
exactly one cache decision.

`DFSBoardService.get_board(query, context)` is the internal collector seam. Its
provider registry is injected explicitly; it never discovers or constructs
providers while collecting. Enabled providers run concurrently behind one
absolute `RetrievalContext` deadline (15 seconds by default), with at most
three concurrent provider workers. The child context is capped at the lesser
of the caller deadline and the configured deadline, never above 15 seconds. A
complete empty snapshot is usable, while
partial snapshots remain one coherent provider observation with their original
`CoverageEvidence`; failed providers cannot remove usable snapshots from other
providers. Expected upstream failures become sanitized `ProviderOutcome`
reason codes (`timeout`, `deadline_exceeded`, `rate_limited`,
`access_denied`, `upstream_error`, or `malformed_response`), while unexpected
implementation defects propagate. Results and disabled-provider metadata are
sorted deterministically. The collector does not resolve athlete/event
identity or build comparison groups. Before returning a board it does apply
the injected immutable Statistic Catalog to each market, preserving the
provider `StatisticEvidence` and attaching a typed canonical or unmapped
`MatchState`.

### Statistic Catalog

`app/config/statistic_catalog.yaml` is the version-controlled source of truth
for the reviewed cross-provider statistic identities. The
`statistic_catalog_schema` loader owns YAML decoding and the implemented schema
version; the shared `app.domain.statistics` module owns the closed
`ScoringPeriod`, `StatisticUnit`, `MatchState`, and `MatchReason` vocabularies
plus the immutable typed `StatisticMatch`, while the catalog service owns
resolution. Schema v1's field vocabulary is exact and closed: a document has
`schema_version` (required, exactly `1`), optional `component_order`, and
`statistics`; each statistic has `id`, `label`, `unit`, `scoring_periods`,
`components`, and optional `provider_mappings` and `comparable`. There are no
aliases — `version`, `canonical_statistics`, `canonical_id`, `display_name`,
`name`, `period`, `periods`, `ordered_components`, `mappings`,
`provider_labels`, `comparison_allowed`, and mapping `alias`/`aliases` are all
rejected, so two spellings of one reviewed fact can never disagree. The
document's values are read exactly rather than normalized: identifiers, units,
scoring periods, and provider names must already be the canonical value, labels
must carry no surrounding whitespace, and list fields must be lists (a scalar
`scoring_periods: full_game` or `provider_mappings: {dabble: points}` is a
defect, not a shorthand). Directly constructed `StatisticCatalog` values are
held to the same version.
`StatisticCatalog.load`
validates the document before constructing immutable `CanonicalStatistic`,
`StatisticMapping`, and `StatisticMatch` objects. Application dependency
assembly loads it before provider construction, so malformed schema, duplicate
canonical identities, duplicate/conflicting provider labels, invalid periods or
units, and inconsistent ordered components fail startup clearly. Route imports
never load or mutate the catalog. A market with absent statistic evidence is
given an immutable `UNMAPPED` match with reason `missing_statistic_evidence`
during snapshot resolution, so every market on `board.snapshots` carries an
explicit match; the provider's coverage evidence and observation remain
unchanged.

The initial catalog maps full-game points, rebounds, assists, three-pointers
made, steals, blocks, turnovers, PRA, PA, PR, and RA. Composite identities use
the reviewed component order (`PRA` is points, rebounds, assists) regardless of
the source label order. Dabble, PrizePicks, and Underdog labels are accepted
only when explicitly listed in the definition; provider evidence is matched
case-insensitively and preserves presentation evidence but does not infer
meaning. Omitted period evidence
stays `ScoringPeriod.UNKNOWN` rather than being guessed as full game, so a
canonical identity always requires explicit full-game evidence. Unknown labels,
unknown or period-specific scoring periods, unit mismatches, and provider
fantasy labels return `MatchState.UNMAPPED` with a closed `MatchReason`, retain
the original provider evidence, and are not included in the board's
canonical-market view. This slice does not create
comparison groups or a public route.

DFS provider requests use connection/read caps of 3/8 seconds (or the
remaining absolute budget), and safe GET transport retries at most once for a
timeout or one of HTTP 429, 500, 502, 503, or 504. Access-denied, ordinary 4xx, and
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

The internal board collector uses a separate typed, bounded aggregate event
collection. Its scalar schema records started-at, optional request correlation,
latency, bounded outcome/reason counters, and coverage counts. Board events do
not enter `recent_provider_events`, `provider_events_total`, or provider
failure counters.

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

`NBAStatsAdapter.fetch_whole_season_schedule(season=...)` is the provider seam
for the canonical event catalog. It accepts only an explicit canonical
`YYYY-YY` season, calls `ScheduleLeagueV2` through the shared NBA Stats
concurrency/timeout bound, and emits the closed `schedule_whole_season`
telemetry operation. Its normalized frame retains the NBA game ID, explicit
home/away team IDs and identities, UTC scheduled time, status text,
postponement evidence, and provider classification. Recorded fixtures use
`parse_recorded_schedule` and never make a network request.

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

### Provider athlete mappings

`AthleteResolver.resolve(provider, evidence, season)` accepts one typed
provider `AthleteEvidence` value and an explicit requested season;
`resolve_market(market, season)` is the board-facing spelling that lifts a
market's athlete and team evidence. There is no other call shape. Resolution
compares only the accent/case/punctuation-normalized official name among
active `is_active_for_season` catalog rows. Normalization strips combining
marks and folds the documented non-decomposing Latin letters (`ø`→`o`,
`œ`→`oe`, `æ`→`ae`, `ł`→`l`, `đ`→`d`, `ð`→`d`, `þ`→`th`, `ħ`→`h`, `ŧ`→`t`,
`ı`→`i`) to ASCII; it applies no aliases, nicknames, or fuzzy similarity.
Exactly one candidate with non-conflicting canonical team evidence is
automatically qualifying; missing team evidence is allowed. Duplicate names,
inactive-only rows, aliases, fuzzy matches, and team conflicts remain typed
non-matches. `AthleteMappingRepository` persists one current
`provider_athlete_mappings` row per provider identity, an append-only
`athlete_mapping_decisions` audit log, and durable
`athlete_mapping_rejections` suppressions. Provider names and team IDs,
names, and abbreviations are retained as typed evidence.

Ambiguous, inactive-only, unmatched, and team-conflict evidence never becomes
current mapping state, but each is retained as one durable typed observation
in the decision log under the same idempotency key, so repeated board reads
add nothing and `scripts/athlete_mappings.py list` can show what an operator
still has to decide. Repository reads and writes translate `SQLAlchemyError`
to `AthleteMappingPersistenceError` (defined in
`app.services.athlete_mapping_errors`), and the resolver translates the same
failure from a catalog read. That single type is what the DFS Board isolates;
it never catches broad exceptions and still returns usable markets.

An injected DFS board read may transactionally record the first qualifying
automatic decision. The repository is idempotent under repeated and concurrent
reads, never replaces manual approvals or overrides, and isolates persistence
failures from the normalized market result. Later evidence that disagrees with
an active automatic mapping deactivates it as `mapping_conflict` while keeping
the conflicting evidence in the current row and audit history. Operator
approve/override/reject/clear actions require an identity and reason; approve
and override also accept and retain provider name and team evidence, keeping
the previously observed evidence when none is supplied. Active rejections
suppress future automatic mappings until explicitly cleared.
Migration 006 also creates a per-identity lock table and database checks for
closed mapping states, the closed decision-state set, active-state coherence,
and cleared-rejection coherence. Those checks compare booleans with
`true`/`false` rather than `1`/`0`, so they are valid on PostgreSQL as well as
SQLite. The operator CLI never runs migrations implicitly; run
`scripts/migrate.py` explicitly first.

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

Migration 005 creates the writable `event_catalog` and
`event_catalog_refreshes` tables. Migrations are applied in order. Event
refreshes upsert by NBA game ID in one transaction without replacing the
table; omitted historical rows remain available and replacement IDs remain
distinct. Event/competition mapping state belongs to #28; provider-athlete
mapping state is owned by migration 006 below. Event freshness is
independent from Athlete Catalog freshness and defaults to 72 hours through
`EVENT_CATALOG_MAX_AGE_HOURS`. Operators use
`scripts/refresh_event_catalog.py` with one or more explicit seasons; each
season is independent and the command exits nonzero if any season fails.

Migration 006 creates the provider athlete mapping, append-only decision, and
durable rejection tables. Operators use
`scripts/athlete_mappings.py` for
read-only listing, dry runs, audited approve/override/reject/clear actions,
and history. `list` reports current mappings, active rejections, and the
latest unresolved observation per provider identity. These commands require an
explicit writable database URL and never contact a provider.

The tracked `nba_play_types.db` file is a public read-only fixture. Run
`scripts/validate_demo_db.py` to check its required tables and columns without
opening it for writes. Migration tests must use a temporary database, and the
validator must not be used to repair the fixture.

## Test seams

- App and route behavior: use the `app` and `client` fixtures in `tests/conftest.py`.
- Route/service interaction: replace methods on the dependency graph supplied
  through the `DEPENDENCIES` app-factory override.
- Provider failures: raise the relevant `requests` timeout/error from a patched service or endpoint constructor.
- Provider response contracts: run the recorded fixtures in `tests/fixtures/nba_stats` and `tests/fixtures/pbp_stats` through the production parse seams (`parse_recorded_game_logs`, `parse_recorded_player_roster`, `parse_recorded_schedule`, `PBPTotalsAdapter.parse_totals`) with no network.
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
