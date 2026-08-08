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
| Persistence | `app/models/` | SQLAlchemy models and session creation |

Most route modules currently construct service instances at import time. Tests should patch the instance exposed by the route module—for example, `app.routes.game_routes.game_service`—or instantiate a service directly with a temporary or mocked engine.

## Data-source seams

The app reads from three distinct sources:

| Source | Access path | Expected behavior |
| --- | --- | --- |
| Bundled SQLite demo data | `app.utils.db.get_engine()` | Default, offline-capable read path |
| NBA Stats | `nba_api` → `stats.nba.com` | Live game logs and selected team/player data; bounded by `NBA_STATS_TIMEOUT_SECONDS` |
| PBP Stats | shared `requests.Session` → `api.pbpstats.com` | Play-by-play aggregates and the existing external health probe |

Redis is an optional cache. Connection failure disables caching without blocking startup. OpenAI is an optional fallback for low-confidence natural-language parsing. Firebase is optional for local development but should be configured in production.

The default database URL is `sqlite:///nba_play_types.db`, relative to the current working directory. Run commands from the repository root or set an absolute `DATABASE_URL`.

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

## Test seams

- App and route behavior: use the `app` and `client` fixtures in `tests/conftest.py`.
- Route/service interaction: patch the module-level service on the route module.
- Provider failures: raise the relevant `requests` timeout/error from a patched service or endpoint constructor.
- Parser behavior: use the bundled SQLite data and patch static NBA lookups when the parser needs a deterministic team list.
- LLM behavior: inject or mock the OpenAI client; the default suite must not require an API key.

The authoritative local and CI gate is `./scripts/check.sh`.

## Known seams to improve incrementally

- Service construction at module import time couples imports to database, Redis, and parser initialization.
- The request layer mixes synchronous Flask handlers with `asyncio.run` for game-log work.
- Several services catch broad exceptions and return sentinel values, which can hide provider-specific failures.
- The bundled database schema is implicit rather than managed through migrations.

Keep these constraints visible when changing nearby code. Improve them behind tests in small slices instead of combining them with unrelated feature work.
