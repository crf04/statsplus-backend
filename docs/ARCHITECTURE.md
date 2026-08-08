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

Route modules expose lazy service handles rather than constructing services at
import time. A handle resolves its service from the active app's
`app.extensions["request_services"]` registry, passing that app's canonical
`RuntimeSettings` object into the constructor. Tests can patch the handle
exposed by the route module—for example,
`app.routes.game_routes.game_service`—or instantiate a service directly with a
temporary or mocked engine.

## Data-source seams

The app reads from three distinct sources:

| Source | Access path | Expected behavior |
| --- | --- | --- |
| Bundled SQLite demo data | `app.utils.db.get_engine()` | Default, offline-capable read path |
| NBA Stats | `nba_api` → `stats.nba.com` | Live game logs and selected team/player data; bounded by `NBA_STATS_TIMEOUT_SECONDS` |
| PBP Stats | shared `requests.Session` → `api.pbpstats.com` | Play-by-play aggregates and the PBP-specific health probe |

Redis is an optional cache. Connection failure disables caching without blocking startup. OpenAI is an optional fallback for low-confidence natural-language parsing. Firebase is optional for local development but should be configured in production.

The default database URL is `sqlite:///nba_play_types.db`, relative to the current working directory. Run commands from the repository root or set an absolute `DATABASE_URL`.

Runtime configuration is loaded and validated once by
`app.config.settings.load_settings()`. The resulting typed `RuntimeSettings`
object is attached to the app and passed into request services; see
[SETTINGS.md](SETTINGS.md) for the field and environment-variable contract.

## Provider telemetry and correlation IDs

Every request is correlated with one safe ID. `app.utils.request_id` accepts an
inbound `X-Request-ID` only when it matches `^[A-Za-z0-9._:-]{1,128}$` and
otherwise generates a fresh UUID; the app binds it to `flask.g.request_id` in a
`before_request` and echoes it on the `X-Request-ID` response header. The same
ID flows into provider telemetry events, so a log, a provider event, and a
response header share one correlation key.

Provider calls at the two external seams are wrapped in one structured event
(`app.utils.telemetry.ProviderEvent`):

| Provider | Seam | Operations |
| --- | --- | --- |
| NBA Stats | `NBAStatsAdapter` (via `nba_api`) | The closed `NBA_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `health_probe`, `player_game_logs`, `player_game_logs_recorded`, `league_opponent_team_stats`, `league_opponent_shot_chart`, `league_opponent_shooting_zone`, `synergy_team_play_types`, `synergy_player_play_types`, `player_per36_stats`, `player_shooting_zone`, `player_shot_chart`, `player_gamelogs_against` |
| PBP Stats | `PBPTotalsAdapter` (shared retrying session) | The closed `PBP_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `get_totals_player`, `get_totals_opponent`, `health_probe` |

An event records provider, operation, outcome (success/timeout/http_error/
malformed/error), duration, retry count (thread-safe counter incremented by
`RetryWithLogging`), cache status (hit/miss/disabled), HTTP status, and the
request ID. Events are written as one structured log line and retained in a
bounded, thread-safe buffer (capacity 5000); credentials, authorization
headers, URLs, raw bodies, and exception messages are never captured.

Provider failures are counted at the provider seam. The central error handler
in `app.errors` counts only *application* failures (actual `AppError` codes and
HTTP >= 500 responses) and skips `provider_unavailable` so the same provider
error is never double counted. Operators read the bounded counters and recent
events from the admin-only `GET /api/data/telemetry` endpoint.

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

The `DataService` refresh callables first collect every provider frame in
memory (failures stop the job before any write), then hand the whole related
set to `AtomicTablePublisher`, which stages each `DataFrame` under a unique
name and swaps every name inside one `engine.begin()` transaction. Readers
never observe a mixed old/new set, and a failed swap rolls back to the
previous tables.

## Schema maintenance

Application-owned tables are versioned by `app.migrations.run_migrations` and
the `scripts/migrate.py` command. A fresh or existing application database can
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
- Route/service interaction: patch the module-level service on the route module.
- Provider failures: raise the relevant `requests` timeout/error from a patched service or endpoint constructor.
- Provider response contracts: run the recorded fixtures in `tests/fixtures/nba_stats` and `tests/fixtures/pbp_stats` through the production parse seams (`parse_recorded_game_logs`, `PBPTotalsAdapter.parse_totals`) with no network.
- `PBPTotalsAdapter.parse_totals` validates the operation-specific columns consumed by the PBP publication/assist transforms. A nonempty row set missing a required column is a malformed provider response; an empty result is materialized with that declared schema so refresh publication cannot replace a valid table with a schema-less frame.
- Live provider contracts: `tests/live/test_provider_contracts.py` hits the real providers and is excluded from the default gate by the registered `live` marker (`addopts = -m "not live"`). Opt in with `LIVE_CONTRACT_TESTS=true` plus `-m live`.
- Parser behavior: use the bundled SQLite data and patch static NBA lookups when the parser needs a deterministic team list.
- LLM behavior: inject or mock the OpenAI client; the default suite must not require an API key.

The authoritative local and CI gate is `./scripts/check.sh`.

## Known seams to improve incrementally

- The lazy request-service registry keeps app-factory isolation explicit while
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
