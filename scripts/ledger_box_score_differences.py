"""List the open differences between the ledger's provider and NBA's official box score.

Each ledger acceptance from 2026-27 compares every player's counting stats with
``boxscoretraditionalv3`` and publishes the box score's value
(crf04/statsplus-backend#763).  This prints one JSON object per game, player
and stat the provider still disagrees on, oldest first, the same items the
run summary's ``open_box_score_differences`` counts.  An item resolves when a
later acceptance finds the two agree::

    python scripts/ledger_box_score_differences.py --database-url "$DATABASE_URL" [--season 2026-27]
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
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--season", help="YYYY-YY season (default: every season)")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    from app.services.collection_control import CollectionControlService

    engine = create_engine(args.database_url)
    try:
        for difference in CollectionControlService(engine).open_box_score_differences(season=args.season):
            print(json.dumps(difference, sort_keys=True), flush=True)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
