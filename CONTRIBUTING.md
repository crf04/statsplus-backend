# Contributing

Thanks for helping improve StatsPlus NBA Backend. Keep changes small, documented, and easy to verify.

## Local setup

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
```

The script creates a Python 3.11.9 virtual environment, installs the hashed, fully resolved `requirements-lock.txt`, and creates a local `.env` from the example when needed.

`requirements-lock.txt` is generated from `requirements-dev.txt` for the exact version in `runtime.txt`. After changing either requirements input, regenerate it with:

```bash
uv pip compile requirements-dev.txt \
  --python-version "$(sed -n '1s/^python-//p' runtime.txt | tr -d '[:space:]')" \
  --generate-hashes \
  --output-file requirements-lock.txt
```

## Before opening a change

Run:

```bash
./scripts/check.sh
```

This is the same Ruff and pytest gate used by CI. To run only the tests:

```bash
./run_tests.sh
```

If a test requires network access or a real provider credential, mock that integration unless the test is explicitly marked as an integration check.

## Database changes

Use `python scripts/migrate.py --database-url <database-url>` (or set
`DATABASE_URL`) to create or upgrade an application database. The command
requires one of those explicit targets, is repeatable, and records applied
versions in `schema_migrations`; use a disposable SQLite database for
migration tests. It rejects the tracked `nba_play_types.db` file as a
read-only fixture, and masks database passwords in status output.

Run `python scripts/validate_demo_db.py` to validate that fixture. This command
opens the file read-only and rejects missing schema elements or user records.

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
