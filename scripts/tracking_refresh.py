"""Collect rebound chances and potential assists: the nightly step and the backfill.

Stores NBA.com player-tracking values (``player_game_tracking``, migration 078)
for every completed regular-season game of each ``--season`` that has none yet
(crf04/statsplus-backend#604).  Rebound chances come from
``boxscoreplayertrackv3`` (one call per game), potential assists from the
``leaguedashptstats`` Passing day feed (one call per game day).  A game whose
tracking is not published yet is listed under ``not_published`` and is picked up
by a later run; a game whose feed could not be fetched is listed under
``failed``.  Reruns are idempotent: stored games are skipped, and ``--game-id``
re-reads a named game and leaves identical rows.

Nightly cron (``railway.tracking.json``)::

    python scripts/tracking_refresh.py --season 2025-26 --season 2026-27 \
        --database-url "$DATABASE_URL" --max-games 80

Backfill a season by hand, bounded and paced, repeating until ``remaining`` is 0::

    python scripts/tracking_refresh.py --season 2025-26 \
        --database-url "$DATABASE_URL" --max-games 400 --throttle 0.6

Exits 1 when any game failed to fetch, so the scheduler records the failure;
games not yet published exit 0.  Needs a network path to ``stats.nba.com``
(reachable from Railway containers).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--season", action="append", required=True, dest="seasons", help="YYYY-YY season (repeatable)"
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--max-games", type=int, help="stop after this many games per season")
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.6,
        help="seconds to pause between games (default 0.6)",
    )
    parser.add_argument(
        "--game-id",
        action="append",
        dest="game_ids",
        help="re-read this completed regular-season game id (repeatable; needs one --season)",
    )
    args = parser.parse_args(argv)
    if args.game_ids and len(args.seasons) != 1:
        parser.error("--game-id needs exactly one --season")

    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies
    from app.migrations import run_migrations
    from app.utils.db import get_engine

    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    run_migrations(get_engine(settings))
    dependencies = build_dependencies(settings)
    failed = False
    try:
        for season in args.seasons:
            result = dependencies.player_tracking_collection_service.collect(
                season,
                max_games=args.max_games,
                throttle_seconds=args.throttle,
                game_ids=args.game_ids,
            )
            print(json.dumps(result, sort_keys=True), flush=True)
            failed = failed or bool(result["failed"])
    finally:
        dependencies.engine.dispose()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
