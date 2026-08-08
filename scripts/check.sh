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
COVERAGE_MIN=46

"$PYTHON_BIN" -m ruff check app tests scripts
"$PYTHON_BIN" -m pytest \
    --cov=app \
    --cov-report=term-missing \
    --cov-fail-under="$COVERAGE_MIN"
