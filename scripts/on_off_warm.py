"""Warm the on/off (WOWY) cache for a game day's injury-report players (#424).

Reads the official injury report stored for the target day (``--date``,
default today in Eastern time) and, for every Out, Doubtful or Questionable
player, fetches the two splits an Injury Scenario reads (teammates with him
off, and with him on, the floor) into Redis.  Probable and Available players
are not warmed.

Run it after each scheduled injury refresh (``railway.on_off_warm.json``:
same 30-minute cadence, offset five minutes).  It is idempotent: an entry
still fresh in the 24-hour cache costs no upstream call, so each run fetches
only players not yet cached, and a run with nothing new makes no calls.  Exits
1 when it could not store what it fetched (Redis write failure), so the
scheduler records the failure.  Needs DATABASE_URL and REDIS_URL.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.services.on_off import injury_warm_requests
from app.utils.db import _normalize_database_url, is_demo_database_url

logger = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")


def warm(repository, service, season, target_date):
    """Warm from the report stored for ``target_date`` (an Eastern date).

    The stored report is the latest official snapshot; one retrieved on
    another Eastern day is not the target day's report and is refused.
    """

    snapshot = repository.get_latest_source("nba_official")
    if snapshot is None:
        return {"state": "no_injury_report"}
    if snapshot.retrieved_at.astimezone(EASTERN).date() != target_date:
        return {"state": "no_injury_report_for_date",
                "target_date": target_date.isoformat(),
                "retrieved_at": snapshot.retrieved_at.isoformat()}
    requests = injury_warm_requests(snapshot.normalized_entries, season)
    return {"state": "warmed", "requests": len(requests), **service.warm(requests)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", type=date.fromisoformat,
        help="game day (YYYY-MM-DD, Eastern); defaults to today",
    )
    parser.add_argument(
        "--season", help="YYYY-YY season; defaults to the current season"
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or is_demo_database_url(database_url):
        parser.error("DATABASE_URL must identify a writable application database")
    from app.config.settings import load_settings
    from app.dependencies import build_dependencies
    from app.services.injury_snapshot_repository import InjurySnapshotRepository

    settings = load_settings()
    engine = create_engine(_normalize_database_url(database_url))
    try:
        dependencies = build_dependencies(settings)
        result = warm(
            InjurySnapshotRepository(engine),
            dependencies.on_off_service,
            args.season or settings.nba.current_season,
            args.date or datetime.now(timezone.utc).astimezone(EASTERN).date(),
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 1 if result.get("cache_write_failed") else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
