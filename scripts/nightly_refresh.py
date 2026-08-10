#!/usr/bin/env python3
"""Run stats and schedule refreshes as one deployment-owned process unit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.config.settings import load_settings  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.providers.nba_stats import NBAStatsAdapter  # noqa: E402
from app.services.data_service import DataService  # noqa: E402
from app.services.event_catalog_service import EventCatalogService  # noqa: E402
from app.services.stats_freshness_repository import (  # noqa: E402
    StatsFreshnessRepository,
)
from app.utils.db import _normalize_database_url, is_demo_database_url  # noqa: E402


def run_nightly_refresh(
    refresh_stats: Callable[[], Any], refresh_schedule: Callable[[], Any]
) -> int:
    """Run the complete unit, retrying from its first step exactly once."""

    for attempt in range(1, 3):
        failed_step = "stats"
        try:
            stats_succeeded = refresh_stats() is not False
        except Exception:
            stats_succeeded = False
        if stats_succeeded:
            failed_step = "schedule"
            try:
                refresh_schedule()
                return 0
            except Exception:
                pass
        disposition = "retrying" if attempt == 1 else "no retries remain"
        print(
            f"Nightly Refresh attempt {attempt} failed during "
            f"{failed_step} refresh; {disposition}.",
            file=sys.stderr,
        )
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the StatsPlus Nightly Refresh.")
    parser.add_argument(
        "--database-url", help="SQLAlchemy database URL (or set DATABASE_URL)"
    )
    return parser


def _run(database_url: str) -> int:
    """Assemble and execute the command against one writable database."""
    settings = load_settings(overrides={"DATABASE_URL": database_url})
    engine = create_engine(_normalize_database_url(database_url))
    try:
        run_migrations(engine)
        provider = NBAStatsAdapter(settings=settings)
        stats_freshness = StatsFreshnessRepository(engine)
        data_service = DataService(
            engine,
            settings=settings,
            nba_stats_provider=provider,
            stats_freshness=stats_freshness,
        )
        event_service = EventCatalogService(
            engine, settings=settings, nba_stats_provider=provider
        )
        return run_nightly_refresh(
            data_service.update_all_data,
            lambda: event_service.refresh(settings.nba.current_season),
        )
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error(
            "a refresh target is required: pass --database-url or set DATABASE_URL"
        )
    if is_demo_database_url(database_url):
        parser.error("the tracked nba_play_types.db is a read-only demo database")

    result = _run(database_url)
    print(json.dumps({"status": "succeeded" if result == 0 else "failed"}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
