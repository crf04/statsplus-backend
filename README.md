# StatsPlus NBA Backend

Flask API for NBA player stats, game logs, team context, and natural-language stat queries. The backend exposes conventional REST endpoints and a hybrid natural-language parser that can turn prompts such as "Stephen Curry last 10 home games with 25+ points" into structured filters for the game-log API.

## What is included

- Flask app factory with blueprints for players, teams, games, data refreshes, health checks, users, and natural-language queries.
- Bundled SQLite demo database, `nba_play_types.db`, so the project can run immediately after install.
- NBA data integrations through the injectable, instrumented NBA Stats adapter
  (`nba_api` → `stats.nba.com`) and the shared-session PBP Stats adapter.
- Deterministic NLP parsing with spaCy, aliases, fuzzy matching, date parsing, and optional OpenAI fallback.
- Firebase Admin authentication for protected routes, with an explicit local-only bypass for credential-free development.
- Optional Redis-backed caching for NBA API responses.

## Prerequisites

- Python 3.11.9. The exact supported runtime is pinned in `runtime.txt` as `python-3.11.9`.
- A shell with virtualenv support.
- Optional: Redis if you want shared caching through `REDIS_URL`.
- Optional for read-only local exploration: Firebase Admin credentials. Protected and admin routes require Firebase unless the explicit local-only bypass is enabled.
- Optional: OpenAI API key if you want LLM fallback for harder NL queries.

## Setup

```bash
git clone https://github.com/crf04/statsplus-backend.git
cd statsplus-backend
./scripts/bootstrap.sh
source .venv/bin/activate
```

The bootstrap script creates `.venv`, installs the hashed, fully resolved `requirements-lock.txt`, and copies `.env.example` to `.env` when needed. It uses `uv` when available and falls back to Python's standard virtual-environment tooling.

`requirements.txt` includes the spaCy English model wheel used by the parser. If your environment blocks wheel installation from GitHub, install dependencies after allowing that download or install `en_core_web_sm` manually with `python -m spacy download en_core_web_sm`.

`requirements-lock.txt` is generated from `requirements-dev.txt` for Python 3.11.9. After changing either requirements input, regenerate it with `uv`:

```bash
uv pip compile requirements-dev.txt \
  --python-version "$(sed -n '1s/^python-//p' runtime.txt | tr -d '[:space:]')" \
  --generate-hashes \
  --output-file requirements-lock.txt
```

For a manual installation without the bootstrap script:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-lock.txt
cp .env.example .env
```

## Configuration

The app loads `.env` automatically through `python-dotenv`. The authoritative
interface is the typed `RuntimeSettings` object; see
[docs/SETTINGS.md](docs/SETTINGS.md) for the complete model, environment
mapping, defaults, production validation, and current-season rule. These are
the most important variables:

| Variable | Required | Default or behavior |
| --- | --- | --- |
| `DATABASE_URL` | No | `sqlite:///nba_play_types.db` |
| `PORT` | No | `5000` |
| `FLASK_DEBUG` | No | `1` in `run.py` |
| `LOG_LEVEL` | No | `INFO` |
| `OPENAI_API_KEY` | No | If absent, NL queries stay in NLP-only mode |
| `LLM_MODEL` | No | `gpt-4o-mini` |
| `ENABLE_LLM_FALLBACK` | No | Enabled only when `OPENAI_API_KEY` is present |
| `LLM_CONFIDENCE_THRESHOLD` | No | `0.7` |
| `REDIS_URL` | No | If unavailable, caching falls back without blocking app startup |
| `NBA_STATS_TIMEOUT_SECONDS` | No | `10`; timeout for `stats.nba.com` requests |
| `CORS_ALLOWED_ORIGINS` | Local default only; required in production | Comma-separated exact `http://` or `https://` origins; local default is `http://localhost:3000` |
| `NBA_STATS_MAX_CONCURRENCY` | No | `10`; process-shared bound for in-flight NBA Stats calls |
| `NBA_API_TIMEOUT_CONNECT` / `NBA_API_TIMEOUT_READ` | No | `10` / `30`; PBP Stats connect/read timeouts |
| `NBA_API_MAX_RETRIES` | No | `3`; retries for safe PBP Stats requests |
| `DABBLE_CONNECT_TIMEOUT_SECONDS` / `DABBLE_READ_TIMEOUT_SECONDS` | No | `5` / `30`; dedicated Dabble DFS feed timeouts |
| `DABBLE_MAX_RETRIES` | No | `2`; retries for safe Dabble reads |
| `DABBLE_MAX_FIXTURES_PER_REQUEST` | No | `5`; maximum fixture-details fan-out for one lines request |
| `FIREBASE_ADMIN_DISABLED` | No | `false`; local/test-only credential bypass, rejected outside those environments |
| `FIREBASE_SERVICE_ACCOUNT_PATH` | No | Path to local Firebase Admin JSON |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | No | Inline service-account JSON for hosted deploys |
| `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL` | No | Alternative Firebase credential form |

Never commit `.env`, Firebase service-account JSON, OpenAI keys, database dumps containing private users, or provider credentials.

## Demo database

`nba_play_types.db` is intentionally tracked for public demos and local onboarding. It should contain public NBA-derived data only. The local `users` table is created by the app and should not be populated with real user data in public snapshots.

The default SQLite URL is relative to the repository root, so run commands from the project directory unless you set an absolute `DATABASE_URL`.

## Database schema and migrations

Application-owned tables are managed by repeatable migrations. Create a fresh
schema or upgrade an existing application database by passing an explicit
database URL or setting `DATABASE_URL`:

```bash
python scripts/migrate.py --database-url sqlite:////tmp/statsplus.sqlite3
# Or: DATABASE_URL=sqlite:////tmp/statsplus.sqlite3 python scripts/migrate.py
```

The migration command has no database-file fallback: it fails when neither
target is provided. It also rejects the tracked `nba_play_types.db` fixture as
a read-only demo database. Status output preserves the non-secret parts of a
URL while masking its password.

Running the command again is safe; applied versions are recorded in the
`schema_migrations` table. Keep migration databases disposable in tests.

Validate the bundled fixture without changing it:

```bash
python scripts/validate_demo_db.py
```

The validator checks the required public tables and columns and fails if the
`users` table contains records.

## Run locally

```bash
source .venv/bin/activate
python run.py
```

The API listens on `http://localhost:5000`.

Useful smoke checks:

```bash
curl http://localhost:5000/api/health/db
curl http://localhost:5000/api/players
curl "http://localhost:5000/api/teams/stats?team=Los%20Angeles%20Lakers&category=Traditional"
curl "http://localhost:5000/api/dfs/lines?competition=NBA&player=LeBron" \
  -H "Authorization: Bearer <firebase-id-token>"
```

`GET /api/health/nba-api` checks `stats.nba.com`; `GET /api/health/pbp-api`
checks `api.pbpstats.com`; and `/api/health/detailed` reports both providers
under distinct `nba_api` and `pbp_stats` keys. These endpoints can fail if the
network or either upstream API is unavailable.

The app factory assembles one `ApplicationDependencies` graph containing the
database, cache, provider adapters, services, durable refresh coordinator, and
health service. Routes resolve those injected objects from the active app;
tests can replace the graph with the `DEPENDENCIES` override. CORS uses exact
origins from `CORS_ALLOWED_ORIGINS` (with `http://localhost:3000` as the local
default); production requires an explicit allow-list.

Every response carries an `X-Request-ID` used to correlate a request, its
provider calls, and its logs; safe inbound values are echoed, otherwise one is
generated. Every call to `stats.nba.com` (via `nba_api`), `api.pbpstats.com`,
and the Dabble DFS read feed is recorded as one sanitized provider event with outcome,
duration, retry count, cache status, and HTTP status — operator counters and
recent events are available on the admin-only `GET /api/data/telemetry`.

Live requests to `stats.nba.com` use a 10-second timeout by default. If that
provider times out, game-log requests return `503 Service Unavailable` instead
of exposing a generic internal-server error. Override the timeout with
`NBA_STATS_TIMEOUT_SECONDS` when needed.

`NBA_STATS_MAX_CONCURRENCY` bounds in-flight NBA Stats provider calls per
worker process: all adapters in one worker share the configured gate, while
each Gunicorn worker has its own gate. The maximum calls the whole application
can issue at once is workers × `NBA_STATS_MAX_CONCURRENCY` (the Procfile runs
4 workers, so up to 4 × the bound). This is a per-process bound, not a
cluster-global lock.

Application failures use a documented structured JSON error response with
stable category codes, including `invalid_input`, `resource_not_found`,
`provider_unavailable`, `authentication_required`, `invalid_token`, and
`forbidden`. See the [API error contract](docs/API_DOCUMENTATION.md#error-responses)
for statuses and examples; internal exception details are logged but not
returned to clients.

## Authentication behavior

The code uses Firebase ID tokens in `Authorization: Bearer <token>` headers.

- Protected routes fail closed when Firebase Admin is unavailable (`503 Service Unavailable`). Requests without a valid Firebase ID token receive `401 Unauthorized`.
- Protected routes include `GET /api/games/game_logs`, both `/api/dfs/*` routes, `POST /api/nl-query`, and most `/api/user/*` routes.
- Admin routes include `/api/user/admin/stats`, all `/api/data/*` endpoints, and `PUT /api/players/fetch`. They require a verified Firebase ID token with one of these custom claims: `admin=true`, `role=admin`, or `roles` containing `admin`.
- For local, credential-free development only, set `FLASK_ENV=development` and `FIREBASE_ADMIN_DISABLED=true`. This explicit bypass uses a synthetic `dev-user`, is rejected outside development/tests and in production, and must not be enabled in a deployed environment.
- Player and team read routes remain optional-auth. `POST /api/user/activity/ping` also remains optional-auth.

## API examples

Base URL:

```text
http://localhost:5000/api
```

Players:

```bash
curl http://localhost:5000/api/players
curl "http://localhost:5000/api/players/profile?player_name=LeBron%20James&category=Playtypes"
```

Teams:

```bash
curl http://localhost:5000/api/teams
curl "http://localhost:5000/api/teams/stats?team=Los%20Angeles%20Lakers&category=Traditional"
```

Game logs:

```bash
curl "http://localhost:5000/api/games/game_logs?player_name=LeBron%20James&minutes_filter=25,48&location_filter=Home&self_filters[PTS]=25,60" \
  -H "Authorization: Bearer <firebase-id-token>"
```

When using the local-only bypass, set `FIREBASE_ADMIN_DISABLED=true` in `.env` instead of sending a token. Do not use that bypass outside local development.

Natural-language parsing:

```bash
curl -X POST http://localhost:5000/api/nl-query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <firebase-id-token>" \
  -d '{"query": "Stephen Curry last 10 home games with 25+ points"}'
```

Admin data refresh endpoints:

```bash
curl -X POST http://localhost:5000/api/data/update_database \
  -H "Authorization: Bearer <firebase-admin-token>"
curl -X POST http://localhost:5000/api/data/fetch_players_with_teams \
  -H "Authorization: Bearer <firebase-admin-token>"
curl -X PUT http://localhost:5000/api/data/player_PBP \
  -H "Authorization: Bearer <firebase-admin-token>"
curl -X PUT http://localhost:5000/api/data/opponent_PBP \
  -H "Authorization: Bearer <firebase-admin-token>"
curl -X PUT http://localhost:5000/api/players/fetch \
  -H "Authorization: Bearer <firebase-admin-token>"
curl http://localhost:5000/api/data/fetch_playtypes \
  -H "Authorization: Bearer <firebase-admin-token>"
curl http://localhost:5000/api/data/jobs/<job_id> \
  -H "Authorization: Bearer <firebase-admin-token>"
curl http://localhost:5000/api/data/telemetry \
  -H "Authorization: Bearer <firebase-admin-token>"
```

Refresh endpoints schedule durable jobs: they return `202 Accepted` with a
`job_id`, and the refresh runs afterward; poll
`GET /api/data/jobs/<job_id>` (admin-only) for status. They call external
NBA/PBP APIs and may replace local tables. The bundled database is enough for
exploring read-only routes. Jobs are stored in the application database and
claimed with expiring leases by the app-scoped dispatcher, so queued work and
work abandoned by a crashed process can be recovered after restart. Operation
names and request IDs are persisted; executable handlers are registered at app
startup. `progress` moves through fetch/transform/publish phases and remains
safe to retry after a failed attempt. Execution is at-least-once: an expired
lease may rerun a handler. Every claim has an `attempt_count` fencing token;
the publisher renews that token inside the same database transaction as the
live-table swap, so stale attempts cannot overwrite a newer publication. This
fences publication but does not cancel provider calls that were already in
flight when the lease expired.

See [docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md) for endpoint details and [docs/NLP_SYSTEM.md](docs/NLP_SYSTEM.md) for the natural-language pipeline.

## Tests

Run the public test suite from the repository root:

```bash
python -m pytest
```

More verbose output:

```bash
python -m pytest -v --tb=short
```

Run the same lint and test gate used by CI:

```bash
./scripts/check.sh
```

`./run_tests.sh` remains available when only pytest is needed.

Tests should not require real Firebase, OpenAI, Redis, or NBA network calls unless a specific test explicitly mocks or opts into that behavior.

Live provider-contract tests hit the real providers and are excluded from the
default gate (registered `live` marker). Opt in explicitly:

```bash
LIVE_CONTRACT_TESTS=true python -m pytest -m live
```

## Project map

```text
app/
  __init__.py              Flask app factory and blueprint registration
  routes/                  HTTP route handlers
  providers/               Injectable NBA Stats and PBP Stats seams
  services/                Business logic, refresh queue, NL/LLM services
  services/nl_query/       Deterministic parser and typed query models
  utils/                   Auth, database, cache, date, and helper utilities
docs/
  ARCHITECTURE.md          Runtime interfaces, data sources, and test seams
  API_DOCUMENTATION.md     Endpoint reference
  NLP_SYSTEM.md            Natural-language query architecture
prompts/
  system_prompt_optimized.txt  Production prompt loaded by NLService
  system_prompt.txt            Reference prompt used by the LLM smoke script
tests/
  pytest suite
scripts/
  bootstrap.sh             Create the Python 3.11 development environment
  check.sh                 Run the authoritative Ruff and pytest gate
  migrate.py               Create or upgrade the application schema
  validate_demo_db.py      Validate the public demo database read-only
```

## Deployment notes

The included `Procfile` starts Gunicorn:

```bash
gunicorn --workers 4 --threads 2 --timeout 180 --keep-alive 5 --max-requests 1000 --max-requests-jitter 100 --bind 0.0.0.0:${PORT} wsgi:app
```

For production:

- Set `DATABASE_URL` to your managed database if you are not using SQLite.
- Set Firebase credentials so protected and admin routes enforce real tokens and claims.
- Keep `FIREBASE_ADMIN_DISABLED=false`; the bypass is accepted only in development/tests. Never enable it in a deployed environment.
- Set `OPENAI_API_KEY` only if LLM fallback should be enabled.
- Configure `REDIS_URL` if you want cache sharing across processes or instances.
- Rotate any credentials that were ever committed or shared outside your secret manager.
