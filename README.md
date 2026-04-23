# StatsPlus NBA Backend

Flask API for NBA player stats, game logs, team context, and natural-language stat queries. The project combines deterministic query parsing with optional OpenAI fallback so a frontend can ask questions like "LeBron last 10 home games with 25+ points" and receive structured filters for the game-log API.

## What It Shows

- Flask app factory with grouped REST blueprints.
- SQLite demo database with public NBA-derived data for immediate local use.
- Natural-language query parsing with spaCy/rule-based extraction and optional LLM fallback.
- Firebase-authenticated user routes, while public stat routes support optional auth where appropriate.
- Health checks for database and NBA API connectivity.

## Quick Start

Use Python 3.11, matching `runtime.txt`.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

The API runs at `http://localhost:5000`.

The bundled `nba_play_types.db` is intentionally tracked as a demo database so reviewers can run the project without a separate data import. It should contain public NBA data only; the `users` table is expected to be empty in the public repo.

## Configuration

`DATABASE_URL` defaults to the bundled SQLite database:

```bash
DATABASE_URL=sqlite:///nba_play_types.db
```

OpenAI fallback is optional. If `OPENAI_API_KEY` is missing, the app continues in deterministic NLP mode.

Firebase Admin credentials are required only for routes protected by Firebase auth. Provide them through environment variables, never through committed JSON files.

## API Snapshot

Health:

- `GET /api/health/db`
- `GET /api/health/detailed`
- `GET /api/health/nba-api`

Players and teams:

- `GET /api/players`
- `GET /api/players/profile?player_name=LeBron%20James&category=Playtypes`
- `GET /api/teams`
- `GET /api/teams/stats?team=LAL&category=Defense`

Game logs and natural language:

- `GET /api/games/game_logs?player_name=LeBron%20James&minutes_filter=25,48`
- `POST /api/nl-query`

Example:

```bash
curl -X POST http://localhost:5000/api/nl-query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <firebase-id-token>" \
  -d '{"query": "Stephen Curry last 10 home games with 25+ points"}'
```

Data refresh endpoints are under `/api/data/*` and call external NBA/PBP APIs. They are useful for maintenance, but the bundled DB is enough for demo exploration.

## Auth Behavior

- Required Firebase auth: `/api/games/game_logs`, `/api/nl-query`, and most `/api/user/*` routes.
- Optional Firebase auth: player, team, and data-management routes.
- Firebase Admin can initialize from `FIREBASE_SERVICE_ACCOUNT_PATH`, `FIREBASE_SERVICE_ACCOUNT_JSON`, individual Firebase env vars, or Google application default credentials.

## Development

Install development tools separately:

```bash
pip install -r requirements-dev.txt
```

Run tests:

```bash
python -m pytest
```

Or use the thin wrapper:

```bash
./run_tests.sh
```

Detailed API and NLP notes live in `docs/`.

## Deployment

The included `Procfile` runs:

```bash
gunicorn --workers 4 --threads 2 --timeout 180 --keep-alive 5 --max-requests 1000 --max-requests-jitter 100 --bind 0.0.0.0:${PORT} wsgi:app
```

Set production secrets through your hosting provider environment. Before publishing this repo, rotate any previously exposed Firebase service-account key and publish from a clean history that does not contain credential JSON.
