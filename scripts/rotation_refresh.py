"""Collect game rotations from NBA LiveData: the nightly step and the backfill.

Stores the rotation (starters, minutes, stints) of every completed regular-season
game in the Event Catalog that has none yet (crf04/statsplus#223).  A game
whose derived rotation fails validation is not stored and is listed under
``rejected``; a game whose documents cannot be fetched is listed under
``failed`` and retried next run.  Reruns are idempotent: stored games are
skipped, and ``--game-id`` replaces a named game's rows with identical ones.

Nightly, the ledger cron service runs it right after ``ledger_refresh.py``
(``railway.ledger.json``).  Backfill a season by running it by hand with a
bound and a pause between requests, repeating until ``remaining`` is 0::

    python scripts/rotation_refresh.py 2025-26 --database-url "$DATABASE_URL" \
        --max-games 200 --throttle 1

Exits 1 when any game failed to fetch, so the scheduler records the failure;
rejected games alone exit 0 and are reported in the summary.
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
    parser.add_argument("season", help="YYYY-YY season")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--max-games", type=int, help="stop after this many games")
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.5,
        help="seconds to pause between games (default 0.5)",
    )
    parser.add_argument(
        "--game-id",
        action="append",
        dest="game_ids",
        help="collect this regular-season game id (repeatable) instead of every missing game",
    )
    args = parser.parse_args(argv)

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
    try:
        result = dependencies.rotation_collection_service.collect(
            args.season,
            max_games=args.max_games,
            throttle_seconds=args.throttle,
            game_ids=args.game_ids,
        )
    finally:
        dependencies.engine.dispose()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
