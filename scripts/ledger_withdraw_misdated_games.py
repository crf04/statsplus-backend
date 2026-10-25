"""Withdraw ledger games the LiveData fallback stored on their tip's UTC date.

Operator command for crf04/statsplus#142.  Before the LiveData date fix, a game
accepted from the NBA LiveData fallback took the UTC date of ``gameTimeUTC``,
so a tip at 8 PM ET or later was stored on the next day.  A correction cannot
change a game's identity, so every later PBP recheck of such a game fails.
This command lists every stored game whose date differs from its Event
Catalog tip's Eastern date, each with a verdict:

- ``withdraw``: fallback evidence stored on the tip's UTC date.  Its typed
  facts and raw rows are deleted, and the next ``ledger_refresh.py`` run
  re-accepts it through the governed path.  Observations are never deleted.
- ``referenced_by_publication``: a publication version contains the game, so
  its rows stay for the correction path.  ``publication_ids`` names them.
- ``needs_investigation``: any other disagreement.  Nothing is written.

Idempotent: a rerun withdraws nothing more.  ``--dry-run`` writes nothing and
reads any season; without it the season must be the active season.

    python scripts/ledger_withdraw_misdated_games.py 2026-27 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.services.canonical_game_ledger import CanonicalGameLedgerRepository, LedgerValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("season", help="canonical season, for example 2026-27")
    parser.add_argument("--database-url", help="SQLAlchemy database URL (or set DATABASE_URL)")
    parser.add_argument(
        "--dry-run", action="store_true", help="list misdated games and verdicts without writing"
    )
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a database target is required: pass --database-url or set DATABASE_URL")
    engine = create_engine(database_url)
    try:
        games = CanonicalGameLedgerRepository(engine).withdraw_misdated_games(
            args.season, dry_run=args.dry_run
        )
    except LedgerValidationError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "season": args.season,
                "dry_run": args.dry_run,
                "games": [
                    {
                        "game_id": game.game_id,
                        "stored_game_date": game.stored_game_date.isoformat(),
                        "eastern_game_date": game.eastern_game_date.isoformat(),
                        "verdict": game.verdict,
                        "publication_ids": list(game.publication_ids),
                    }
                    for game in games
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
