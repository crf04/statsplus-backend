#!/usr/bin/env python3
"""Run exactly one sportsbook-odds collection beat (#377).

This is the entry point of its own Railway cron service (``railway.odds.json``,
every five minutes), deliberately not part of the projection collector: a
slow Odds API must never delay a DFS beat.  Requires ``ODDS_API_ENABLED=true``
and ``ODDS_API_KEY`` in the environment; ``DATABASE_URL`` is read from the
environment unless ``--database-url`` is given.  When ``SENTRY_DSN`` is set,
Sentry is initialized as in the web app so the guard's alerts are delivered.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings  # noqa: E402
from app.dependencies import build_dependencies  # noqa: E402
from app.services import odds_collection  # noqa: E402
from app.utils.db import is_demo_database_url  # noqa: E402
from app.utils.sentry_setup import init_sentry  # noqa: E402


def _flush_sentry() -> None:
    import sentry_sdk

    if sentry_sdk.get_client().is_active():
        sentry_sdk.flush(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("--database-url or DATABASE_URL is required")
    if is_demo_database_url(args.database_url):
        parser.error("the read-only demo database cannot collect odds")
    init_sentry()
    try:
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
    finally:
        _flush_sentry()


if __name__ == "__main__":
    raise SystemExit(main())
