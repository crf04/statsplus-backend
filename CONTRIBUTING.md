# Contributing

Thanks for helping improve StatsPlus NBA Backend. Keep changes small, documented, and easy to verify.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

The dev requirements include runtime dependencies plus pytest and lint tooling.

## Before opening a change

Run:

```bash
python -m pytest
```

For more detail:

```bash
python -m pytest -v --tb=short
```

If a test requires network access or a real provider credential, mock that integration unless the test is explicitly marked as an integration check.

## Secrets and data

Do not commit:

- `.env`
- OpenAI API keys
- Firebase service-account JSON
- Google application credentials
- Redis or database passwords
- Database dumps containing real users or private data

The tracked `nba_play_types.db` is intended as a public demo database. Keep it limited to public NBA-derived data and avoid committing real `users` table content.

## Documentation

When route behavior, auth requirements, environment variables, or supported query parameters change, update `README.md`, `docs/API_DOCUMENTATION.md`, or `docs/NLP_SYSTEM.md` in the same change.
