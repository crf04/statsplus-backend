#!/usr/bin/env python3
"""Refresh canonical athlete catalogs for one or more explicit NBA seasons."""

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

from app.config.settings import load_settings
from app.migrations import run_migrations
from app.services.athlete_catalog_service import AthleteCatalogService
from app.utils.db import _normalize_database_url, is_demo_database_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh the application-owned canonical athlete catalog."
    )
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy database URL (or set DATABASE_URL)",
    )
    parser.add_argument(
        "--season",
        dest="season_options",
        action="append",
        help="one explicit canonical NBA season (repeat for multiple seasons)",
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        help="explicit canonical NBA seasons (alternative to repeated --season)",
    )
    return parser


def _database_url_for_refresh(
    parser: argparse.ArgumentParser, requested_url: str | None
) -> str:
    database_url = requested_url if requested_url is not None else os.getenv("DATABASE_URL")
    if not database_url:
        parser.error(
            "a refresh target is required: pass --database-url or set DATABASE_URL"
        )
    return database_url


def _redacted_database_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid database URL>"


def _run(database_url: str, seasons: tuple[str, ...]):
    """Migrate and refresh a writable database; no background worker is started."""

    settings = load_settings(overrides={"DATABASE_URL": database_url})
    engine = create_engine(_normalize_database_url(database_url))
    try:
        run_migrations(engine)
        service = AthleteCatalogService(engine, settings=settings)
        return service.refresh(seasons)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    database_url = _database_url_for_refresh(parser, args.database_url)
    raw_seasons = (args.season_options or []) + (args.seasons or [])
    seasons = tuple(
        season.strip()
        for value in raw_seasons
        for season in value.split(",")
        if season.strip()
    )
    if not seasons:
        parser.error("at least one explicit --season is required")
    if is_demo_database_url(database_url):
        parser.error(
            "the tracked nba_play_types.db is a read-only demo database and "
            "cannot be an athlete catalog target"
        )

    try:
        result = _run(database_url, seasons)
    except Exception as error:
        print(
            f"Athlete catalog refresh failed for {_redacted_database_url(database_url)}: "
            "the operation did not complete.",
            file=sys.stderr,
        )
        del error
        return 1

    for state in result.results:
        detail = f" ({state.row_count} rows)"
        if state.failure_summary:
            detail += f": {state.failure_summary}"
        print(f"{state.season}: {state.status}{detail}")
    succeeded = sum(state.status == "succeeded" for state in result.results)
    print(
        f"Refreshed {succeeded}/{len(seasons)} athlete catalog season(s) in "
        f"{_redacted_database_url(database_url)}."
    )
    return 1 if result.has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
