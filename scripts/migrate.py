#!/usr/bin/env python3
"""Apply repeatable migrations to an application database."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.migrations import MigrationResult, run_migrations  # noqa: E402
from app.utils.db import (  # noqa: E402
    DEFAULT_SQLITE_PATH,
    _normalize_database_url,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or upgrade the StatsPlus application schema."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", DEFAULT_SQLITE_PATH),
        help="SQLAlchemy database URL (default: DATABASE_URL or the bundled SQLite URL)",
    )
    return parser


def _run(database_url: str) -> MigrationResult:
    engine = create_engine(_normalize_database_url(database_url))
    try:
        return run_migrations(engine)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Run migrations and return a process exit status."""
    args = _build_parser().parse_args(argv)
    result = _run(args.database_url)
    if result.applied:
        print(
            f"Applied {len(result.applied)} migration(s) to {args.database_url}: "
            f"{', '.join(result.applied)}"
        )
    else:
        print(
            f"Database is already up to date at version {result.current_version}: "
            f"{args.database_url}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
