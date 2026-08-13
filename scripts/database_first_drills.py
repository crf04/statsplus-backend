#!/usr/bin/env python3
"""Run deterministic failure and isolated restore/replay drills."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
        help="configured Postgres DATABASE_URL; SQLite is allowed only with --sqlite-unit",
    )
    parser.add_argument(
        "--sqlite-unit",
        action="store_true",
        help="run the explicit local SQLite adapter drill (not production evidence)",
    )
    parser.add_argument(
        "--postgres-restore-command",
        help=(
            "operator-supplied command that performs a configured Postgres "
            "backup/restore and prints JSON evidence"
        ),
    )
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if str(args.database_url).startswith("sqlite") and not args.sqlite_unit:
        parser.error("SQLite drills require explicit --sqlite-unit")
    restore_adapter = None
    if args.postgres_restore_command:
        if args.sqlite_unit:
            parser.error("--postgres-restore-command cannot be combined with --sqlite-unit")

        def restore_adapter():
            completed = subprocess.run(
                args.postgres_restore_command,
                shell=True,
                check=True,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            output = completed.stdout.strip()
            if not output:
                raise ValueError("Postgres restore command returned no evidence")
            evidence = json.loads(output)
            if not isinstance(evidence, dict):
                raise ValueError("Postgres restore evidence must be an object")
            return evidence

    report = run_failure_drills(
        database_url=args.database_url,
        require_production_evidence=not args.sqlite_unit,
        restore_adapter=restore_adapter,
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
