"""Store each game's last official injury report before tip-off (a one-off backfill).

For every game of ``--season`` in the Event Catalog that has tipped and was not
postponed, find the newest official NBA injury report whose own header time is
at or before the game's scheduled tip, and store it as that game's snapshot,
through the path the live refresh uses.  A game that already holds a snapshot is
skipped, so a report the live refresh captured always wins and a second run
stores nothing.  Each report file is requested once per run.

``--from`` / ``--to`` bound the games by their US Eastern tip date;
``--dry-run`` computes and prints everything and writes nothing.  Reads
``DATABASE_URL`` from the environment unless ``--database-url`` is given (an
operator's laptop cannot reach Railway's private host; see
docs/ARCHITECTURE.md, "Matchup injury snapshots").  Prints one JSON summary
line; exits 1 when it stopped after 3 network failures in a row.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from app.domain.nba_events import (  # noqa: E402
    is_all_star_kind,
    is_postponed_event,
    is_preseason_kind,
    resolve_stored_event_classification,
)
from app.domain.utc import assume_utc, parse_utc_iso  # noqa: E402
from app.providers.nba_injury_report import (  # noqa: E402
    EASTERN,
    FetchedReport,
    ReportFileCache,
    UnreadableReportError,
    snapshot_from_report,
)
from app.services.injury_snapshot_repository import InjurySnapshotScope  # noqa: E402

#: Report names walked back from a tip.  The league publishes at least hourly
#: (every 30 minutes later in the season) and each hour has up to 5 names, so
#: 60 names reach ~12 hours back.
MAX_NAMES_PER_GAME = 60
#: Seconds between requests, whatever the live provider is configured to use.
MIN_PROBE_DELAY_SECONDS = 0.5
MAX_CONSECUTIVE_NETWORK_FAILURES = 3


@dataclass(slots=True)
class BackfillSummary:
    dry_run: bool
    games_in_scope: int = 0
    games_stored: int = 0
    games_skipped_already_stored: int = 0
    games_no_report_found: int = 0
    games_parse_failed: int = 0
    games_network_failed: int = 0
    games_not_attempted: int = 0
    report_files_downloaded: int = 0
    requests_made: int = 0
    parse_failures: list[str] = field(default_factory=list)
    stopped_after_network_failures: bool = False

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        scope = self.games_in_scope - self.games_skipped_already_stored
        for key, count in (
            ("no_report_found_share", self.games_no_report_found),
            ("parse_failed_share", self.games_parse_failed),
        ):
            document[key] = round(count / scope, 4) if scope else 0.0
        return document


def games_to_backfill(
    events: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Mapping[str, Any]]:
    """Tipped, not postponed, regular season / play-in / playoffs, inside the Eastern date range; in tip order."""

    kept = []
    for event in events:
        tip = parse_utc_iso(str(event["scheduled_at"]))
        if tip > now or is_postponed_event(event):
            continue
        kind = resolve_stored_event_classification(
            str(event.get("nba_game_id", "")), str(event.get("classification") or "")
        ).kind
        if is_all_star_kind(kind) or is_preseason_kind(kind):
            continue
        tip_day = tip.astimezone(EASTERN).date()
        if (date_from and tip_day < date_from) or (date_to and tip_day > date_to):
            continue
        kept.append(event)
    kept.sort(key=lambda event: (str(event["scheduled_at"]), str(event["nba_game_id"])))
    return kept


def run_backfill(
    *,
    season: str,
    events: Sequence[Mapping[str, Any]],
    provider: Any,
    injuries: Any,
    now: datetime,
    dry_run: bool,
    date_from: date | None = None,
    date_to: date | None = None,
    log: TextIO | None = None,
) -> BackfillSummary:
    """Choose each game's report, then store them one report at a time."""

    log = log or sys.stderr
    now = assume_utc(now)
    summary = BackfillSummary(dry_run=dry_run)
    games = games_to_backfill(events, now=now, date_from=date_from, date_to=date_to)
    summary.games_in_scope = len(games)
    scopes = {
        str(game["nba_game_id"]): InjurySnapshotScope(season, str(game["nba_game_id"]))
        for game in games
    }
    already = injuries.snapshot_repository.stored_scopes(tuple(scopes.values()))
    todo = [game for game in games if scopes[str(game["nba_game_id"])] not in already]
    summary.games_skipped_already_stored = len(games) - len(todo)

    cache = ReportFileCache()
    delay = max(
        MIN_PROBE_DELAY_SECONDS,
        provider.settings.providers.injury_report_probe_delay_seconds,
    )
    # Games sharing a tip slot share one search; its outcome is a FetchedReport,
    # None (no report found) or the URL of the unreadable file that ended it.
    by_tip: dict[str, FetchedReport | str | None] = {}
    chosen: dict[str, tuple[FetchedReport, list[Mapping[str, Any]]]] = {}
    unreadable: set[str] = set()
    for index, game in enumerate(todo):
        tip_key = str(game["scheduled_at"])
        if tip_key not in by_tip:
            try:
                by_tip[tip_key] = provider.report_as_of(
                    parse_utc_iso(tip_key),
                    cache=cache,
                    max_requests=MAX_NAMES_PER_GAME,
                    probe_delay_seconds=delay,
                )
            except UnreadableReportError as error:
                by_tip[tip_key] = error.url
                if error.url not in unreadable:
                    unreadable.add(error.url)
                    summary.parse_failures.append(error.url)
                    print(f"unreadable layout: {error.url}", file=log, flush=True)
            except requests.RequestException as error:
                summary.games_network_failed += 1
                print(
                    f"network failure ({cache.consecutive_failures} in a row) "
                    f"for game {game['nba_game_id']}: {type(error).__name__}",
                    file=log,
                    flush=True,
                )
                if cache.consecutive_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                    summary.stopped_after_network_failures = True
                    summary.games_not_attempted = len(todo) - index - 1
                    break
                continue
        found = by_tip[tip_key]
        if found is None:
            summary.games_no_report_found += 1
        elif isinstance(found, str):
            summary.games_parse_failed += 1
        else:
            chosen.setdefault(found.url, (found, []))[1].append(game)
            summary.games_stored += 1
    summary.report_files_downloaded = cache.downloaded
    summary.requests_made = cache.requests

    if not dry_run:
        for fetched, report_games in chosen.values():
            injuries.store_past_report(
                snapshot_from_report(fetched.report, fetched.url, now),
                events=report_games,
                season=season,
            )
    return summary


def _eastern_date(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from None
    return parsed


def main(argv: Sequence[str] | None = None, *, clock: Callable[[], datetime] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", required=True, help="canonical season, e.g. 2025-26")
    parser.add_argument("--from", dest="date_from", type=_eastern_date, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=_eastern_date, metavar="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("--database-url or DATABASE_URL is required")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--from must not be after --to")

    from app.config.settings import load_settings
    from app.dependencies import build_dependencies
    from app.providers.nba_stats import validate_canonical_season
    from app.utils.db import is_demo_database_url
    from app.utils.json_logging import configure_json_logging

    if is_demo_database_url(args.database_url):
        parser.error("the read-only demo database cannot hold injury snapshots")
    try:
        season = validate_canonical_season(args.season)
    except ValueError as error:
        parser.error(str(error))

    settings = load_settings(
        overrides={"DATABASE_URL": args.database_url, "INJURY_REPORT_ENABLED": "true"}
    )
    configure_json_logging(settings.log_level)
    dependencies = build_dependencies(settings)
    try:
        injuries = dependencies.injury_refresh_service.injuries
        if injuries.provider is None or injuries.snapshot_repository is None:
            print(json.dumps({"state": "injury_service_unavailable"}), flush=True)
            return 1
        summary = run_backfill(
            season=season,
            events=dependencies.event_catalog_service.get_events(season),
            provider=injuries.provider,
            injuries=injuries,
            now=(clock or (lambda: datetime.now(timezone.utc)))(),
            dry_run=args.dry_run,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    finally:
        dependencies.engine.dispose()
    print(json.dumps(summary.to_document(), sort_keys=True), flush=True)
    return 1 if summary.stopped_after_network_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
