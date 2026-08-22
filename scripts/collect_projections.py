#!/usr/bin/env python3
"""Run exactly one scheduled projection-collection coordinator attempt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings  # noqa: E402
from app.dependencies import build_dependencies  # noqa: E402
from app.utils.db import is_demo_database_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--season")
    args = parser.parse_args(argv)

    if is_demo_database_url(args.database_url):
        parser.error("the read-only demo database cannot collect projections")
    settings = load_settings(overrides={"DATABASE_URL": args.database_url})
    if args.season is not None and args.season != settings.nba.current_season:
        parser.error("--season must match the configured current NBA season")
    dependencies = build_dependencies(settings)
    coordinator = dependencies.projection_collection_coordinator
    if coordinator is None:
        print("no_work: projection collection is unavailable")
        return 0
    result = coordinator.run()
    print(f"{result.status}: {result.reason}")
    return 0 if result.status in {"complete", "partial", "no_work", "busy"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
