#!/usr/bin/env python3
"""Run deterministic failure and isolated restore/replay drills."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.database_first_drills import run_failure_drills


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="disposable isolated drill database URL",
    )
    parser.add_argument(
        "--sqlite-unit",
        action="store_true",
        help="run the explicit local SQLite adapter drill (not production evidence)",
    )
    parser.add_argument(
        "--production-database-url",
        help=(
            "production/control URL used only to reject accidental same-database drills"
        ),
    )
    parser.add_argument(
        "--restored-database-url",
        help="disposable isolated Postgres URL containing the completed restore",
    )
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if args.sqlite_unit and not str(args.database_url).startswith("sqlite"):
        parser.error("--sqlite-unit requires a SQLite drill database")
    if str(args.database_url).startswith("sqlite") and not args.sqlite_unit:
        parser.error("SQLite drills require explicit --sqlite-unit")
    if not args.sqlite_unit and not args.restored_database_url:
        parser.error("operator drills require --restored-database-url")

    report = run_failure_drills(
        database_url=args.database_url,
        environment="unit" if args.sqlite_unit else "operator",
        isolated=True,
        production_database_url=args.production_database_url,
        restored_database_url=args.restored_database_url,
        require_production_evidence=not args.sqlite_unit,
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
