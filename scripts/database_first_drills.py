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
    parser.add_argument(
        "--marker-nonce",
        default=os.environ.get("STATPLUS_DISPOSABLE_MARKER_NONCE"),
        help="nonce from the out-of-band statsplus_disposable_control marker",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("STATPLUS_DISPOSABLE_SCHEMA"),
        help="dedicated disposable Postgres schema name",
    )
    parser.add_argument(
        "--restored-marker-nonce",
        default=os.environ.get("STATPLUS_RESTORED_MARKER_NONCE"),
        help="nonce from the restored database's out-of-band marker",
    )
    parser.add_argument(
        "--restored-schema",
        default=os.environ.get("STATPLUS_RESTORED_SCHEMA"),
        help="dedicated schema containing the restored control plane",
    )
    parser.add_argument(
        "--restore-expectations",
        type=Path,
        help="JSON expected IDs/checksums for the restored database",
    )
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if args.sqlite_unit and not str(args.database_url).startswith("sqlite"):
        parser.error("--sqlite-unit requires a SQLite drill database")
    if str(args.database_url).startswith("sqlite") and not args.sqlite_unit:
        parser.error("SQLite drills require explicit --sqlite-unit")
    if not args.marker_nonce:
        parser.error("--marker-nonce or STATPLUS_DISPOSABLE_MARKER_NONCE is required")
    if not args.sqlite_unit and not args.restored_database_url:
        parser.error("operator drills require --restored-database-url")
    if not args.sqlite_unit and not args.restored_marker_nonce:
        parser.error("--restored-marker-nonce is required for operator restore evidence")
    expectations = None
    if args.restore_expectations:
        expectations = json.loads(args.restore_expectations.read_text(encoding="utf-8"))
        if not isinstance(expectations, dict):
            parser.error("--restore-expectations must contain a JSON object")

    report = run_failure_drills(
        database_url=args.database_url,
        environment="unit" if args.sqlite_unit else "operator",
        isolated=False,
        production_database_url=args.production_database_url,
        restored_database_url=args.restored_database_url,
        disposable_marker_nonce=args.marker_nonce,
        disposable_schema=args.schema,
        restored_marker_nonce=args.restored_marker_nonce,
        restored_schema=args.restored_schema,
        restore_expectations=expectations,
        require_production_evidence=not args.sqlite_unit,
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
