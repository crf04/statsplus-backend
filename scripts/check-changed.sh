#!/usr/bin/env bash
set -euo pipefail

# Inner-loop gate: lint and test only what the diff against origin/master can
# break (scripts/changed_scope.py decides). ./scripts/check.sh stays the full
# suite and CI stays the authority.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -x .venv/bin/python ]]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="${PYTHON:-python3}"
fi

SCOPE="$("$PYTHON_BIN" scripts/changed_scope.py)"
MODE=""
REASON=""
MIGRATIONS=0
CHANGED=0
LINT_FILES=()
TEST_FILES=()
while IFS='=' read -r key value; do
    case "$key" in
        changed) CHANGED="$value" ;;
        mode) MODE="$value" ;;
        reason) REASON="$value" ;;
        migrations) MIGRATIONS="$value" ;;
        lint) LINT_FILES+=("$value") ;;
        test) TEST_FILES+=("$value") ;;
    esac
done <<<"$SCOPE"

printf 'check-changed: %s changed file(s) since merge-base with origin/master\n' "$CHANGED"
printf 'check-changed: mode %s (%s)\n' "$MODE" "$REASON"

if [[ "$MODE" == none ]]; then
    printf 'check-changed: nothing to check\n'
    exit 0
fi

if ((${#LINT_FILES[@]})); then
    "$PYTHON_BIN" -m ruff check "${LINT_FILES[@]}"
    "$PYTHON_BIN" -m ruff format --check "${LINT_FILES[@]}"
else
    printf 'check-changed: no Python files to lint\n'
fi

# Same temporary-directory handling as check.sh.
PYTEST_TMPDIR=""
MIGRATION_DATABASE=""
trap 'rm -rf ${PYTEST_TMPDIR:+"$PYTEST_TMPDIR"} ${MIGRATION_DATABASE:+"$MIGRATION_DATABASE"}' EXIT
PYTEST_TMP_ARGS=()
if [[ -d /dev/shm && -w /dev/shm ]]; then
    PYTEST_TMPDIR="$(mktemp -d /dev/shm/statsplus-pytest.XXXXXX)"
    PYTEST_TMP_ARGS=(--basetemp="$PYTEST_TMPDIR/basetemp")
fi

PYTEST_ARGS=()
if [[ "$MODE" == affected ]]; then
    PYTEST_ARGS=(${TEST_FILES[@]+"${TEST_FILES[@]}"})
fi
if [[ "$MODE" == full ]] || ((${#PYTEST_ARGS[@]})); then
    printf 'check-changed: pytest %s\n' "${PYTEST_ARGS[*]:-(full suite)}"
    # No coverage floor: a partial run cannot meet it. Exit 5 means every
    # selected test was deselected by marker (live or integration only).
    status=0
    TMPDIR="${PYTEST_TMPDIR:-${TMPDIR:-/tmp}}" "$PYTHON_BIN" -m pytest \
        -n auto \
        --dist worksteal \
        ${PYTEST_TMP_ARGS[@]+"${PYTEST_TMP_ARGS[@]}"} \
        ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"} || status=$?
    if [[ "$status" == 5 ]]; then
        printf 'check-changed: no selected test runs under the default markers\n'
    elif [[ "$status" != 0 ]]; then
        exit "$status"
    fi
else
    printf 'check-changed: no tests import the changed files\n'
fi

if [[ "$MIGRATIONS" == 1 ]]; then
    MIGRATION_DATABASE="$(mktemp "${TMPDIR:-/tmp}/statsplus-migrations.XXXXXX")"
    MIGRATION_DATABASE_URL="sqlite:///${MIGRATION_DATABASE}"
    "$PYTHON_BIN" scripts/migrate.py --database-url "$MIGRATION_DATABASE_URL"
    "$PYTHON_BIN" scripts/migrate.py --database-url "$MIGRATION_DATABASE_URL"
    "$PYTHON_BIN" scripts/validate_demo_db.py
fi

printf 'check-changed: passed (mode %s); CI is the completion gate\n' "$MODE"
