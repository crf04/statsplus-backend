#!/usr/bin/env python3
"""Keep the dedicated Railway projection collector awake on a five-minute beat."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from datetime import timedelta

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import collect_projections

#: How often the service wakes to ask whether a provider is due.
BEAT_SECONDS = 300


def check_cadence(settings: object) -> None:
    """Refuse a cadence that lets Latest Player Projections lapse between polls.

    A provider is polled on the first beat at or after ``last_poll + interval``,
    so the gap between confirmations can reach the interval plus one beat.
    Latest projections are live only while that confirmation is younger than
    ``PROJECTION_LIVE_MAX_AGE``; a longer gap empties every Slate and Matchup
    pool until the next poll.
    """

    from app.services.projection_archive import PROJECTION_LIVE_MAX_AGE

    beat = timedelta(seconds=BEAT_SECONDS)
    for name in ("slow_interval", "fast_interval"):
        interval = getattr(settings, name)
        if interval + beat >= PROJECTION_LIVE_MAX_AGE:
            raise RuntimeError(
                f"projection collection {name} of {interval} plus the "
                f"{beat} beat reaches the {PROJECTION_LIVE_MAX_AGE} live window; "
                "lines would disappear between polls"
            )


def run() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for projection collection")
    if collect_projections.is_demo_database_url(database_url):
        raise RuntimeError("the read-only demo database cannot collect projections")
    settings = collect_projections.load_settings(overrides={"DATABASE_URL": database_url})
    check_cadence(settings.projection_collection)
    dependencies = collect_projections.build_dependencies(settings)
    coordinator = dependencies.projection_collection_coordinator
    while True:
        try:
            collect_projections.run_once(coordinator)
        except Exception as error:
            print(f"projection collector attempt failed: {error}")
        time.sleep(BEAT_SECONDS)


if __name__ == "__main__":
    run()
