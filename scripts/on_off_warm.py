"""Warm the on/off (WOWY) cache for today's injury-report players (#424).

Reads the latest stored official injury report, and for every Out, Doubtful
or Questionable player fetches the two splits an Injury Scenario reads
(teammates with him off, and with him on, the floor) into Redis.  Idempotent:
an entry still fresh in the 24-hour cache costs no upstream call, so a re-run
within the TTL fetches nothing.  Needs DATABASE_URL and REDIS_URL; run it
nightly after the injury report is stored.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.services.on_off import injury_warm_requests
from app.utils.db import _normalize_database_url, is_demo_database_url

logger = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")


def warm(repository, service, season, now):
    """Warm from the latest stored report; refuses a report from another day."""

    snapshot = repository.get_latest_source("nba_official")
    if snapshot is None:
        return {"state": "no_injury_report"}
    if snapshot.retrieved_at.astimezone(EASTERN).date() != now.astimezone(EASTERN).date():
        return {"state": "injury_report_not_today",
                "retrieved_at": snapshot.retrieved_at.isoformat()}
    requests = injury_warm_requests(snapshot.normalized_entries, season)
    return {"state": "warmed", "requests": len(requests), **service.warm(requests)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
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
            settings.nba.current_season,
            datetime.now(timezone.utc),
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
