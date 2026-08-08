#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RUNTIME_PYTHON_VERSION="$(sed -n '1s/^python-//p' runtime.txt | tr -d '[:space:]')"
if [[ ! "$RUNTIME_PYTHON_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'runtime.txt must contain an exact Python version such as python-3.11.9\n' >&2
    exit 1
fi

RUNTIME_PYTHON_MAJOR_MINOR="${RUNTIME_PYTHON_VERSION%.*}"
PIP_BOOTSTRAP_VERSION="25.3"

check_python_version() {
    EXPECTED_PYTHON_VERSION="$RUNTIME_PYTHON_VERSION" "$1" -c \
        'import os, sys; expected = tuple(int(part) for part in os.environ["EXPECTED_PYTHON_VERSION"].split(".")); assert sys.version_info[:3] == expected, f"Python {expected[0]}.{expected[1]}.{expected[2]} is required"'
}

if command -v uv >/dev/null 2>&1; then
    if [[ ! -x .venv/bin/python ]]; then
        uv venv --python "$RUNTIME_PYTHON_VERSION" .venv
    fi
    check_python_version .venv/bin/python
    uv pip install --python .venv/bin/python -r requirements-lock.txt
else
    if [[ ! -x .venv/bin/python ]]; then
        PYTHON_BIN="${PYTHON:-}"
        if [[ -z "$PYTHON_BIN" ]]; then
            for candidate in "python${RUNTIME_PYTHON_VERSION}" "python${RUNTIME_PYTHON_MAJOR_MINOR}"; do
                if command -v "$candidate" >/dev/null 2>&1; then
                    PYTHON_BIN="$candidate"
                    break
                fi
            done
        fi
        if [[ -z "$PYTHON_BIN" ]]; then
            printf 'Python %s is required; set PYTHON to its interpreter or install it\n' \
                "$RUNTIME_PYTHON_VERSION" >&2
            exit 1
        fi

        check_python_version "$PYTHON_BIN"
        "$PYTHON_BIN" -m venv .venv
    fi
    check_python_version .venv/bin/python
    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
        .venv/bin/python -m ensurepip --upgrade
    fi
    .venv/bin/python -m pip install --upgrade "pip==${PIP_BOOTSTRAP_VERSION}"
    .venv/bin/python -m pip install --require-hashes -r requirements-lock.txt
fi

if [[ ! -e .env ]]; then
    cp .env.example .env
    printf 'Created .env from .env.example\n'
fi

printf 'Environment ready. Run ./scripts/check.sh\n'
