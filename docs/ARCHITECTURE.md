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
point for the database engine, cache client, and request services. The app
factory constructs that graph once and stores it in
`app.extensions["dependencies"]`. Route modules contain read-only handles that
resolve objects from the active app; importing a route never selects a
database, connects to Redis, initializes Firebase, or loads a parser. Tests can
provide a complete replacement graph through the `DEPENDENCIES` app-factory
override without patching module globals.

## Data-source seams

The app reads from three distinct sources:

| Source | Access path | Expected behavior |
| --- | --- | --- |
| Bundled SQLite demo data | `app.utils.db.get_engine()` | Default, offline-capable read path |
| NBA Stats | `nba_api` → `stats.nba.com` | Live game logs and selected team/player data; bounded by `NBA_STATS_TIMEOUT_SECONDS` |
| PBP Stats | shared `requests.Session` → `api.pbpstats.com` | Play-by-play aggregates and the existing external health probe |

Redis is an optional cache. Connection failure disables caching without blocking startup. OpenAI is an optional fallback for low-confidence natural-language parsing. Firebase is optional for local development but should be configured in production.

The default database URL is `sqlite:///nba_play_types.db`, relative to the current working directory. Run commands from the repository root or set an absolute `DATABASE_URL`.

Runtime configuration is loaded and validated once by
`app.config.settings.load_settings()`. The resulting typed `RuntimeSettings`
object is attached to the app and passed into request services; see
[SETTINGS.md](SETTINGS.md) for the field and environment-variable contract.

## Request flows

Game logs:

```text
GET /api/games/game_logs
  → Firebase auth (or explicit local-only bypass)
  → GameService.get_filtered_logs
  → cached nba_api PlayerGameLogs request
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
/api/data route
  → DataService provider calls
  → dataframe transformations
  → replace one or more database tables
```

Data refreshes are mutations, not health checks. Use a disposable database and mocked providers when testing them.

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
- Route/service interaction: replace methods on the dependency graph supplied
  through the `DEPENDENCIES` app-factory override.
- Provider failures: raise the relevant `requests` timeout/error from a patched service or endpoint constructor.
- Parser behavior: use the bundled SQLite data and patch static NBA lookups when the parser needs a deterministic team list.
- LLM behavior: inject or mock the OpenAI client; the default suite must not require an API key.

The authoritative local and CI gate is `./scripts/check.sh`.

## Known seams to improve incrementally

- The app-owned dependency graph keeps app-factory isolation explicit while
  avoiding database, Redis, Firebase, and parser initialization during route
  imports.
- The request layer mixes synchronous Flask handlers with `asyncio.run` for game-log work.
- Several services catch broad exceptions and return sentinel values, which can hide provider-specific failures.
- The bundled provider-generated tables are validated as a public fixture; they
  are not application migration targets.

Keep these constraints visible when changing nearby code. Improve them behind tests in small slices instead of combining them with unrelated feature work.
