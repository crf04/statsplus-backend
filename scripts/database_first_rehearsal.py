#!/usr/bin/env python3
"""Run the isolated seven-date database-first Historical Rehearsal."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.database_first_rehearsal import (
    DEFAULT_REHEARSAL_DATES,
    HistoricalRehearsalRunner,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--production-database-url",
        help="optional read-only production/control-plane URL whose pointers must remain unchanged",
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=("historical_rehearsal", "testing"),
        help="must point at an isolated non-production control plane",
    )
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--cutoff", action="append", dest="cutoffs")
    parser.add_argument(
        "--collect-command",
        required=True,
        help="command template returning JSON; supports {season}, {cutoff}, and {database_url}",
    )
    parser.add_argument(
        "--synergy-command",
        required=True,
        help="completed-season Synergy command returning true or JSON status=passed",
    )
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    isolated_engine = create_engine(args.database_url)
    run_migrations(isolated_engine)
    production_engine = (
        create_engine(args.production_database_url)
        if args.production_database_url
        else isolated_engine
    )
    cutoffs = (
        tuple(date.fromisoformat(value) for value in args.cutoffs)
        if args.cutoffs
        else DEFAULT_REHEARSAL_DATES
    )
    def run_command(template: str, cutoff: date) -> object:
        command = template.format(
            season=args.season,
            cutoff=cutoff.isoformat(),
            database_url=args.database_url,
        )
        completed = subprocess.run(
            command,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "STATSPLUS_REHEARSAL_SEASON": args.season},
        )
        output = completed.stdout.strip()
        if not output:
            raise ValueError("rehearsal command returned no JSON output")
        return json.loads(output)

    def collect(cutoff: date) -> object:
        return run_command(args.collect_command, cutoff)

    def synergy(cutoff: date) -> object:
        result = run_command(args.synergy_command, cutoff)
        return result

    report = HistoricalRehearsalRunner(
        production_engine,
        environment=args.environment,
        isolated_engine=isolated_engine,
    ).run(
        args.season,
        cutoffs=cutoffs,
        collect=collect,
        synergy_check=synergy,
    )
    report.write(args.report)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
