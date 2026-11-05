"""Re-derive the ledger's ``offensive_possessions`` from the archived raw rows.

Operator command for crf04/statsplus#433.  It reads each game's archived
``canonical_game_ledger_raw_rows`` and fills the nullable
``offensive_possessions`` columns: each player's PBPStats ``OffPoss``, and each
team's sum of them over five.  It never calls a provider and never queues a
composition job.  ``offensive_possessions`` is outside the typed game checksum,
and a re-derived game whose checksum would move is rejected rather than
written; the report's ``checksums_changed`` compares the season's stored
checksums before and after the run and is always 0.

Idempotent.  The values are a pure function of the archived rows and only the
facts whose value differs are written, so a rerun (or a rerun after a crash at
any game) reports ``updated: 0``.  ``--dry-run`` reports what would change
without writing.

Publishing.  The backfill publishes nothing.  Once the active pair's season is
filled, a traditional-opponent rebuild (``POST
/api/admin/collection/publication-rebuilds``, then
``scripts/publication_rebuild.py``) recomposes its Season and L15 windows in
``traditional_opponent.v3``, which carries ``possessions`` and so Defensive
Rating.  Before the backfill that rebuild composes v2 and fails as
``publication_format_unsupported``, promoting nothing.

Exit status: 0 when every game was derived or already current, 2 when a game's
archived rows fail validation (reported in ``rejected``, never written) or the
schema lacks migration 085.

    python -m scripts.ledger_possessions_backfill 2025-26 --database-url "$DATABASE_URL"
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

from sqlalchemy import create_engine, inspect

from app.services.canonical_game_ledger import CanonicalGameLedgerRepository

_TABLES = ("canonical_game_ledger_player_facts", "canonical_game_ledger_team_facts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("season", help="canonical season, for example 2025-26")
    parser.add_argument("--database-url", help="SQLAlchemy database URL (or set DATABASE_URL)")
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
        parser.error("a database target is required: pass --database-url or set DATABASE_URL")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        missing = [
            f"{table}.offensive_possessions"
            for table in _TABLES
            if "offensive_possessions" not in {column["name"] for column in inspector.get_columns(table)}
        ]
        if missing:
            # Migration 085 owns the schema; this command never alters it.
            print(
                json.dumps({"error": "migration_085_required", "missing_columns": missing}, sort_keys=True),
                file=sys.stderr,
            )
            return 2
        repository = CanonicalGameLedgerRepository(engine)
        before = repository.game_checksums(args.season)
        result = repository.backfill_offensive_possessions(
            args.game_ids, season=args.season, dry_run=args.dry_run
        )
        after = repository.game_checksums(args.season)
    finally:
        engine.dispose()
    changed = sum(1 for game_id, checksum in before.items() if after.get(game_id) != checksum)
    print(json.dumps({"season": args.season, **asdict(result), "checksums_changed": changed}, sort_keys=True))
    return 2 if result.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
