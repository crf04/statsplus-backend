#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -x .venv/bin/python ]]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="${PYTHON:-python3}"
fi

# Coverage floor. Raise this as tests are added; never lower it to make CI pass.
COVERAGE_MIN=52

"$PYTHON_BIN" -m ruff check app tests scripts
"$PYTHON_BIN" -m pytest \
    --cov=app \
    --cov-report=term-missing \
    --cov-fail-under="$COVERAGE_MIN"

MIGRATION_DATABASE="$(mktemp "${TMPDIR:-/tmp}/statsplus-migrations.XXXXXX")"
trap 'rm -f "$MIGRATION_DATABASE"' EXIT
MIGRATION_DATABASE_URL="sqlite:///${MIGRATION_DATABASE}"
"$PYTHON_BIN" scripts/migrate.py --database-url "$MIGRATION_DATABASE_URL"
"$PYTHON_BIN" scripts/migrate.py --database-url "$MIGRATION_DATABASE_URL"
"$PYTHON_BIN" scripts/validate_demo_db.py
