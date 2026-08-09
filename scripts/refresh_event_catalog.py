#!/usr/bin/env python3
"""Refresh one or more explicit NBA seasons into the canonical event catalog.

This is an operator/deployment command, not a background scheduler.  It runs
one whole-season retrieval, publishes the validated result atomically, and
exits.  Pass ``--fixture`` for an offline recorded ScheduleLeagueV2 payload.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.config.settings import load_settings  # noqa: E402
from app.errors import ProviderUnavailableError  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.providers.nba_stats import NBAStatsAdapter  # noqa: E402
from app.services.event_catalog_service import EventCatalogService  # noqa: E402
from app.services.nba_stats_adapter import (  # noqa: E402
    parse_recorded_schedule,
    validate_canonical_season,
)
from app.utils.db import _normalize_database_url, is_demo_database_url  # noqa: E402


class _RecordedScheduleProvider:
    """Tiny provider used by the command's offline fixture seam."""

    def __init__(self, payload: dict):
        self.payload = payload

    def fetch_whole_season_schedule(self, *, season: str):
        return parse_recorded_schedule(self.payload, season=season)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh the canonical event catalog for explicit NBA seasons."
    )
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy database URL (or set DATABASE_URL)",
    )
    parser.add_argument(
        "--season",
        required=True,
        action="append",
        help="Explicit canonical NBA season; repeat for multiple seasons, for example 2025-26",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="Recorded ScheduleLeagueV2 JSON payload for an offline refresh",
    )
    return parser


def _database_url_for_command(
    parser: argparse.ArgumentParser,
    requested_url: str | None,
) -> str:
    database_url = requested_url if requested_url is not None else os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a refresh target is required: pass --database-url or set DATABASE_URL")
    if is_demo_database_url(database_url):
        parser.error(
            "the tracked nba_play_types.db is a read-only demo database and "
            "cannot receive the event catalog"
        )
    return database_url


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        seasons = tuple(sorted({validate_canonical_season(season) for season in args.season}))
    except ValueError as error:
        parser.error(str(error))
    database_url = _database_url_for_command(parser, args.database_url)
    settings = load_settings(overrides={"DATABASE_URL": database_url})
    engine = create_engine(_normalize_database_url(database_url))
    try:
        run_migrations(engine)
        if args.fixture is not None:
            provider = _RecordedScheduleProvider(json.loads(args.fixture.read_text()))
        else:
            provider = NBAStatsAdapter(settings=settings)
        service = EventCatalogService(engine, provider, settings=settings)
        result = service.refresh(seasons)
        payload = {
            "results": [
                {"season": item.season, "event_count": item.event_count, "refreshed_at": item.refreshed_at}
                for item in result.results
            ],
            "failures": result.failures,
        }
        print(
            json.dumps(payload, sort_keys=True)
        )
        return 1 if result.failures else 0
    except ProviderUnavailableError as error:
        print(error.public_message, file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
