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
| `FeatureSettings` | `dfs_board_enabled` and `injury_report_enabled` exposure gates | `DFS_BOARD_ENABLED`, `INJURY_REPORT_ENABLED` (both default `false`) |
| `ProviderSettings` | NBA Stats/PBP settings, internal DFS provider settings, and the explicit RotoWire permission assertion | `NBA_STATS_TIMEOUT_SECONDS`, `NBA_STATS_MAX_CONCURRENCY`, `NBA_API_TIMEOUT_CONNECT`, `NBA_API_TIMEOUT_READ`, `NBA_API_MAX_RETRIES`, `NBA_API_POOL_CONNECTIONS`, `NBA_API_POOL_MAXSIZE`, `DFS_ENABLED_PROVIDERS`, `DFS_BOARD_DEADLINE_SECONDS`, `DFS_PROVIDER_CONNECT_TIMEOUT_SECONDS`, `DFS_PROVIDER_READ_TIMEOUT_SECONDS`, `DFS_DABBLE_DETAIL_CONCURRENCY`, `DFS_CACHE_FRESH_SECONDS`, `DFS_CACHE_STALE_IF_ERROR_SECONDS`, `DFS_COMPARISON_MAX_MARKETS`, provider-specific `DFS_<PROVIDER>_CACHE_*` overrides, and `ROTOWIRE_PERMISSION_GRANTED` (default `false`) |
| `LLMSettings` | API key, model, temperature, token/time limits, retries, fallback, confidence threshold | `OPENAI_API_KEY`, `LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `ENABLE_LLM_FALLBACK`, `LLM_CONFIDENCE_THRESHOLD` |
| `CORSSettings` | Exact browser origins allowed to make cross-origin requests | `CORS_ALLOWED_ORIGINS` |
| `NBASeasonSettings` | `current_season` | Derived by `current_nba_season()` |
| `CatalogSettings` | Catalog/read thresholds: athlete freshness, player-log coverage and age, event matching/schedule age, and matchup-selection H2H/archetype thin sample minimums | `ATHLETE_CATALOG_FRESHNESS_DAYS` (default `7`), `PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME` (default `5`), `EVENT_CATALOG_MAX_AGE_HOURS` (default `72`), `EVENT_MAPPING_MATCH_WINDOW_HOURS` (default `6`), `SLATE_SCHEDULE_MAX_AGE_HOURS` (default `30`), `PLAYER_GAME_LOG_MAX_AGE_HOURS` (default `30`), `MATCHUP_SELECTION_H2H_MIN_GAMES` (default `1`), `MATCHUP_SELECTION_ARCHETYPE_MIN_GAMES` (default `5`) |

General process settings (`environment`, `port`, `debug`, and `log_level`) are
also fields on `RuntimeSettings` and map to `FLASK_ENV`, `PORT`, `FLASK_DEBUG`,
and `LOG_LEVEL`.

`NBA_STATS_MAX_CONCURRENCY` is a **per-worker-process** bound: all
`NBAStatsAdapter` instances in one worker share one
`threading.BoundedSemaphore`, while each Gunicorn worker has its own gate. It
is not a cross-process lock, so the maximum simultaneous calls the whole
application can make is
`workers × NBA_STATS_MAX_CONCURRENCY` (the Procfile runs 4 workers).

Season player Diet collection uses the same NBA Stats timeout/concurrency and
PBP Stats transport settings as the other Nightly provider calls. It has no
display-threshold setting: the durable bulk seam always returns raw shares and
volumes, and the frontend owns chip thresholds. It also has no Last-15 or
request-time fallback setting; `NBASeasonSettings.current_season` selects the
explicit Nightly Season and each stored Base carries its own timezone-aware
retrieval time and availability observation.

The internal DFS collector receives an explicit injected provider registry. In
development and testing, omitting `DFS_ENABLED_PROVIDERS` disables all DFS
adapters. Tests and local experiments may explicitly configure the recorded
provider adapters (`dabble`, `prizepicks`, and `underdog`). Production must
provide a non-empty, comma-separated `DFS_ENABLED_PROVIDERS` list, whether or
not the board is published.

The RotoWire injury adapter has two independent, false-by-default gates.
`INJURY_REPORT_ENABLED=true` opts the deployment into the surface;
`ROTOWIRE_PERMISSION_GRANTED=true` asserts that written permission or explicit
legal approval covers automated collection and display. The adapter is not
constructed unless both are true. Enabled without permission returns the
matchup injury block as `unavailable/permission_required`; disabled returns
`unavailable/disabled`. There is no fallback injury provider.

Publishing the board needs both halves of that configuration.
`DFS_BOARD_ENABLED=true` says the route may be exposed and
`DFS_ENABLED_PROVIDERS` says which providers it may call; either alone
publishes nothing, and `GET /api/dfs/board` answers an authenticated request
with `404 dfs_board_disabled` before reading its query string. Both default to
off in **every** environment, including local development and tests, so a
deployment opts in explicitly. `DFS_BOARD_ENABLED=true` with an empty registry
is refused at startup with `ConfigurationError` in every environment, rather
than exposing a route that could never call a provider.

The board deadline defaults to 15 seconds. Each DFS GET defaults to a 3-second
connect cap and an 8-second read cap, both reduced to the remaining absolute
budget. A safe GET receives at most one retry for a timeout or HTTP 429, 500,
502, 503, or 504; access-denied, ordinary 4xx, and malformed responses
are not retried. Dabble fixture-detail concurrency defaults to 3. These
settings apply only to the internal collector and do not change NBA Stats or
PBP Stats timeout/health signals.

The injected DFS snapshot cache stores only complete, normalized
`ProviderSnapshot` values in Redis. Its default fresh window is 300 seconds and
its maximum stale-if-error age is 1800 seconds; both scalar settings can be
overridden per provider with `DFS_DABBLE_CACHE_FRESH_SECONDS` and
`DFS_DABBLE_CACHE_STALE_IF_ERROR_SECONDS` (and the equivalent provider name).
The cache key contains the provider, semantic NBA query, and adapter-contract
version. Boards are never serialized. A partial observation is returned to its
caller but cannot replace a complete Redis value. A stale complete value is
returned only after a later expected upstream failure, with bounded cache
provenance on the provider outcome. Redis errors fail open to direct upstream
work and never create an in-process stale store; single-flight suppression is
per worker only.

Both windows are exact decimal seconds inside one **time-window domain**, owned
by `app.domain.freshness` and enforced at startup: a window is at least `1E-6`
seconds (the microsecond every age is measured at) and at most `1E+9` seconds
(about thirty-one years), and a provider's fresh window may never exceed its
stale-if-error age. A boolean, a nonfinite value, a value outside that domain,
or a fresh window past its own ceiling raises `ConfigurationError` naming the
variable and the domain, never quoting the value.

The ordering is a runtime invariant, not only a configuration check: the same
`cache_window_policy` decides it wherever a cache is built, so a directly
constructed `ProviderSnapshotCache`, a `ProviderSnapshotCacheCoordinator`, and
a coordinator-decorated provider are all refused a fresh window past their
stale-if-error age. Equal windows are accepted, because a fresh window is
exclusive at its endpoint and a maximum age is inclusive at its own.

Every catalog and mapping window enters the same domain in its own unit and is
kept exactly: `EVENT_CATALOG_MAX_AGE_HOURS`,
`EVENT_MAPPING_MATCH_WINDOW_HOURS`, `SLATE_SCHEDULE_MAX_AGE_HOURS`, and
`PLAYER_GAME_LOG_MAX_AGE_HOURS` in hours, `ATHLETE_CATALOG_FRESHNESS_DAYS` in
whole days. They are read as
exact decimals rather than through a float, bounded once at startup, and only
then converted to a whole-microsecond `timedelta` — so an absurd window is one
typed configuration error rather than an `OverflowError` from a service
constructor. Direct service overrides (`EventCatalogService(max_age=...)`,
`event_match_window(...)`, `AthleteCatalogService(freshness_days=...)`,
`SlateService(schedule_max_age=...)`, and
`PlayerGameLogRepository(stats_surface_max_age=...)`) use the same authority,
so a service built by hand can hold no window an operator could not configure.
Because the cache and the comparison board read the identical
authority, every configuration the process starts with can be used by both.

Those same two windows decide comparison freshness, through one shared boundary:
a fresh window is exclusive at its endpoint and a maximum age is inclusive at
its own. A snapshot inside its provider's fresh window is contemporaneous — an
observation exactly one window old is not, at the cache and on the board alike;
one past it may still enter a Comparison Group as explicitly stale while its age
is at most the stale-if-error age; beyond that its markets stay visible on the
board but enter no group. `DFS_COMPARISON_MAX_MARKETS` is the post-filter comparison-board
ceiling and defaults to 10000. A read that observes more markets than the
ceiling is refused with `board_too_large`, the observed count, and the
supported narrowing filters; it is never truncated.

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
- Athlete and event catalogs have independent persisted freshness policies;
  refreshes require explicit seasons and do not use a wall-clock season default.

Production startup raises `ConfigurationError` with the invalid field names
when `DATABASE_URL` still points at the bundled SQLite fixture, Firebase
credentials are absent/invalid, `CORS_ALLOWED_ORIGINS` is not explicitly set,
`DFS_ENABLED_PROVIDERS` names no provider, or the local bypass is enabled. This prevents the process from starting with a
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
