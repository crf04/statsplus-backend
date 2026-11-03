"""Backfill a season's lineup segments for on/off splits (crf04/statsplus#410).

Refetches the LiveData box score and play-by-play of every completed
regular-season game in the Event Catalog that has no segments yet, and
stores its rotation and segments together, as the nightly rotation job does
(``scripts/rotation_refresh.py``).  A game the derivation rejects is recorded
as missing.  When every completed game is stored or recorded as missing, the
season's coverage is recorded and ``/api/on_off`` serves it from segments
instead of PBP Stats.

Resumable and idempotent: games already written are skipped, so rerun it
(with ``--max-games`` to bound a run) until ``remaining`` is 0.  A game whose
documents could not be fetched stays pending for the next run.  Two requests
per game and a two-second pause between games keep it near one request a
second::

    python scripts/lineup_segment_backfill.py 2025-26 --database-url "$DATABASE_URL"

Exits 1 when any game failed to fetch.  Seasons before 2025-26 are refused:
they keep the PBP Stats path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIRST_SEGMENT_SEASON = "2025-26"


def backfill(
    collection,
    segments,
    season: str,
    *,
    max_games: int | None = None,
    throttle_seconds: float = 2.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """Collect up to ``max_games`` games without segments; record coverage when none remain."""

    if season < FIRST_SEGMENT_SEASON:
        raise ValueError(f"segments start in {FIRST_SEGMENT_SEASON}; {season} keeps PBP Stats")
    completed = collection.completed_game_ids(season)
    written = segments.written_game_ids(season)
    pending = [game_id for game_id in completed if game_id not in written]
    batch = pending if max_games is None else pending[:max_games]
    collected = (
        collection.collect(season, game_ids=batch, throttle_seconds=throttle_seconds)
        if batch
        else {"stored": 0, "rejected": [], "failed": []}
    )
    written = segments.written_game_ids(season)
    remaining = [game_id for game_id in completed if game_id not in written]
    if not remaining:
        segments.record_coverage(season, clock())
    return {
        "season": season,
        "completed_games": len(completed),
        "attempted": len(batch),
        "stored": collected["stored"],
        "rejected": collected["rejected"],
        "failed": collected["failed"],
        "remaining": len(remaining),
        "covered": not remaining,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("season", help="YYYY-YY season, 2025-26 or later")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--max-games", type=int, help="stop after this many games")
    parser.add_argument(
        "--throttle",
        type=float,
        default=2.0,
        help="seconds to pause between games (default 2: about one request a second)",
    )
    args = parser.parse_args(argv)

    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies
    from app.migrations import run_migrations
    from app.services.lineup_segment_repository import LineupSegmentRepository
    from app.utils.db import get_engine

    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    run_migrations(get_engine(settings))
    dependencies = build_dependencies(settings)
    try:
        result = backfill(
            dependencies.rotation_collection_service,
            LineupSegmentRepository(dependencies.engine),
            args.season,
            max_games=args.max_games,
            throttle_seconds=args.throttle,
        )
    finally:
        dependencies.engine.dispose()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
