"""Store each game's team context for the Matchup: the nightly step and the backfill.

Recomputes, for every Event Catalog game of the season except Preseason,
All-Star and postponed games, each team's rest before the game, its Season and
Last 10 pace and ratings, and its last five results, from the Canonical Game
Ledger games strictly before the game's Eastern date (crf04/statsplus#466).
Only rows whose values changed are written, so a rerun reports every game
``unchanged``; a game that left the schedule loses its rows (``removed``).

Nightly, the ledger cron service runs it after ``ledger_refresh.py`` and
``rotation_refresh.py`` (``railway.ledger.json``).  Fill an earlier season by
running it once by hand::

    python scripts/game_context_refresh.py 2025-26 --database-url "$DATABASE_URL"

It calls no provider.
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
    args = parser.parse_args(argv)

    from app.config.settings import RuntimeSettings
    from app.migrations import run_migrations
    from app.services.game_context import GameContextRepository
    from app.utils.db import get_engine

    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    engine = get_engine(settings)
    run_migrations(engine)
    try:
        result = GameContextRepository(engine).refresh(args.season)
    finally:
        engine.dispose()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
