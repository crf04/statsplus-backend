#!/usr/bin/env python3
"""Run exactly one sportsbook-odds collection beat (#377).

The Railway collector normally runs this beat inside
``scripts/projection_collection_service.py``; this entry point is for a
standalone cron service or an operator one-shot.  Requires
``ODDS_API_ENABLED=true`` and ``ODDS_API_KEY`` in the environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings  # noqa: E402
from app.dependencies import build_dependencies  # noqa: E402
from app.services import odds_collection  # noqa: E402
from app.utils.db import is_demo_database_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    if is_demo_database_url(args.database_url):
        parser.error("the read-only demo database cannot collect odds")
    settings = load_settings(overrides={"DATABASE_URL": args.database_url})
    try:
        coordinator = odds_collection.build_odds_collection(
            build_dependencies(settings), settings
        )
    except RuntimeError as error:
        print(f"failed: {error}")
        return 1
    if coordinator is None:
        print("no_work: ODDS_API_ENABLED is false")
        return 0
    result = odds_collection.run_once(coordinator)
    print(
        f"{result.status}: {result.reason} tier={result.tier} calls={result.calls} "
        f"credits={result.credits} unresolved_events={result.unresolved_events} "
        f"unresolved_players={result.unresolved_players}"
    )
    return 0 if result.status in {"complete", "partial", "no_work", "busy", "backoff"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
