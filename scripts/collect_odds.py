#!/usr/bin/env python3
"""Run exactly one sportsbook-odds collection beat (#377).

This is the entry point of its own Railway cron service (``railway.odds.json``,
every five minutes), deliberately not part of the projection collector: a
slow Odds API must never delay a DFS beat.  Requires ``ODDS_API_ENABLED=true``
and ``ODDS_API_KEY`` in the environment; ``DATABASE_URL`` is read from the
environment unless ``--database-url`` is given.  When ``SENTRY_DSN`` is set,
Sentry is initialized as in the web app so the guard's alerts are delivered.

After the odds beat, whatever its outcome, each run takes two side steps in
this order, each in its own guard so one failing never skips the other or
changes the exit code, and each abandoned after ``SIDE_STEP_SECONDS``: it
reads ESPN's scoreboard once and stores the day's game lines
(crf04/statsplus#493), then stores NBA.com's daily lineups
(crf04/statsplus#492).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings
from app.dependencies import build_dependencies
from app.services import game_lines, odds_collection, team_lineups
from app.utils.db import is_demo_database_url
from app.utils.json_logging import configure_json_logging
from app.utils.sentry_setup import init_sentry

#: Wall-clock bound on each side step after the odds beat, whatever it waits on.
SIDE_STEP_SECONDS = 30.0


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
        # Cron logs show the cycle's INFO lines (tier, calls, remaining=...),
        # in the same JSON format the web app writes.
        configure_json_logging(settings.log_level)
        dependencies = build_dependencies(settings)
        try:
            return _odds_beat(dependencies, settings)
        finally:
            collect_game_lines(dependencies)
            collect_lineups(dependencies)
    finally:
        _flush_sentry()


def _odds_beat(dependencies, settings) -> int:
    try:
        coordinator = odds_collection.build_odds_collection(dependencies, settings)
    except RuntimeError as error:
        print(f"failed: {error}")
        return 1
    if coordinator is None:
        print("no_work: ODDS_API_ENABLED is false")
        return 0
    result = odds_collection.run_once(coordinator)
    print(
        f"{result.status}: {result.reason} tier={result.tier} calls={result.calls} "
        f"credits={result.credits} remaining={result.remaining} unresolved_events={result.unresolved_events} "
        f"unresolved_players={result.unresolved_players}"
    )
    return 0 if result.status in {"complete", "partial", "no_work", "busy", "backoff"} else 1


def collect_game_lines(dependencies) -> None:
    """Store today's ESPN game lines; a failure is printed and leaves the odds beat's exit code alone."""

    _side_step("game_lines", lambda: game_lines.collect(dependencies.engine))


def collect_lineups(dependencies) -> None:
    """Store today's NBA.com lineups; a failure is printed and leaves the odds beat's exit code alone."""

    _side_step("lineups", lambda: team_lineups.collect_team_lineups(dependencies.engine))


def _side_step(name: str, run) -> None:
    """Run one side step for at most ``SIDE_STEP_SECONDS`` and print its summary or why it failed.

    The step runs on a daemon thread, abandoned rather than joined when it
    overruns, so a step blocked on a download or a database lock never holds
    up the next step or the end of the run.
    """

    outcome: dict = {}

    def target() -> None:
        try:
            outcome["summary"] = run()
        except Exception as error:
            outcome["error"] = error

    try:
        worker = threading.Thread(target=target, name=name, daemon=True)
        worker.start()
        worker.join(SIDE_STEP_SECONDS)
    except Exception as error:
        print(f"{name} failed: {type(error).__name__}: {error}")
        return
    if worker.is_alive():
        print(f"{name} failed: TimeoutError: still running after {SIDE_STEP_SECONDS} s")
    elif "error" in outcome:
        print(f"{name} failed: {type(outcome['error']).__name__}: {outcome['error']}")
    else:
        print(f"{name}: {json.dumps(outcome['summary'], sort_keys=True)}")


if __name__ == "__main__":
    raise SystemExit(main())
