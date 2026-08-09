# Runtime settings

`app.config.settings.load_settings()` is the authoritative configuration
interface. Application startup calls it once, validates the result, stores the
typed `RuntimeSettings` object in `app.extensions["runtime_settings"]`, and
passes that object to request services. Adapters outside a request can use
`get_runtime_settings()`; they should not read environment variables directly.

The model is intentionally grouped by responsibility:

| Model | Fields | Environment variables |
| --- | --- | --- |
| `DatabaseSettings` | `url` | `DATABASE_URL` |
| `AuthenticationSettings` | Firebase credential sources and `firebase_admin_disabled` | `FIREBASE_SERVICE_ACCOUNT_PATH`, `FIREBASE_SERVICE_ACCOUNT_JSON`, `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`, `FIREBASE_ADMIN_DISABLED` |
| `CacheSettings` | `enabled`, Redis URL/host/port/database/password/TLS | `ENABLE_CACHE`, `REDIS_URL`, `REDISHOST`/`REDIS_HOST`, `REDISPORT`/`REDIS_PORT`, `REDISDB`/`REDIS_DB`, `REDISPASSWORD`/`REDIS_PASSWORD`, `REDISTLS`/`REDIS_TLS` |
| `ProviderSettings` | NBA Stats/PBP settings plus the internal DFS enabled-provider registry, deadline, transport caps, and Dabble fan-out bound | `NBA_STATS_TIMEOUT_SECONDS`, `NBA_STATS_MAX_CONCURRENCY`, `NBA_API_TIMEOUT_CONNECT`, `NBA_API_TIMEOUT_READ`, `NBA_API_MAX_RETRIES`, `NBA_API_POOL_CONNECTIONS`, `NBA_API_POOL_MAXSIZE`, `DFS_ENABLED_PROVIDERS`, `DFS_BOARD_DEADLINE_SECONDS`, `DFS_PROVIDER_CONNECT_TIMEOUT_SECONDS`, `DFS_PROVIDER_READ_TIMEOUT_SECONDS`, `DFS_DABBLE_DETAIL_CONCURRENCY` |
| `LLMSettings` | API key, model, temperature, token/time limits, retries, fallback, confidence threshold | `OPENAI_API_KEY`, `LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `ENABLE_LLM_FALLBACK`, `LLM_CONFIDENCE_THRESHOLD` |
| `CORSSettings` | Exact browser origins allowed to make cross-origin requests | `CORS_ALLOWED_ORIGINS` |
| `NBASeasonSettings` | `current_season` | Derived by `current_nba_season()` |
| `CatalogSettings` | `athlete_freshness_days` | `ATHLETE_CATALOG_FRESHNESS_DAYS` (default `7`) |

General process settings (`environment`, `port`, `debug`, and `log_level`) are
also fields on `RuntimeSettings` and map to `FLASK_ENV`, `PORT`, `FLASK_DEBUG`,
and `LOG_LEVEL`.

`NBA_STATS_MAX_CONCURRENCY` is a **per-worker-process** bound: all
`NBAStatsAdapter` instances in one worker share one
`threading.BoundedSemaphore`, while each Gunicorn worker has its own gate. It
is not a cross-process lock, so the maximum simultaneous calls the whole
application can make is
`workers × NBA_STATS_MAX_CONCURRENCY` (the Procfile runs 4 workers).

The internal DFS collector receives an explicit injected provider registry. In
development and testing, omitting `DFS_ENABLED_PROVIDERS` opts into the three
recorded provider adapters (`dabble`, `prizepicks`, and `underdog`) for local
experimentation; an empty value disables them. Production must provide a
non-empty, comma-separated `DFS_ENABLED_PROVIDERS` list.

The board deadline defaults to 15 seconds. Each DFS GET defaults to a 3-second
connect cap and an 8-second read cap, both reduced to the remaining absolute
budget. A safe GET receives at most one retry for a timeout or HTTP 429, 500,
502, 503, or 504; access-denied, ordinary 4xx, and malformed responses
are not retried. Dabble fixture-detail concurrency defaults to 3. These
settings apply only to the internal collector and do not change NBA Stats or
PBP Stats timeout/health signals.

## Defaults and validation

Local and test startup is credential-free by default:

- `DATABASE_URL` defaults to `sqlite:///nba_play_types.db`.
- Redis remains optional; a failed connection disables caching.
- OpenAI fallback is enabled only when both `ENABLE_LLM_FALLBACK` is truthy
  and `OPENAI_API_KEY` is present. Without a key, deterministic NLP remains
  available.
- Firebase is optional until a protected request is made. The explicit
  `FIREBASE_ADMIN_DISABLED=true` bypass is accepted only in development,
  testing, or local environments.
- CORS defaults explicitly to `http://localhost:3000` for local development.
  `CORS_ALLOWED_ORIGINS` is a comma-separated list of exact `http://` or
  `https://` origins; wildcard `*` values are rejected.
- Canonical athlete catalogs are refreshed only for explicit seasons by
  `scripts/refresh_athlete_catalog.py`; `ATHLETE_CATALOG_FRESHNESS_DAYS`
  controls the persisted success TTL and defaults to seven days.

Production startup raises `ConfigurationError` with the invalid field names
when `DATABASE_URL` still points at the bundled SQLite fixture, Firebase
credentials are absent/invalid, `CORS_ALLOWED_ORIGINS` is not explicitly set,
or the local bypass is enabled. This prevents the process from starting with a
configuration that cannot enforce its security contract.

For example, a deployment should set:

```bash
CORS_ALLOWED_ORIGINS=https://stats.example.com,https://admin.example.com
```

## Current season rule

`current_nba_season(today)` uses the October boundary: October through
December belong to the season beginning in that calendar year; January through
September belong to the season beginning in the previous calendar year. For
example, September 30, 2026 is `2025-26`, while October 1, 2026 is `2026-27`.
The same computed value is injected into game-log route defaults, the NL
parser/mapper, cache freshness checks, and provider requests.

Tests can pass an explicit date to `current_nba_season` or a mapping to
`load_settings(environ=...)` without changing process environment state.

`NBA_STATS_TIMEOUT_SECONDS` is reserved for `stats.nba.com` calls made through
`nba_api`. `NBA_API_TIMEOUT_CONNECT`, `NBA_API_TIMEOUT_READ`, and
`NBA_API_MAX_RETRIES` configure the PBP Stats adapter's shared HTTP session;
the two providers therefore keep distinct timeout and health signals.
