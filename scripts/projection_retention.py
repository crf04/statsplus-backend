#!/usr/bin/env python3
"""Prune the projection archive to three days and record its line history (#502).

  --dry-run   read only: print exact counts to delete and keep per table and
              provider, the protected rows and the history rows it would write
  --execute   the real run: write the history, then delete in short batches

Exactly one of the two is required.  The nightly cron runs the same job only
after one ``--execute`` run has succeeded.  ``DATABASE_URL`` is read from the
environment unless ``--database-url`` is given.  Prints the JSON report only,
never the database address.  Exits 1 when the run failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect

from app.services.event_catalog_repository import EventCatalogRepository
from app.services.projection_retention import ProjectionArchiveRetention
from app.utils.db import _normalize_database_url, is_demo_database_url


class _CatalogEvents:
    """The Event Catalog read the archive's closing reads use."""

    def __init__(self, repository: EventCatalogRepository) -> None:
        self._repository = repository

    def get_events_by_ids(self, season, game_ids):
        return self._repository.list_events_by_ids(season, game_ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("--database-url or DATABASE_URL is required")
    if is_demo_database_url(args.database_url):
        parser.error("the read-only demo database has no projection archive")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    engine = create_engine(_normalize_database_url(args.database_url))
    try:
        if not inspect(engine).has_table("projection_line_history"):
            print("failed: migration 074 has not run; run scripts/migrate.py first")
            return 1
        retention = ProjectionArchiveRetention(
            engine, event_reader=_CatalogEvents(EventCatalogRepository(engine))
        )
        report = retention.run(dry_run=args.dry_run)
    finally:
        engine.dispose()
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if report.outcome == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
