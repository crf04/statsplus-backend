#!/usr/bin/env python3
"""Backfill 2025-26 closing NBA points lines from The Odds API (#433).

Buys, for each game of the season, the ``player_points`` snapshot at the last
5-minute mark before tip (10 credits, region ``us``), stores one line per
player with its source book, and prints a coverage report.  Idempotent and
resumable: every response is recorded before it is parsed, and a game that is
recorded is never bought again, so a rerun spends 0 credits on it.

  --dry-run         no call is made and nothing is written: prints the plan
                    and the estimated credits
  --spot-check N    buy only N games spread over the season, then the report
  --max-credits     hard cap on what the whole backfill spends, earlier runs
                    included (default 15000)
  --keep-remaining  stop before a call that would take the account's
                    x-requests-remaining below this (default 5000), so the live
                    odds collector keeps its credits for the billing cycle
  --re-resolve      buy nothing: re-read the quotes kept on the ledger and
                    rewrite each game's lines with the current name resolver
                    (a resolver fix reaches games already bought).  Makes no
                    API call, needs no ODDS_API_KEY, spends no credits, prints
                    the coverage before and after, and a second run changes
                    nothing.  It reads the governed athlete mappings and
                    records none; with --dry-run it writes nothing at all and
                    prints the coverage the real run would reach.  Nothing is
                    bought, so --spot-check is refused and --max-credits and
                    --keep-remaining are unused.

Needs ``ODDS_API_KEY`` in the environment (not for ``--dry-run`` or ``--re-resolve``) and a
PostgreSQL database that has migration 067.  ``DATABASE_URL`` is read from the
environment unless ``--database-url`` is given.  Prints counts, costs and
times only: never the key, a URL or the database address.

The script builds only the database readers it needs.  It does not compose the
application's dependency container, which would register publication streams
and start the refresh-job dispatcher.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings  # noqa: E402
from app.providers.odds_api import SPORTSBOOK_REGION, OddsApiClient  # noqa: E402
from app.services.athlete_catalog_service import AthleteCatalogReader  # noqa: E402
from app.services.athlete_mapping_errors import AthleteMappingPersistenceError  # noqa: E402
from app.services.athlete_mapping_repository import AthleteMappingRepository  # noqa: E402
from app.services.closing_line_backfill import (  # noqa: E402
    SEASON,
    BoxScoreNames,
    ClosingLineBackfill,
    ClosingLineReResolve,
    SettledSeasonCatalog,
    format_coverage,
    format_plan,
    format_re_resolve,
    format_summary,
)
from app.services.closing_lines import (  # noqa: E402
    ClosingLineStore,
    RunRefused,
    coverage_report,
)
from app.services.event_catalog_repository import EventCatalogRepository  # noqa: E402
from app.services.event_mapping_repository import EventMappingRepository  # noqa: E402
from app.services.event_resolver import EventResolver  # noqa: E402
from app.services.odds_identity import OddsEventMapper  # noqa: E402
from app.utils.db import get_engine, is_demo_database_url  # noqa: E402

#: Stops that are the guards doing their job, not a fault.
EXPECTED_STOPS = {None, "max_credits", "keep_remaining"}


class _NoCalls:
    """The client of a dry run: any call is a bug, so none can be made."""

    regions = SPORTSBOOK_REGION

    def __getattr__(self, name: str):
        raise RuntimeError(f"a dry run makes no Odds API call ({name})")


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--spot-check", type=_positive, metavar="N")
    parser.add_argument("--max-credits", type=_positive, default=15000)
    parser.add_argument("--keep-remaining", type=_non_negative, default=5000)
    parser.add_argument("--re-resolve", action="store_true")
    args = parser.parse_args(argv)
    if args.re_resolve and args.spot_check is not None:
        parser.error("--re-resolve buys no game, so --spot-check does not apply")
    if not args.database_url.strip():
        parser.error("--database-url or DATABASE_URL is required")
    if is_demo_database_url(args.database_url):
        parser.error("the read-only demo database cannot hold closing lines")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout
    )
    settings = load_settings(overrides={"DATABASE_URL": args.database_url})
    engine = get_engine(settings)
    store = ClosingLineStore(engine)
    if not store.ready():
        print("failed: migration 067 has not run; run scripts/migrate.py first")
        return 1
    event_catalog = EventCatalogRepository(engine)
    events = event_catalog.list_events(SEASON)

    if args.re_resolve:
        return _re_resolve(args, engine, store, events)

    if args.dry_run:
        backfill = ClosingLineBackfill(
            store=store,
            client=_NoCalls(),
            event_mapper=None,
            names=None,
            events=events,
            max_credits=args.max_credits,
            keep_remaining=args.keep_remaining,
        )
        plan = backfill.plan(args.spot_check)
        for line in format_plan(plan, dry_run=True, max_credits=args.max_credits):
            print(line)
        _print_coverage(store, engine)
        return 0

    odds = settings.odds_api
    if not odds.api_key:
        print("failed: ODDS_API_KEY is not set")
        return 1
    event_mappings = EventMappingRepository(engine, settings=settings)
    backfill = ClosingLineBackfill(
        store=store,
        client=OddsApiClient(
            odds.api_key,
            base_url=odds.base_url,
            timeout=odds.request_timeout_seconds,
            regions=SPORTSBOOK_REGION,
        ),
        event_mapper=OddsEventMapper(
            resolver=EventResolver(
                SettledSeasonCatalog(event_catalog),
                mapping_repository=event_mappings,
                settings=settings,
            ),
            mapping_repository=event_mappings,
            season=SEASON,
        ),
        names=BoxScoreNames(
            engine=engine,
            catalog=AthleteCatalogReader(engine),
            mapping_repository=AthleteMappingRepository(engine),
        ),
        events=events,
        max_credits=args.max_credits,
        keep_remaining=args.keep_remaining,
    )
    plan = backfill.plan(args.spot_check)
    for line in format_plan(plan, dry_run=False, max_credits=args.max_credits):
        print(line)
    try:
        summary = backfill.run(spot_check=args.spot_check)
    except RunRefused as error:
        # The store refuses a paid run it cannot lock; its message is fixed text.
        print(f"failed: {error}")
        return 1
    for line in format_summary(summary):
        print(line)
    _print_coverage(store, engine)
    return 0 if summary.stop in EXPECTED_STOPS else 1


def _re_resolve(args, engine, store: ClosingLineStore, events) -> int:
    """Rewrite the lines from the stored quotes: no client, no key, no credits."""

    print("coverage before:")
    _print_coverage(store, engine)
    try:
        summary = ClosingLineReResolve(
            store=store,
            events=events,
            engine=engine,
            catalog=AthleteCatalogReader(engine),
            mappings=AthleteMappingRepository(engine),
        ).run(dry_run=args.dry_run)
    except RunRefused as error:
        print(f"failed: {error}")
        return 1
    except AthleteMappingPersistenceError:
        # Fixed text: the lines already written stand, and a rerun does the rest.
        print("failed: the stored athlete mappings could not be read; rerun to finish")
        return 1
    for line in format_re_resolve(summary, dry_run=args.dry_run):
        print(line)
    if summary.stop:
        return 1
    print("coverage projected:" if args.dry_run else "coverage after:")
    _print_coverage(store, engine, projected=summary.projected if args.dry_run else None)
    return 0


def _print_coverage(store: ClosingLineStore, engine, *, projected=None) -> None:
    report = coverage_report(engine, SEASON, projected=projected)
    for line in format_coverage(report, store.credits_recorded()):
        print(line)


if __name__ == "__main__":
    raise SystemExit(main())
