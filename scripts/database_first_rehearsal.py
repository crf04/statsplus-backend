#!/usr/bin/env python3
"""Run the isolated seven-date database-first Historical Rehearsal."""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--cutoff", action="append", dest="cutoffs")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    engine = create_engine(args.database_url)
    run_migrations(engine)
    cutoffs = (
        tuple(date.fromisoformat(value) for value in args.cutoffs)
        if args.cutoffs
        else DEFAULT_REHEARSAL_DATES
    )
    report = HistoricalRehearsalRunner(engine).run(args.season, cutoffs=cutoffs)
    report.write(args.report)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
