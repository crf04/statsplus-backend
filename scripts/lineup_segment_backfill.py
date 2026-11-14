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

``--makes`` fills the made field goals and free throws (crf04/statsplus#500,
migration 089) of games stored before them instead.  It refetches each such
game's two documents, derives its segments again and writes only the makes,
in one transaction per game, and only when every count the game already
stores is unchanged and each row's points are 2·(FGM − 3PM) + 3·3PM + FTM.
A game that fails the check is listed under ``mismatched`` and left as it
was; it stays pending, so ``scripts/rotation_refresh.py <season> --game-id
<id>`` rewrites it whole when its new counts are right.  Rerun it until
``remaining`` is 0, as above::

    python scripts/lineup_segment_backfill.py 2025-26 --makes --database-url "$DATABASE_URL"

Exits 1 when any game failed to fetch or was mismatched.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
        else {"stored": 0, "rejected": [], "segments_rejected": [], "failed": []}
    )
    written = segments.written_game_ids(season)
    remaining = [game_id for game_id in completed if game_id not in written]
    if not remaining:
        segments.record_coverage(season, clock())
    return {
        "season": season,
        "completed_games": len(completed),
        "attempted": len(batch),
        "stored": collected["stored"] - len(collected["segments_rejected"]),
        "rejected": collected["rejected"] + collected["segments_rejected"],
        "failed": collected["failed"],
        "remaining": len(remaining),
        "covered": not remaining,
    }


def fill_makes(
    provider,
    segments,
    season: str,
    *,
    max_games: int | None = None,
    throttle_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Fill the makes of up to ``max_games`` stored games that lack them."""

    import requests

    from app.services.lineup_segments import derive_segments
    from app.services.rotation_derivation import RotationRejected
    from app.utils.telemetry import ProviderResponseError

    pending = segments.games_without_makes(season)
    batch = pending if max_games is None else pending[:max_games]
    filled = 0
    mismatched: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for index, game_id in enumerate(batch):
        if index and throttle_seconds:
            sleep(throttle_seconds)
        try:
            box = provider.fetch_game_stats(game_id, season)
            play_by_play = provider.fetch_play_by_play(game_id)
        except (requests.RequestException, ProviderResponseError, ValueError) as error:
            failed.append({"game_id": game_id, "error": str(error)})
            continue
        try:
            reason = segments.fill_makes(derive_segments(box, play_by_play, season))
        except (RotationRejected, KeyError, TypeError, ValueError) as error:
            reason = f"segments rejected: {error}"
        if reason is None:
            filled += 1
        else:
            mismatched.append({"game_id": game_id, "reason": reason})
    return {
        "season": season,
        "pending": len(pending),
        "attempted": len(batch),
        "filled": filled,
        "mismatched": mismatched,
        "failed": failed,
        "remaining": len(segments.games_without_makes(season)),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("season", help="YYYY-YY season, 2025-26 or later")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--max-games", type=int, help="stop after this many games")
    parser.add_argument(
        "--makes",
        action="store_true",
        help="fill made field goals and free throws of games stored before migration 089",
    )
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
        if args.makes:
            result = fill_makes(
                dependencies.rotation_collection_service.provider,
                LineupSegmentRepository(dependencies.engine),
                args.season,
                max_games=args.max_games,
                throttle_seconds=args.throttle,
            )
        else:
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
    return 1 if result["failed"] or result.get("mismatched") else 0


if __name__ == "__main__":
    raise SystemExit(main())
