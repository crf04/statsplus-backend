"""Re-derive the ledger's per-game ``usage_pct`` from the archived raw rows.

Operator command for crf04/statsplus#236 (backend #450).  It reads each game's
archived ``canonical_game_ledger_raw_rows`` and fills the nullable ``usage_pct``
column on the player facts from PBPStats' ``Usage`` (0-100, unrounded).  It
never calls a provider and never queues a composition job, and ``usage_pct`` is
outside the typed game checksum, so no game checksum changes.

Idempotent.  The value is a pure function of the archived rows and only the
players whose value differs are written, so a rerun (or a rerun after a crash at
any game) reports ``updated: 0`` and leaves every row as it was.  ``--dry-run``
reports what would change without writing.

Exit status: 0 when every game was derived or already current, 2 when a game's
archived rows fail validation (reported in ``rejected``, never written) or the
schema lacks migration 071.

    python -m scripts.ledger_usage_backfill 2025-26 --database-url "$DATABASE_URL"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect  # noqa: E402

from app.services.canonical_game_ledger import CanonicalGameLedgerRepository  # noqa: E402

_TABLE = "canonical_game_ledger_player_facts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("season", help="canonical season, for example 2025-26")
    parser.add_argument(
        "--database-url", help="SQLAlchemy database URL (or set DATABASE_URL)"
    )
    parser.add_argument(
        "--game-id",
        action="append",
        dest="game_ids",
        help="restrict to one game (repeatable); default is the whole season",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error(
            "a database target is required: pass --database-url or set DATABASE_URL"
        )
    engine = create_engine(database_url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns(_TABLE)}
        if "usage_pct" not in columns:
            # Migration 071 owns the schema; this command never alters it.
            print(
                json.dumps(
                    {
                        "error": "migration_071_required",
                        "missing_columns": [f"{_TABLE}.usage_pct"],
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        result = CanonicalGameLedgerRepository(engine).backfill_usage(
            args.game_ids, season=args.season, dry_run=args.dry_run
        )
    finally:
        engine.dispose()
    print(json.dumps({"season": args.season, **asdict(result)}, sort_keys=True))
    return 2 if result.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
