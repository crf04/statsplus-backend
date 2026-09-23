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
# Branch coverage, so a half-exercised if/else does not read as covered.
COVERAGE_MIN=62

"$PYTHON_BIN" -m ruff check app tests scripts

# Test databases are small and short-lived; keep them in RAM when a writable
# /dev/shm exists (Linux), otherwise use the platform default (macOS).
PYTEST_TMPDIR=""
MIGRATION_DATABASE=""
trap 'rm -rf ${PYTEST_TMPDIR:+"$PYTEST_TMPDIR"} ${MIGRATION_DATABASE:+"$MIGRATION_DATABASE"}' EXIT
PYTEST_TMP_ARGS=()
if [[ -d /dev/shm && -w /dev/shm ]]; then
    PYTEST_TMPDIR="$(mktemp -d /dev/shm/statsplus-pytest.XXXXXX)"
    PYTEST_TMP_ARGS=(--basetemp="$PYTEST_TMPDIR/basetemp")
fi

# Distributed across cores; the suite is offline and mocked, so the only shared
# artifact is the read-only demo database. pytest-cov combines the per-worker
# data, so the coverage floor below still applies to the whole run. Test
# durations vary widely, so idle workers steal queued tests (worksteal).
TMPDIR="${PYTEST_TMPDIR:-${TMPDIR:-/tmp}}" "$PYTHON_BIN" -m pytest \
    -n auto \
    --dist worksteal \
    ${PYTEST_TMP_ARGS[@]+"${PYTEST_TMP_ARGS[@]}"} \
    --cov=app \
    --cov-branch \
    --cov-report=term-missing \
    --cov-fail-under="$COVERAGE_MIN"

MIGRATION_DATABASE="$(mktemp "${TMPDIR:-/tmp}/statsplus-migrations.XXXXXX")"
MIGRATION_DATABASE_URL="sqlite:///${MIGRATION_DATABASE}"
"$PYTHON_BIN" scripts/migrate.py --database-url "$MIGRATION_DATABASE_URL"
"$PYTHON_BIN" scripts/migrate.py --database-url "$MIGRATION_DATABASE_URL"
"$PYTHON_BIN" scripts/validate_demo_db.py
