"""Backtest the NBA official injury-report adapter over a finished season.

For every Regular Season game date this finds the last report published at or
before that date's first tip-off, parses it with the production parser, and
scores five checks (statsplus#171): coverage, parse completeness, identity,
and the box-score ground truth for each reported status, overall and per
month.  Reports are downloaded once into ``--cache-dir`` (misses are cached
too), and the database connection is read-only, so a rerun is idempotent.

    python scripts/injury_report_backtest.py --season 2025-26 \\
        --database-url "$DATABASE_URL" --cache-dir .cache/injury-reports \\
        --output report.json

Every parsed row must be a game in the Event Catalog or a listed exception
(``KNOWN_GAMES_NOT_IN_CATALOG``, extended with ``--allow-game DATE:AWAY@HOME``);
identity is scored over every parsed row.

Exit status: 0 when every threshold passes, 1 when one fails, 2 when an input
could not be read (database, publication, ``pdftotext``).
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from app.domain.nba_events import (  # noqa: E402
    REGULAR_SEASON_TYPE,
    player_game_log_season_type,
)
from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID, NBA_TEAM_TRICODES  # noqa: E402
from app.domain.utc import parse_utc_iso  # noqa: E402
from app.providers.nba_injury_report import (  # noqa: E402
    EASTERN,
    REPORT_BASE_URL,
    REPORT_STATUSES,
    USER_AGENT,
    ParsedInjuryReport,
    candidate_report_names,
    entries_from_report,
    entry_id_for,
    parse_injury_report,
)
from app.services.matchup_injuries import (  # noqa: E402
    AthleteNameIndex,
    reconcile_injury_entries,
)
from scripts.season_verify import open_read_only_engine  # noqa: E402

MIN_IDENTITY_RATE = 0.99
MIN_OUT_PRECISION = 0.98
DEFAULT_MAX_PROBES = 48
#: Real 2025-26 games the Event Catalog deliberately lacks: the NBA Cup final
#: (Cup knockouts are excluded from the catalog) and three postponed games.
#: Reports list them; they are not graded.  Anything else absent from the
#: catalog fails completeness.  Extend per run with ``--allow-game``.
KNOWN_GAMES_NOT_IN_CATALOG = frozenset(
    {
        (date(2025, 12, 16), "SAS@NYK"),
        (date(2026, 1, 8), "MIA@CHI"),
        (date(2026, 1, 24), "GSW@MIN"),
        (date(2026, 1, 25), "DAL@MIL"),
    }
)
DEFAULT_DELAY_SECONDS = 0.3
#: Statuses whose play rate the report states (Out is scored as precision).
PLAY_RATE_STATUSES = ("Probable", "Available", "Questionable", "Doubtful")
_MATCHUP_ARG = re.compile(r"^[A-Z]{3}@[A-Z]{3}$")
_STATUS_CELL = re.compile(r"(?:^|\s{2,})(?:" + "|".join(sorted(REPORT_STATUSES)) + r")(?=\s{2,}|\s*$)")


class BacktestInputError(RuntimeError):
    """An input the backtest needs could not be read."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonInputs:
    events: tuple[Mapping[str, Any], ...]
    athletes: tuple[Mapping[str, Any], ...]
    #: ``PlayerGameLogRecord`` rows of the published season stream.
    logs: tuple[Any, ...]


def load_inputs(engine: Any, season: str, *, publication_reader: Any | None = None) -> SeasonInputs:
    """Read the Event Catalog, Athlete Catalog and published game logs."""

    from app.services.athlete_catalog_service import AthleteCatalogReader
    from app.services.database_first_activation import (
        DatabaseFirstPublicationReader,
        decode_player_game_logs,
    )
    from app.services.event_catalog_repository import EventCatalogRepository

    events = EventCatalogRepository(engine).list_events(season)
    athletes = AthleteCatalogReader(engine).get_catalog(season)
    reader = publication_reader or DatabaseFirstPublicationReader(engine)
    read = reader.read("player_game_logs", season=season)
    if read.legacy_fallback_allowed or not read.available:
        raise BacktestInputError(
            f"no active published player_game_logs for {season} " f"(status={read.status})"
        )
    logs = (
        tuple(read.decoded)
        if read.decoded is not None
        else decode_player_game_logs(read.payload, season=season)
    )
    if not events:
        raise BacktestInputError(f"the Event Catalog has no {season} events")
    if not athletes:
        raise BacktestInputError(f"the Athlete Catalog has no {season} athletes")
    return SeasonInputs(tuple(events), tuple(athletes), tuple(logs))


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------


class ReportFetcher:
    """Cached, throttled report downloads; a 403/404 is cached as a miss."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_dir = cache_dir
        self.delay_seconds = delay_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.sleep = sleep
        self.downloads = 0
        self.cache_hits = 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, name: str) -> bytes | None:
        hit, miss = self.cache_dir / name, self.cache_dir / f"{name}.missing"
        if hit.exists():
            self.cache_hits += 1
            return hit.read_bytes()
        if miss.exists():
            self.cache_hits += 1
            return None
        self.sleep(self.delay_seconds)
        response = self.session.get(REPORT_BASE_URL + name, timeout=(5, 30))
        self.downloads += 1
        if response.status_code in (403, 404):
            miss.write_bytes(b"")
            return None
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            raise BacktestInputError(f"{name} is not a PDF")
        hit.write_bytes(response.content)
        return response.content


def find_last_report(
    fetcher: ReportFetcher, tip: datetime, *, max_probes: int
) -> tuple[str, bytes, ParsedInjuryReport] | None:
    """The newest report whose own header time is at or before ``tip``.

    The file name only bounds the search: a report can be stamped later than
    its name (``05PM`` printing "05:30 PM"), and one stamped after ``tip``
    carries information from after it, so it is skipped.
    """

    for name in candidate_report_names(tip, max_requests=max_probes):
        content = fetcher.get(name)
        if content is None:
            continue
        report = parse_injury_report(content)
        if report.report_time is not None and report.report_time <= tip:
            return name, content, report
    return None


def pdftotext_status_cells(content: bytes) -> int:
    """Independent count of status cells, from ``pdftotext -layout`` text."""

    binary = shutil.which("pdftotext")
    if binary is None:
        raise BacktestInputError("pdftotext (poppler) is required for check 2")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "report.pdf"
        path.write_bytes(content)
        result = subprocess.run(
            [binary, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    if result.returncode != 0:
        raise BacktestInputError("pdftotext could not read a report")
    return sum(1 for line in result.stdout.splitlines() if _STATUS_CELL.search(line))


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@dataclass
class _Tally:
    dates: int = 0
    covered: int = 0
    reports_complete: int = 0
    identity_rows: int = 0
    identity_matched: int = 0
    graded: Counter = field(default_factory=Counter)
    played: Counter = field(default_factory=Counter)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "dates": self.dates,
            "covered": self.covered,
            "reports_complete": self.reports_complete,
            "identity_rows": self.identity_rows,
            "identity_matched": self.identity_matched,
            "identity_rate": _rate(self.identity_matched, self.identity_rows),
            "statuses": {},
        }
        for status in ("Out", *PLAY_RATE_STATUSES):
            graded, played = self.graded[status], self.played[status]
            entry = {"graded": graded, "played": played}
            if status == "Out":
                entry["precision"] = _rate(graded - played, graded)
            else:
                entry["play_rate"] = _rate(played, graded)
            document["statuses"][status] = entry
        return document


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _fraction(numerator: int, denominator: int) -> float | None:
    """Unrounded, so a threshold is never met by rounding up."""

    return numerator / denominator if denominator else None


def _eastern_date(scheduled_at: str) -> date:
    return parse_utc_iso(scheduled_at).astimezone(EASTERN).date()


def regular_season_dates(
    events: Iterable[Mapping[str, Any]],
) -> dict[date, list[Mapping[str, Any]]]:
    """Regular Season events by Eastern game date, in schedule order."""

    by_date: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if player_game_log_season_type(event) != REGULAR_SEASON_TYPE:
            continue
        by_date[_eastern_date(str(event["scheduled_at"]))].append(event)
    return dict(sorted(by_date.items()))


def evaluate(
    inputs: SeasonInputs,
    fetcher: ReportFetcher,
    *,
    max_probes: int = DEFAULT_MAX_PROBES,
    count_status_cells: Callable[[bytes], int] = pdftotext_status_cells,
    min_identity: float = MIN_IDENTITY_RATE,
    min_out_precision: float = MIN_OUT_PRECISION,
    allowed_games: Iterable[tuple[date, str]] = KNOWN_GAMES_NOT_IN_CATALOG,
) -> dict[str, Any]:
    allowed = frozenset(allowed_games)
    dates = regular_season_dates(inputs.events)
    event_by_key = {
        (
            _eastern_date(str(event["scheduled_at"])),
            event["away_team_tricode"],
            event["home_team_tricode"],
        ): event
        for events in dates.values()
        for event in events
    }
    played_minutes: dict[tuple[str, int], float] = {}
    games_with_logs: set[str] = set()
    for record in inputs.logs:
        games_with_logs.add(str(record.game_id))
        played_minutes[(str(record.game_id), int(record.player_id))] = float(record.minutes)
    # Every row of the season, active or not: the catalog is an end-of-season
    # snapshot here, so players who left mid-season are inactive in it, while
    # the live catalog refreshes daily and has no such gap.
    index = AthleteNameIndex(list(inputs.athletes))
    catalog_games = {
        (
            _eastern_date(str(event["scheduled_at"])),
            f"{event['away_team_tricode']}@{event['home_team_tricode']}",
        )
        for event in inputs.events
    }
    teams = {code: {"team_id": team_id} for code, team_id in NBA_TEAM_TRICODE_TO_ID.items()}

    overall = _Tally()
    months: dict[str, _Tally] = defaultdict(_Tally)
    coverage_failures: list[dict[str, Any]] = []
    completeness_failures: list[dict[str, Any]] = []
    unmatched: Counter = Counter()
    games_not_in_catalog: Counter = Counter()
    identity_seen: set[str] = set()
    out_played: list[dict[str, Any]] = []
    ungraded_games: set[str] = set()
    per_date: list[dict[str, Any]] = []

    for game_date, events in dates.items():
        month = months[f"{game_date:%Y-%m}"]
        tallies = (overall, month)
        for tally in tallies:
            tally.dates += 1
        first_tip = min(parse_utc_iso(str(event["scheduled_at"])) for event in events)
        record: dict[str, Any] = {
            "date": game_date.isoformat(),
            "first_tip_utc": first_tip.astimezone(timezone.utc).isoformat(),
        }
        per_date.append(record)
        try:
            found = find_last_report(fetcher, first_tip, max_probes=max_probes)
        except Exception as error:  # noqa: BLE001 - any parse failure is a finding
            record["report"] = None
            coverage_failures.append({**record, "reason": f"parse error: {error}"})
            continue
        if found is None:
            record["report"] = None
            coverage_failures.append({**record, "reason": "no report found"})
            continue
        name, content, report = found
        record["report"] = name
        record["report_time"] = report.report_time.isoformat()
        for tally in tallies:
            tally.covered += 1

        problems = _completeness_problems(report, content, count_status_cells, catalog_games, allowed)
        record["rows"] = len(report.rows)
        if problems:
            completeness_failures.append({**record, "problems": problems})
        else:
            for tally in tallies:
                tally.reports_complete += 1

        # Identity covers every parsed row, whatever its date or game; a row
        # is counted once however many selected reports repeat it.
        entries = entries_from_report(report, source_url=REPORT_BASE_URL + name)
        reconciled, _ = reconcile_injury_entries(entries, teams=teams, catalog=index)
        resolved = {row["entry_id"]: row for row in reconciled}
        for row in report.rows:
            entry_id = entry_id_for(row)
            if row.game_date == game_date and (game_date, row.matchup) not in catalog_games:
                games_not_in_catalog[(game_date.isoformat(), row.matchup)] += 1
            reconciled_row = resolved.get(entry_id)
            player_id = None if reconciled_row is None else reconciled_row["canonical_player_id"]
            if entry_id not in identity_seen:
                identity_seen.add(entry_id)  # one player, one game: counted once
                for tally in tallies:
                    tally.identity_rows += 1
                    tally.identity_matched += player_id is not None
                if player_id is None:
                    unmatched[(row.display_name, row.team_tricode)] += 1
            if player_id is None:
                continue
            away, _, home = row.matchup.partition("@")
            # Looked up by this report's date: another date's rows, and games the
            # catalog lacks, find no event and are never graded here.
            event = event_by_key.get((game_date, away, home))
            if event is None or str(event["nba_game_id"]) not in games_with_logs:
                if event is not None:
                    ungraded_games.add(str(event["nba_game_id"]))
                continue
            game_id = str(event["nba_game_id"])
            minutes = played_minutes.get((game_id, int(player_id)), 0.0)
            played = minutes > 0
            for tally in tallies:
                tally.graded[row.status] += 1
                tally.played[row.status] += played
            if played and row.status == "Out":
                out_played.append(
                    {
                        "date": game_date.isoformat(),
                        "game_id": game_id,
                        "matchup": row.matchup,
                        "team": row.team_tricode,
                        "name": row.display_name,
                        "player_id": int(player_id),
                        "minutes": minutes,
                        "reason": row.reason,
                        "report": name,
                    }
                )

    document = {
        "dates": overall.to_document()["dates"],
        "overall": overall.to_document(),
        "by_month": {key: months[key].to_document() for key in sorted(months)},
        "downloads": {"network": fetcher.downloads, "cache_hits": fetcher.cache_hits},
        "coverage_failures": coverage_failures,
        "completeness_failures": completeness_failures,
        "unmatched": [
            {"name": name, "team": team, "rows": count}
            for (name, team), count in sorted(unmatched.items(), key=lambda item: (-item[1], item[0]))
        ],
        "unmatched_rows": sum(unmatched.values()),
        "games_not_in_catalog": [
            {"date": day, "matchup": matchup, "rows": count}
            for (day, matchup), count in sorted(games_not_in_catalog.items())
        ],
        "out_players_who_played": out_played,
        "games_without_logs": sorted(ungraded_games),
        "per_date": per_date,
    }
    document["thresholds"] = _thresholds(
        overall, min_identity=min_identity, min_out_precision=min_out_precision
    )
    document["passed"] = all(item["passed"] for item in document["thresholds"].values())
    return document


def _completeness_problems(
    report: ParsedInjuryReport,
    content: bytes,
    count_status_cells: Callable[[bytes], int],
    catalog_games: set[tuple[date, str]],
    allowed: frozenset[tuple[date, str]],
) -> list[str]:
    problems: list[str] = []
    expected = count_status_cells(content)
    if len(report.rows) != expected:
        problems.append(f"parsed {len(report.rows)} rows, pdftotext counts {expected}")
    if report.unparsed:
        problems.append(f"{len(report.unparsed)} unparsed lines: {list(report.unparsed)[:3]}")
    for row in report.rows:
        away, _, home = row.matchup.partition("@")
        where = f"{row.game_date} {row.matchup} {row.player_name}"
        if (row.game_date, row.matchup) not in catalog_games | allowed:
            problems.append(f"{where}: game is not in the Event Catalog or the allowed exceptions")
        if away not in NBA_TEAM_TRICODES or home not in NBA_TEAM_TRICODES:
            problems.append(f"{where}: matchup names a non-NBA team")
        if row.status not in REPORT_STATUSES:
            problems.append(f"{where}: invalid status {row.status!r}")
        if row.team_tricode not in {away, home}:
            problems.append(f"{where}: team {row.team_name!r} is not in the matchup")
    return problems


def _thresholds(
    overall: _Tally, *, min_identity: float, min_out_precision: float
) -> dict[str, dict[str, Any]]:
    graded_out = overall.graded["Out"]
    identity = _fraction(overall.identity_matched, overall.identity_rows)
    precision = _fraction(graded_out - overall.played["Out"], graded_out)
    return {
        "coverage": {
            "value": _rate(overall.covered, overall.dates),
            "required": 1.0,
            "passed": overall.dates > 0 and overall.covered == overall.dates,
        },
        "parse_completeness": {
            "value": _rate(overall.reports_complete, overall.dates),
            "required": 1.0,
            "passed": overall.dates > 0 and overall.reports_complete == overall.dates,
        },
        "identity": {
            "value": _rate(overall.identity_matched, overall.identity_rows),
            "required": min_identity,
            "passed": identity is not None and identity >= min_identity,
        },
        "out_precision": {
            "value": _rate(graded_out - overall.played["Out"], graded_out),
            "required": min_out_precision,
            "passed": precision is not None and precision >= min_out_precision,
        },
    }


def format_summary(document: Mapping[str, Any]) -> str:
    lines = [f"Injury report backtest over {document['dates']} game dates"]
    for name, item in document["thresholds"].items():
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(f"  {mark} {name}: {item['value']} (required >= {item['required']})")
    overall = document["overall"]["statuses"]
    for status in PLAY_RATE_STATUSES:
        lines.append(
            f"  {status}: played {overall[status]['played']}/{overall[status]['graded']}"
            f" = {overall[status]['play_rate']}"
        )
    lines.append("  month    dates covered complete identity  out-precision")
    for month, item in document["by_month"].items():
        out = item["statuses"]["Out"]
        lines.append(
            f"  {month}  {item['dates']:>5} {item['covered']:>7} "
            f"{item['reports_complete']:>8} {item['identity_rate']!s:>8}  {out['precision']}"
        )
    lines.append(
        f"  unmatched: {document['unmatched_rows']} rows, "
        f"{len(document['unmatched'])} (name, team) pairs; "
        f"games not in catalog: {len(document['games_not_in_catalog'])}; "
        f"Out who played: {len(document['out_players_who_played'])}; "
        f"coverage failures: {len(document['coverage_failures'])}; "
        f"incomplete reports: {len(document['completeness_failures'])}"
    )
    lines.append(
        f"  downloads: {document['downloads']['network']} network, "
        f"{document['downloads']['cache_hits']} cached"
    )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any | None = None,
    publication_reader: Any | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-probes", type=int, default=DEFAULT_MAX_PROBES)
    parser.add_argument(
        "--allow-game",
        action="append",
        default=[],
        metavar="DATE:AWAY@HOME",
        help="a real game the Event Catalog lacks (repeatable), e.g. 2026-01-08:MIA@CHI",
    )
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    args = parser.parse_args(argv)
    try:
        extra_games = {
            (date.fromisoformat(day), matchup)
            for day, _, matchup in (item.partition(":") for item in args.allow_game)
            if _MATCHUP_ARG.match(matchup)
        }
        if len(extra_games) != len(set(args.allow_game)):
            raise ValueError
    except ValueError:
        parser.error("--allow-game must look like 2026-01-08:MIA@CHI")

    try:
        engine = open_read_only_engine(args.database_url)
        inputs = load_inputs(engine, args.season, publication_reader=publication_reader)
        document = evaluate(
            inputs,
            ReportFetcher(args.cache_dir, delay_seconds=args.delay, session=session),
            max_probes=args.max_probes,
            allowed_games=KNOWN_GAMES_NOT_IN_CATALOG | extra_games,
        )
    except (BacktestInputError, requests.RequestException) as error:
        print(f"backtest could not run: {error}", file=sys.stderr)
        return 2
    if args.output:
        args.output.write_text(json.dumps(document, indent=2, sort_keys=True))
    print(format_summary(document))
    return 0 if document["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
