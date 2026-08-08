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
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.migrations import MigrationResult, run_migrations  # noqa: E402
from app.utils.db import (  # noqa: E402
    _normalize_database_url,
    is_demo_database_url,
)


# Keep the private helper name for callers that used the original CLI seam;
# target classification itself lives with the database URL utilities.
_is_demo_database = is_demo_database_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or upgrade the StatsPlus application schema."
    )
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy database URL (or set DATABASE_URL)",
    )
    return parser


def _database_url_for_migration(
    parser: argparse.ArgumentParser, requested_url: str | None
) -> str:
    """Resolve the explicit migration target without falling back to the fixture."""
    database_url = requested_url if requested_url is not None else os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a migration target is required: pass --database-url or set DATABASE_URL")
    return database_url


def _redacted_database_url(database_url: str) -> str:
    """Render a database URL without exposing its password."""
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid database URL>"


def _run(database_url: str) -> MigrationResult:
    engine = create_engine(_normalize_database_url(database_url))
    try:
        return run_migrations(engine)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Run migrations and return a process exit status."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    database_url = _database_url_for_migration(parser, args.database_url)
    if _is_demo_database(database_url):
        parser.error(
            "the tracked nba_play_types.db is a read-only demo database and "
            "cannot be a migration target"
        )

    result = _run(database_url)
    redacted_url = _redacted_database_url(database_url)
    if result.applied:
        print(
            f"Applied {len(result.applied)} migration(s) to {redacted_url}: "
            f"{', '.join(result.applied)}"
        )
    else:
        print(
            f"Database is already up to date at version {result.current_version}: {redacted_url}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
