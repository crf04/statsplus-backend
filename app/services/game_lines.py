"""Each game's line from ESPN's scoreboard, stored for the Matchup (crf04/statsplus#493).

``collect`` reads the scoreboard of the current Eastern date once and stores the
first sportsbook line of every game ESPN still lists as not started.  A game
that has started, or whose line ESPN no longer shows, keeps the line last read,
so a finished game serves the last line before tip.  The Matchup reads the
stored row (``read_game_line``) and never calls ESPN.

ESPN's ``spread`` is the home team's; its ``details`` names the favourite
("NY -15.5", "EVEN"), and a line whose two disagree is not stored.  ESPN's
abbreviations are mapped to NBA tricodes and each line is matched to the one
Event Catalog game on the same Eastern date between the same two teams.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from app.domain.nba_events import is_postponed_event
from app.domain.nba_teams import NBA_TEAM_ABBREVIATION_DIALECTS
from app.domain.utc import assume_utc
from app.models.event_catalog import EventCatalogEntry
from app.models.game_lines import GameLine
from app.services.event_catalog_repository import EventCatalogRepository

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
#: Connect and per-read timeouts, in seconds.
TIMEOUT_SECONDS = (5.0, 10.0)
#: The whole read, body included.
DEADLINE_SECONDS = 15.0
SOURCE = "espn_scoreboard"
EASTERN = ZoneInfo("America/New_York")
ESPN_TRICODES = {**NBA_TEAM_ABBREVIATION_DIALECTS, "UTAH": "UTA", "WSH": "WAS"}
LINE_FIELDS = ("espn_event_id", "home_spread", "total", "provider")
PICKEM_DETAILS = frozenset({"EVEN", "PK", "PICK"})


@dataclass(frozen=True, slots=True)
class PostedLine:
    """One not-started ESPN game's line."""

    espn_event_id: str
    game_date: date
    away_tricode: str
    home_tricode: str
    home_spread: float
    total: float
    provider: str

    @property
    def label(self) -> str:
        return f"{self.espn_event_id} {self.away_tricode}@{self.home_tricode}"


def fetch_scoreboard(day: date) -> Mapping[str, Any]:
    """The scoreboard of ``day``, abandoned once reading it passes ``DEADLINE_SECONDS``.

    The per-read timeout alone would let a body that trickles in keep the host
    script waiting indefinitely, so the read runs on a daemon thread that is
    left behind, and dies with the process, when the deadline passes.
    """

    outcome: dict[str, Any] = {}

    def read() -> None:
        try:
            response = requests.get(
                SCOREBOARD_URL, params={"dates": day.strftime("%Y%m%d")}, timeout=TIMEOUT_SECONDS
            )
            response.raise_for_status()
            outcome["payload"] = response.json()
        except Exception as error:
            outcome["error"] = error

    reader = threading.Thread(target=read, name="espn-scoreboard", daemon=True)
    reader.start()
    reader.join(DEADLINE_SECONDS)
    if reader.is_alive():
        raise TimeoutError(f"ESPN scoreboard read exceeded {DEADLINE_SECONDS} s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["payload"]


def parse_scoreboard(payload: Mapping[str, Any]) -> list[PostedLine]:
    """The lines of the scoreboard's not-started games; a game without a whole, consistent line is left out."""

    lines = []
    for event in payload.get("events") or ():
        posted = _posted_line(event)
        if posted is not None:
            lines.append(posted)
    return lines


def _posted_line(event: Mapping[str, Any]) -> PostedLine | None:
    if ((event.get("status") or {}).get("type") or {}).get("state") != "pre":
        return None
    competition = (event.get("competitions") or [{}])[0]
    sides = {side.get("homeAway"): _tricode(side) for side in competition.get("competitors") or ()}
    odds = (competition.get("odds") or [{}])[0]
    home_spread, total = _number(odds.get("spread")), _number(odds.get("overUnder"))
    provider = (odds.get("provider") or {}).get("name")
    if not (event.get("id") and sides.get("away") and sides.get("home") and provider):
        return None
    if home_spread is None or total is None or total <= 0:
        return None
    if not _details_agree(odds.get("details"), home_spread, sides["away"], sides["home"]):
        return None
    try:
        tip = assume_utc(datetime.fromisoformat(str(event.get("date"))))
    except ValueError:
        return None
    return PostedLine(
        espn_event_id=str(event["id"]),
        game_date=tip.astimezone(EASTERN).date(),
        away_tricode=sides["away"],
        home_tricode=sides["home"],
        home_spread=home_spread,
        total=total,
        provider=str(provider),
    )


def _tricode(side: Mapping[str, Any]) -> str | None:
    abbreviation = str((side.get("team") or {}).get("abbreviation") or "").strip().upper()
    return ESPN_TRICODES.get(abbreviation, abbreviation) or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return _unsigned_zero(float(value))


def _unsigned_zero(value: float) -> float:
    """``value`` with a pick'em's -0.0 written as 0.0."""

    return value + 0.0


def _details_agree(details: Any, home_spread: float, away: str, home: str) -> bool:
    """False only when ``details`` names a favourite or number the spread contradicts."""

    parts = str(details or "").split()
    if len(parts) == 1 and parts[0].upper() in PICKEM_DETAILS:
        return home_spread == 0
    if len(parts) != 2:
        return True
    favourite = ESPN_TRICODES.get(parts[0].upper(), parts[0].upper())
    try:
        number = float(parts[1])
    except ValueError:
        return True
    if favourite == home:
        return number == home_spread
    if favourite == away:
        return number == -home_spread
    return True


def collect(
    engine: Engine,
    *,
    fetch: Callable[[date], Mapping[str, Any]] = fetch_scoreboard,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Read today's Eastern scoreboard once and store each matched line, in one transaction."""

    read_at = assume_utc(clock())
    day = read_at.astimezone(EASTERN).date()
    payload = fetch(day)
    lines = parse_scoreboard(payload)
    inserted = updated = unchanged = stale = 0
    unmatched = []
    with engine.begin() as connection:
        games = _catalog_games(connection, {posted.game_date for posted in lines})
        stored = {
            row["game_id"]: row
            for row in connection.execute(
                select(GameLine.__table__).where(GameLine.game_id.in_([row["nba_game_id"] for row in games]))
            ).mappings()
        }
        for posted in lines:
            matches = [
                row
                for row in games
                if row["game_date"] == posted.game_date
                and {row["away_team_tricode"], row["home_team_tricode"]}
                == {posted.away_tricode, posted.home_tricode}
            ]
            if len(matches) != 1:
                unmatched.append(posted.label)
                continue
            row = _row(matches[0], posted, read_at)
            existing = stored.get(row["game_id"])
            if not _upsert_newer(connection, row):
                stale += 1
            elif existing is None:
                inserted += 1
            elif all(existing[field] == row[field] for field in LINE_FIELDS):
                unchanged += 1
            else:
                updated += 1
    return {
        "date": day.isoformat(),
        "events": len(payload.get("events") or ()),
        "lines": len(lines),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "stale": stale,
        "unmatched": unmatched,
    }


def _upsert_newer(connection: Connection, row: Mapping[str, Any]) -> bool:
    """Insert ``row`` or replace the stored line, unless the stored one was read at the same time or later.

    Overlapping cron runs can finish out of order; the run that read last wins.
    """

    dialect_insert = postgresql_insert if connection.dialect.name == "postgresql" else sqlite_insert
    statement = dialect_insert(GameLine.__table__).values(**row)
    statement = statement.on_conflict_do_update(
        index_elements=[GameLine.game_id],
        set_={field: statement.excluded[field] for field in row if field != "game_id"},
        where=GameLine.read_at < statement.excluded.read_at,
    )
    return connection.execute(statement).rowcount == 1


def _catalog_games(connection: Connection, days: Iterable[date]) -> list[dict[str, Any]]:
    days = sorted(days)
    if not days:
        return []
    table = EventCatalogEntry.__table__
    start = datetime.combine(days[0], time(), EASTERN).astimezone(UTC)
    end = datetime.combine(days[-1] + timedelta(days=1), time(), EASTERN).astimezone(UTC)
    rows = connection.execute(
        select(table).where(table.c.scheduled_at >= start, table.c.scheduled_at < end)
    ).mappings()
    games = []
    for row in rows:
        # Decoded as the Event Catalog serves it, so stored empty evidence ('{}') is no postponement.
        if is_postponed_event(EventCatalogRepository._serialize(row)):
            continue
        games.append({**row, "game_date": assume_utc(row["scheduled_at"]).astimezone(EASTERN).date()})
    return games


def _row(game: Mapping[str, Any], posted: PostedLine, read_at: datetime) -> dict[str, Any]:
    home_spread = (
        posted.home_spread
        if game["home_team_tricode"] == posted.home_tricode
        else _unsigned_zero(-posted.home_spread)
    )
    return {
        "game_id": game["nba_game_id"],
        "espn_event_id": posted.espn_event_id,
        "away_team_id": int(game["away_team_id"]),
        "away_tricode": game["away_team_tricode"],
        "home_team_id": int(game["home_team_id"]),
        "home_tricode": game["home_team_tricode"],
        "home_spread": home_spread,
        "total": posted.total,
        "provider": posted.provider,
        "read_at": read_at,
    }


def unavailable_game_line() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "source": SOURCE,
        "provider": None,
        "read_at": None,
        "favorite_team_id": None,
        "favorite_tricode": None,
        "spread": None,
        "total": None,
        "teams": [],
    }


def read_game_line(connection: Connection, game_id: str) -> dict[str, Any]:
    """The Matchup's ``game_context.game_line`` for ``game_id``: away team first, then home."""

    row = connection.execute(select(GameLine.__table__).where(GameLine.game_id == game_id)).mappings().first()
    if row is None:
        return unavailable_game_line()
    total = float(row["total"])
    sides = [
        (int(row["away_team_id"]), row["away_tricode"], False, _unsigned_zero(-float(row["home_spread"]))),
        (int(row["home_team_id"]), row["home_tricode"], True, float(row["home_spread"])),
    ]
    favourite = next((side for side in sides if side[3] < 0), None)
    return {
        "status": "available",
        "source": SOURCE,
        "provider": row["provider"],
        "read_at": assume_utc(row["read_at"]).isoformat(),
        "favorite_team_id": None if favourite is None else favourite[0],
        "favorite_tricode": None if favourite is None else favourite[1],
        "spread": _unsigned_zero(-abs(float(row["home_spread"]))),
        "total": total,
        "teams": [
            {
                "team_id": team_id,
                "tricode": tricode,
                "is_home": is_home,
                "spread": spread,
                "implied_total": (total - spread) / 2,
            }
            for team_id, tricode, is_home, spread in sides
        ],
    }


__all__ = ["collect", "fetch_scoreboard", "parse_scoreboard", "read_game_line", "unavailable_game_line"]
