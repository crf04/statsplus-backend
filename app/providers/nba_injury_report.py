"""NBA official injury-report adapter.

The league publishes each report as a PDF at a time-stamped path under
``https://ak-static.cms.nba.com/referee/injury/``.  Two file-name dialects
exist: ``Injury-Report_2025-10-21_05PM.pdf`` (hourly, early 2025-26) and
``Injury-Report_2025-12-25_05_00PM.pdf`` (quarter-hour).  A time with no
report answers 403 (or 404).

The adapter is only an injected provider boundary: importing this module never
starts a request, and the parser (``parse_injury_report``) is a pure function of
the PDF bytes so recorded files can be replayed offline.

PDF layout.  Seven columns: Game Date, Game Time (ET), Matchup (``CLE@ATL``),
Team (full name), Player Name (``Last, First``), Current Status, Reason.  Game
Date, Game Time, Matchup and Team print once and are blank on the rows below
them (they carry down, across page breaks too).  A cell may wrap, and every
cell of a row is centred on the row, so a wrapped reason has lines both above
and below the line that carries the player and status.  Rows are ~22 points
apart and the lines of one wrapped row ~7 points apart, which is what separates
"next line of this row" from "next row".  A team that has not reported prints
``NOT YET SUBMITTED`` in the Reason column.
"""

from __future__ import annotations

import io
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from pypdf import PdfReader

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.nba_teams import (
    NBA_TEAM_FULL_NAME_TO_TRICODE,
    canonical_nba_team_abbreviation,
)
from app.errors import ProviderUnavailableError
from app.utils.telemetry import (
    PROVIDER_NBA_INJURY_REPORT,
    ProviderResponseError,
    provider_call,
)


REPORT_BASE_URL = "https://ak-static.cms.nba.com/referee/injury/"
#: Public landing page for the report, linked from the Matchup surface.
SOURCE_URL = "https://official.nba.com/nba-injury-report-2025-26-season/"
#: The CDN refuses clients that do not look like a browser.
USER_AGENT = "Mozilla/5.0 (compatible; StatsPlus/1.0)"
EASTERN = ZoneInfo("America/New_York")

#: Rows of the report that carry no canonical status stay in the evidence with
#: their raw status (``Available`` is the one the league prints today).
CANONICAL_INJURY_STATUSES = {
    "probable": "Probable",
    "questionable": "Questionable",
    "doubtful": "Doubtful",
    "out": "Out",
}
REPORT_STATUSES = frozenset(
    {"Out", "Doubtful", "Questionable", "Probable", "Available"}
)

#: Lines of one wrapped reason sit ~7 points apart and a second injury stacked
#: in the same row ~14; rows are >= 22 apart.  Anything under this gap
#: continues the row above.
_ROW_BREAK_POINTS = 18.0
#: Header text starts ~1 point left of the cell text beneath it.
_COLUMN_TOLERANCE = 4.0
_COLUMNS = ("date", "time", "matchup", "team", "player", "status", "reason")
_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_MATCHUP = re.compile(r"^([A-Z]{3})@([A-Z]{3})$")
_CHUNK_BYTES = 16 * 1024
_MAX_REPORT_BYTES = 10 * 1024 * 1024
_NOT_SUBMITTED = "NOT YET SUBMITTED"
_TITLE_TIME = re.compile(
    r"Injury Report:\s*(\d{2})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})\s+(AM|PM)"
)
_REPORT_NAME = re.compile(
    r"^Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})(?:_(\d{2}))?(AM|PM)\.pdf$"
)


@dataclass(frozen=True, slots=True)
class InjuryEntryEvidence:
    """One normalized source row, before canonical athlete reconciliation."""

    entry_id: str
    source_player_id: str
    source_player_name: str
    source_team_tricode: str
    canonical_status: str | None
    raw_status: str
    reason: str
    source_url: str
    #: The game this row describes: Eastern game date (ISO) and ``AWAY@HOME``.
    #: Empty only on evidence stored before the fields existed.
    game_date: str = ""
    matchup: str = ""
    #: The report's own header time (ISO, Eastern offset); empty on evidence
    #: stored before the field existed.
    report_time: str = ""

    def to_document(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "source_player_id": self.source_player_id,
            "source_player_name": self.source_player_name,
            "source_team_tricode": self.source_team_tricode,
            "canonical_status": self.canonical_status,
            "raw_status": self.raw_status,
            "reason": self.reason,
            "source_url": self.source_url,
            "game_date": self.game_date,
            "matchup": self.matchup,
            "report_time": self.report_time,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "InjuryEntryEvidence":
        canonical_status = (
            None
            if value.get("canonical_status") is None
            else str(value["canonical_status"])
        )
        if canonical_status not in {None, *CANONICAL_INJURY_STATUSES.values()}:
            raise ValueError("stored injury status is not canonical")
        return cls(
            entry_id=str(value["entry_id"]),
            source_player_id=str(value["source_player_id"]),
            source_player_name=str(value["source_player_name"]),
            source_team_tricode=str(value["source_team_tricode"]),
            canonical_status=canonical_status,
            raw_status=str(value["raw_status"]),
            reason=str(value.get("reason") or ""),
            source_url=str(value["source_url"]),
            game_date=str(value.get("game_date") or ""),
            matchup=str(value.get("matchup") or ""),
            report_time=str(value.get("report_time") or ""),
        )


@dataclass(frozen=True, slots=True)
class InjuryProviderSnapshot:
    """Raw evidence and its deterministic source normalization."""

    raw_payload: list[Mapping[str, Any]]
    entries: tuple[InjuryEntryEvidence, ...]
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class InjuryReportRow:
    """One printed status row with the carried-down cells filled in."""

    game_date: date
    game_time_et: str
    matchup: str
    team_name: str
    team_tricode: str
    player_name: str  # as printed: "Last, First"
    status: str
    reason: str

    @property
    def display_name(self) -> str:
        """``Last, First`` rewritten to ``First Last`` (``Jimmy Butler III``)."""

        last, comma, first = self.player_name.rpartition(", ")
        return f"{first} {last}".strip() if comma else self.player_name

    def to_document(self) -> dict[str, Any]:
        return {
            "game_date": self.game_date.isoformat(),
            "game_time_et": self.game_time_et,
            "matchup": self.matchup,
            "team": self.team_name,
            "team_tricode": self.team_tricode,
            "player": self.player_name,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class UnsubmittedTeam:
    """A team the report lists as ``NOT YET SUBMITTED``."""

    game_date: date
    matchup: str
    team_name: str
    team_tricode: str


@dataclass(frozen=True, slots=True)
class ParsedInjuryReport:
    title: str
    rows: tuple[InjuryReportRow, ...]
    unsubmitted: tuple[UnsubmittedTeam, ...]
    #: Reason-column text that fits neither a status row nor a team-level
    #: marker; an empty tuple means every printed line was understood.
    unparsed: tuple[str, ...]
    #: The header timestamp ("Injury Report: 10/21/25 05:30 PM", Eastern),
    #: which can differ from the time in the file name.
    report_time: datetime | None = None


def report_file_name(moment: datetime, *, quarter_hour: bool) -> str:
    """The file name the league would use for an Eastern wall-clock time."""

    eastern = moment.astimezone(EASTERN)
    hour = eastern.hour % 12 or 12
    meridiem = "AM" if eastern.hour < 12 else "PM"
    stamp = (
        f"{hour:02d}_{eastern.minute:02d}{meridiem}"
        if quarter_hour
        else f"{hour:02d}{meridiem}"
    )
    return f"Injury-Report_{eastern:%Y-%m-%d}_{stamp}.pdf"


def report_time_from_name(name: str) -> datetime | None:
    """Invert ``report_file_name`` (Eastern wall-clock, as an aware datetime)."""

    match = _REPORT_NAME.match(name)
    if match is None:
        return None
    day, hour, minute, meridiem = match.groups()
    hour_24 = int(hour) % 12 + (12 if meridiem == "PM" else 0)
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, hour_24, int(minute or 0), tzinfo=EASTERN)


def candidate_report_names(
    now: datetime, *, max_requests: int
) -> list[str]:
    """Newest-first file names at or before ``now``, at most ``max_requests``.

    Walks quarter-hour slots backwards.  On the hour both dialects are
    candidates (quarter-hour name first), so the walk covers the hourly era
    and the quarter-hour era without knowing which one a date belongs to.
    """

    eastern = now.astimezone(EASTERN)
    slot = eastern.replace(
        minute=eastern.minute - eastern.minute % 15, second=0, microsecond=0
    )
    names: list[str] = []
    while len(names) < max_requests:
        # A fall-back hour prints the same wall-clock name twice; probe it once.
        for quarter_hour in (True, False):
            if quarter_hour is False and slot.minute != 0:
                break
            name = report_file_name(slot, quarter_hour=quarter_hour)
            if name not in names and len(names) < max_requests:
                names.append(name)
        # Step through UTC so a DST change cannot repeat or skip a slot.
        slot = (slot.astimezone(timezone.utc) - timedelta(minutes=15)).astimezone(
            EASTERN
        )
    return names


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _fragments(page: Any) -> list[tuple[float, float, str]]:
    found: list[tuple[float, float, str]] = []

    def visit(text: str, cm: Any, tm: Any, font_dict: Any, font_size: Any) -> None:
        stripped = text.strip()
        if stripped:
            found.append((round(float(tm[5]), 1), float(tm[4]), stripped))

    page.extract_text(visitor_text=visit)
    return found


def _join(pieces: list[str]) -> str:
    out = ""
    for piece in pieces:
        # "Finney-" + "Smith," and "Two-" + "Way" are one hyphenated word.
        if out.endswith("-") and len(out) > 1 and not out.endswith(" -"):
            out += piece
        else:
            out = f"{out} {piece}" if out else piece
    return out


class _Cluster:
    """The printed lines of one row, split into column cells."""

    def __init__(self) -> None:
        self.cells: dict[str, list[tuple[float, float, str]]] = {
            name: [] for name in _COLUMNS
        }

    def add(self, column: str, y: float, x: float, text: str) -> None:
        self.cells[column].append((y, x, text))

    def text(self, column: str) -> str:
        pieces = sorted(self.cells[column])
        return _join([text for _, _, text in pieces])


def _columns_from_header(
    fragments: list[tuple[float, float, str]],
) -> tuple[float, list[float]] | None:
    """The header row's y and the seven column start positions, if present."""

    by_y: dict[float, list[tuple[float, str]]] = {}
    for y, x, text in fragments:
        by_y.setdefault(y, []).append((x, text))
    for y in sorted(by_y):
        words = sorted(by_y[y])
        texts = [text for _, text in words]
        if "Matchup" in texts and "Reason" in texts and "Team" in texts:
            starts = [
                x
                for x, text in words
                if text in {"Game", "Matchup", "Team", "Player", "Current", "Reason"}
            ]
            if len(starts) == len(_COLUMNS):
                return y, starts
    return None


def _column_of(x: float, starts: list[float]) -> str:
    chosen = _COLUMNS[0]
    for name, start in zip(_COLUMNS, starts):
        if x >= start - _COLUMN_TOLERANCE:
            chosen = name
    return chosen


def _clusters(
    fragments: list[tuple[float, float, str]],
    header_y: float,
    starts: list[float],
) -> list[_Cluster]:
    lines: dict[float, list[tuple[float, str]]] = {}
    for y, x, text in fragments:
        if y > header_y:
            lines.setdefault(y, []).append((x, text))
    clusters: list[_Cluster] = []
    previous_y: float | None = None
    for y in sorted(lines):
        words = sorted(lines[y])
        if words[0][1] == "Page":
            continue  # "Page 1 of 7" footer
        if previous_y is None or y - previous_y > _ROW_BREAK_POINTS:
            clusters.append(_Cluster())
        for x, text in words:
            clusters[-1].add(_column_of(x, starts), y, x, text)
        previous_y = y
    return clusters


def _tricode_for_team(team_name: str) -> str:
    return NBA_TEAM_FULL_NAME_TO_TRICODE.get(team_name, team_name.upper())


def parse_injury_report(data: bytes) -> ParsedInjuryReport:
    """Parse one official injury-report PDF into carried-down rows."""

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [_fragments(page) for page in reader.pages]
    except Exception as error:  # pypdf raises many unrelated types
        raise ProviderResponseError("injury report is not a readable PDF") from error
    if not pages:
        raise ProviderResponseError("injury report has no pages")

    title = ""
    rows: list[InjuryReportRow] = []
    unsubmitted: list[UnsubmittedTeam] = []
    unparsed: list[str] = []
    carried = {"date": "", "time": "", "matchup": "", "team": ""}
    pending_prefix = ""
    starts: list[float] = []

    for page_number, fragments in enumerate(pages):
        header = _columns_from_header(fragments)
        if header is not None:
            header_y, starts = header
        elif starts and fragments:
            # Later pages repeat the title line but not the column header.
            header_y = min(y for y, _, _ in fragments)
        else:
            raise ProviderResponseError("injury report has no column header")
        if page_number == 0:
            title = _join(
                [text for y, _, text in sorted(fragments) if y < header_y]
            )
        clusters = _clusters(fragments, header_y, starts)
        for position, cluster in enumerate(clusters):
            reason = cluster.text("reason")
            if not any(cluster.text(name) for name in _COLUMNS[:6]):
                # Only a wrapped reason.  At the top of a page it finishes the
                # row whose status line closed the last page; at the bottom it
                # starts the first row of the next page.
                if position == 0 and rows:
                    rows[-1] = replace(rows[-1], reason=_join([rows[-1].reason, reason]))
                elif position == len(clusters) - 1:
                    pending_prefix = reason
                else:
                    unparsed.append(reason)
                continue
            for key in carried:
                value = cluster.text(key)
                if value:
                    carried[key] = value
            status = cluster.text("status")
            player = cluster.text("player")
            matchup = carried["matchup"]
            if (
                not _DATE.match(carried["date"])
                or not _MATCHUP.match(matchup)
                or not carried["team"]
            ):
                unparsed.append(_join([cluster.text(c) for c in _COLUMNS]))
                continue
            try:
                game_date = datetime.strptime(carried["date"], "%m/%d/%Y").date()
            except ValueError:
                unparsed.append(_join([cluster.text(c) for c in _COLUMNS]))
                continue
            tricode = _tricode_for_team(carried["team"])
            if reason == _NOT_SUBMITTED and not status and not player:
                unsubmitted.append(
                    UnsubmittedTeam(game_date, matchup, carried["team"], tricode)
                )
                continue
            if not player or status not in REPORT_STATUSES:
                # Includes two rows merged into one ("Out Out").
                unparsed.append(_join([cluster.text(c) for c in _COLUMNS]))
                continue
            if pending_prefix:
                reason = _join([pending_prefix, reason])
                pending_prefix = ""
            rows.append(
                InjuryReportRow(
                    game_date=game_date,
                    game_time_et=carried["time"],
                    matchup=matchup,
                    team_name=carried["team"],
                    team_tricode=tricode,
                    player_name=player,
                    status=status,
                    reason=reason,
                )
            )
    if pending_prefix:
        unparsed.append(pending_prefix)
    header = _TITLE_TIME.search(title)
    if header is None:
        raise ProviderResponseError("injury report has no readable header time")
    month, day, year, hour, minute, meridiem = header.groups()
    try:
        report_time = datetime(
            2000 + int(year),
            int(month),
            int(day),
            int(hour) % 12 + (12 if meridiem == "PM" else 0),
            int(minute),
            tzinfo=EASTERN,
        )
    except ValueError as error:
        raise ProviderResponseError("injury report header time is invalid") from error
    return ParsedInjuryReport(
        title=title,
        rows=tuple(rows),
        unsubmitted=tuple(unsubmitted),
        unparsed=tuple(unparsed),
        report_time=report_time,
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def entry_id_for(row: InjuryReportRow) -> str:
    """The stable id of a row's evidence: date, matchup and player."""

    return f"nba_official:{row.game_date.isoformat()}:{row.matchup}:{_slug(row.display_name)}"


def entries_from_report(
    report: ParsedInjuryReport, *, source_url: str
) -> tuple[InjuryEntryEvidence, ...]:
    """Normalize rows to evidence; one entry per player per game."""

    entries: list[InjuryEntryEvidence] = []
    seen: set[str] = set()
    for row in report.rows:
        slug = _slug(row.display_name)
        entry_id = entry_id_for(row)
        if entry_id in seen:
            continue
        seen.add(entry_id)
        entries.append(
            InjuryEntryEvidence(
                entry_id=entry_id,
                source_player_id=f"{row.team_tricode}:{slug}",
                source_player_name=row.display_name,
                source_team_tricode=canonical_nba_team_abbreviation(row.team_tricode),
                canonical_status=CANONICAL_INJURY_STATUSES.get(row.status.casefold()),
                raw_status=row.status,
                reason=row.reason,
                source_url=source_url,
                game_date=row.game_date.isoformat(),
                matchup=row.matchup,
                report_time=(
                    "" if report.report_time is None else report.report_time.isoformat()
                ),
            )
        )
    return tuple(entries)


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


class NBAInjuryReportProvider:
    """Retrieve the newest published official injury report."""

    BASE_URL = REPORT_BASE_URL
    SOURCE_URL = SOURCE_URL
    #: Hard cap on requests per ``get_snapshot`` call.
    MAX_REQUESTS = 12

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        settings: RuntimeSettings | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_runtime_settings()
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.sleep = sleep
        #: Monotonic time before which a failed search is not repeated.  Local
        #: to this worker: the service's single-flight already serializes
        #: refreshes within it.
        self._cooldown_until = float("-inf")
        self.session.headers.update(
            {
                "Accept": "application/pdf",
                "User-Agent": USER_AGENT,
            }
        )

    def get_snapshot(self) -> InjuryProviderSnapshot:
        """Find, fetch and normalize the newest report at or before now.

        "Newest" is by the report's own header time, which can run past the
        time in its file name; a report stamped after now is not yet current.
        After a failed search no request is made until the cooldown ends.
        """

        retrieved_at = self._now_utc()
        providers = self.settings.providers
        started = self.monotonic()
        if started < self._cooldown_until:
            raise ProviderUnavailableError(
                "The injury provider recently failed; retrying shortly."
            )
        deadline = started + providers.injury_report_deadline_seconds
        try:
            with provider_call(PROVIDER_NBA_INJURY_REPORT, "get_injuries") as tracker:
                snapshot = self._search(retrieved_at, deadline, tracker)
        except ProviderResponseError as error:
            self._cooldown_until = started + providers.injury_report_failure_cooldown_seconds
            raise ProviderUnavailableError(
                "The injury provider returned an invalid response.", detail=error
            ) from error
        except requests.RequestException as error:
            self._cooldown_until = started + providers.injury_report_failure_cooldown_seconds
            raise ProviderUnavailableError(
                "The injury provider could not be reached.", detail=error
            ) from error
        return snapshot

    def _search(
        self, retrieved_at: datetime, deadline: float, tracker: Any
    ) -> InjuryProviderSnapshot:
        providers = self.settings.providers
        requested = 0
        for name in candidate_report_names(
            retrieved_at, max_requests=self.MAX_REQUESTS
        ):
            if requested:
                self.sleep(
                    max(
                        0.0,
                        min(
                            providers.injury_report_probe_delay_seconds,
                            deadline - self.monotonic(),
                        ),
                    )
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise requests.Timeout("injury report deadline exceeded")
            requested += 1
            response = self.session.get(
                self.BASE_URL + name,
                timeout=(
                    min(providers.injury_report_connect_timeout_seconds, remaining),
                    min(providers.injury_report_read_timeout_seconds, remaining),
                ),
                stream=True,
            )
            tracker.status_code = getattr(response, "status_code", None)
            try:
                if response.status_code in (403, 404):
                    continue  # no report was published at that time
                response.raise_for_status()
                content = self._read_body(response, deadline)
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
            if not content.startswith(b"%PDF"):
                raise ProviderResponseError("injury report response is not a PDF")
            url = self.BASE_URL + name
            report = parse_injury_report(content)
            if report.unparsed or not (report.rows or report.unsubmitted):
                raise ProviderResponseError("injury report layout was not understood")
            if report.report_time > retrieved_at:
                continue  # stamped after now: not yet current, try an older one
            entries = entries_from_report(report, source_url=url)
            raw = [row.to_document() for row in report.rows]
            raw.extend(
                {
                    "game_date": team.game_date.isoformat(),
                    "matchup": team.matchup,
                    "team": team.team_name,
                    "team_tricode": team.team_tricode,
                    "status": _NOT_SUBMITTED,
                }
                for team in report.unsubmitted
            )
            if self.monotonic() >= deadline:
                raise requests.Timeout("injury report deadline exceeded")
            return InjuryProviderSnapshot(raw, entries, retrieved_at)
        raise ProviderResponseError("no recent injury report was published")

    def _read_body(self, response: Any, deadline: float) -> bytes:
        """Download the body in chunks, giving up the moment the deadline passes.

        A read timeout only measures silence, so a server dribbling bytes
        would otherwise hold the refresh past its budget.
        """

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            if self.monotonic() >= deadline:
                raise requests.Timeout("injury report deadline exceeded")
            size += len(chunk)
            if size > _MAX_REPORT_BYTES:
                raise ProviderResponseError("injury report is implausibly large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _now_utc(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("injury report clock must return an aware datetime")
        return value.astimezone(timezone.utc)


__all__ = [
    "CANONICAL_INJURY_STATUSES",
    "InjuryEntryEvidence",
    "InjuryProviderSnapshot",
    "InjuryReportRow",
    "NBAInjuryReportProvider",
    "ParsedInjuryReport",
    "REPORT_STATUSES",
    "UnsubmittedTeam",
    "candidate_report_names",
    "entries_from_report",
    "entry_id_for",
    "parse_injury_report",
    "report_file_name",
    "report_time_from_name",
]
