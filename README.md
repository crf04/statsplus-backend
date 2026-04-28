# StatsPlus NBA Backend

Flask API for NBA player stats, game logs, team context, and natural-language stat queries. The backend exposes conventional REST endpoints and a hybrid natural-language parser that can turn prompts such as "Stephen Curry last 10 home games with 25+ points" into structured filters for the game-log API.

## What is included

- Flask app factory with blueprints for players, teams, games, data refreshes, health checks, users, and natural-language queries.
- Bundled SQLite demo database, `nba_play_types.db`, so the project can run immediately after install.
- NBA data integrations through `nba_api` and pbpstats endpoints.
- Deterministic NLP parsing with spaCy, aliases, fuzzy matching, date parsing, and optional OpenAI fallback.
- Firebase Admin authentication support for protected routes, with local development fallback when Firebase is not configured.
- Optional Redis-backed caching for NBA API responses.

## Prerequisites

- Python 3.11. The deployed runtime is pinned in `runtime.txt` as `python-3.11.9`.
- A shell with virtualenv support.
- Optional: Redis if you want shared caching through `REDIS_URL`.
- Optional: Firebase Admin credentials if you want real auth locally.
- Optional: OpenAI API key if you want LLM fallback for harder NL queries.

## Setup

```bash
git clone <repo-url>
cd statsplus-backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`requirements.txt` includes the spaCy English model wheel used by the parser. If your environment blocks wheel installation from GitHub, install dependencies after allowing that download or install `en_core_web_sm` manually with `python -m spacy download en_core_web_sm`.

For contributor tooling:

```bash
pip install -r requirements-dev.txt
```

## Configuration

The app loads `.env` automatically through `python-dotenv`. These are the most important variables:

| Variable | Required | Default or behavior |
| --- | --- | --- |
| `DATABASE_URL` | No | `sqlite:///nba_play_types.db` |
| `PORT` | No | `5000` |
| `FLASK_DEBUG` | No | `1` in `run.py` |
| `LOG_LEVEL` | No | `INFO` |
| `OPENAI_API_KEY` | No | If absent, NL queries stay in NLP-only mode |
| `LLM_MODEL` | No | `gpt-4o-mini` |
| `ENABLE_LLM_FALLBACK` | No | `True` |
| `LLM_CONFIDENCE_THRESHOLD` | No | `0.7` |
| `REDIS_URL` | No | If unavailable, caching falls back without blocking app startup |
| `FIREBASE_SERVICE_ACCOUNT_PATH` | No | Path to local Firebase Admin JSON |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | No | Inline service-account JSON for hosted deploys |
| `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL` | No | Alternative Firebase credential form |

Never commit `.env`, Firebase service-account JSON, OpenAI keys, database dumps containing private users, or provider credentials.

## Demo database

`nba_play_types.db` is intentionally tracked for public demos and local onboarding. It should contain public NBA-derived data only. The local `users` table is created by the app and should not be populated with real user data in public snapshots.

The default SQLite URL is relative to the repository root, so run commands from the project directory unless you set an absolute `DATABASE_URL`.

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
```

`GET /api/health/nba-api` and `/api/health/detailed` call an external NBA data endpoint, so they can fail if the network or upstream API is unavailable.

## Authentication behavior

The code uses Firebase ID tokens in `Authorization: Bearer <token>` headers.

- Protected routes: `GET /api/games/game_logs`, `POST /api/nl-query`, and most `/api/user/*` routes.
- Optional-auth routes: player, team, and data-management routes, plus `POST /api/user/activity/ping`.
- Local development fallback: if Firebase Admin cannot initialize because no credentials are configured, `@require_auth` logs a warning and allows protected requests through as a synthetic `dev-user`.
- Real Firebase mode: once Firebase Admin initializes, protected routes require a valid Firebase ID token and return `401` for missing or invalid tokens.

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
curl "http://localhost:5000/api/games/game_logs?player_name=LeBron%20James&minutes_filter=25,48&location_filter=Home&self_filters[PTS]=25,60"
```

With Firebase initialized, add:

```bash
-H "Authorization: Bearer <firebase-id-token>"
```

Natural-language parsing:

```bash
curl -X POST http://localhost:5000/api/nl-query \
  -H "Content-Type: application/json" \
  -d '{"query": "Stephen Curry last 10 home games with 25+ points"}'
```

Data refresh endpoints:

```bash
curl http://localhost:5000/api/data/fetch_playtypes
curl -X PUT http://localhost:5000/api/data/player_PBP
curl http://localhost:5000/api/data/update_database
```

Refresh endpoints call external NBA/PBP APIs and may replace local tables. The bundled database is enough for exploring read-only routes.

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

The wrapper script runs the same test command:

```bash
./run_tests.sh
```

Tests should not require real Firebase, OpenAI, Redis, or NBA network calls unless a specific test explicitly mocks or opts into that behavior.

## Project map

```text
app/
  __init__.py              Flask app factory and blueprint registration
  routes/                  HTTP route handlers
  services/                Business logic, NBA data calls, NL/LLM services
  services/nl_query/       Parser, mapper, executor, validators
  utils/                   Auth, database, cache, date, and helper utilities
docs/
  API_DOCUMENTATION.md     Endpoint reference
  NLP_SYSTEM.md            Natural-language query architecture
prompts/
  system_prompt_optimized.txt
tests/
  pytest suite
```

## Deployment notes

The included `Procfile` starts Gunicorn:

```bash
gunicorn --workers 4 --threads 2 --timeout 180 --keep-alive 5 --max-requests 1000 --max-requests-jitter 100 --bind 0.0.0.0:${PORT} wsgi:app
```

For production:

- Set `DATABASE_URL` to your managed database if you are not using SQLite.
- Set Firebase credentials so protected routes enforce real tokens.
- Set `OPENAI_API_KEY` only if LLM fallback should be enabled.
- Configure `REDIS_URL` if you want cache sharing across processes or instances.
- Rotate any credentials that were ever committed or shared outside your secret manager.
