"""NBA.com's daily lineups feed: each team's starters and inactive players (crf04/statsplus#492).

One static document per US Eastern day,
``https://stats.nba.com/js/data/leaders/00_daily_lineups_YYYYMMDD.json``, sent
with ``nba_api``'s standard ``STATS_HEADERS``.  A day not published yet
redirects to ``/error/`` instead of answering 404.

Each team lists its players, each with ``lineupStatus`` (``Expected`` or
``Confirmed``), ``position`` and ``rosterStatus`` (``Active`` or
``Inactive``).  A starter is a row whose ``position`` is set; every other row
has an empty one.  Until a lineup is confirmed a starter is listed twice: once
as an ``Expected`` starter and again as a bench row.  ``timestamp`` is US
Eastern wall time with no offset.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.utils.telemetry import ProviderResponseError

EASTERN = ZoneInfo("America/New_York")
URL = "https://stats.nba.com/js/data/leaders/00_daily_lineups_{day:%Y%m%d}.json"
TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class Starter:
    player_id: int
    name: str
    position: str
    lineup_status: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "position": self.position,
            "lineup_status": self.lineup_status,
        }


@dataclass(frozen=True, slots=True)
class InactivePlayer:
    player_id: int
    name: str

    def as_json(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "name": self.name}


@dataclass(frozen=True, slots=True)
class TeamLineup:
    """One team's lineup for one game, as the feed lists it."""

    game_id: str
    team_id: int
    tricode: str
    #: The newest ``timestamp`` among the team's rows, or None when it lists none.
    feed_timestamp: datetime | None
    starters: tuple[Starter, ...]
    inactive: tuple[InactivePlayer, ...]


def fetch_daily_lineups(
    day: date,
    *,
    timeout: float = TIMEOUT_SECONDS,
    get: Callable[..., Any] = requests.get,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """The feed document for one US Eastern day, or None when it is not published yet.

    ``timeout`` bounds the whole download, not only each socket read, so a
    body that trickles in is abandoned: at most ``timeout`` plus one read.
    """

    from nba_api.stats.library.http import STATS_HEADERS

    deadline = monotonic() + timeout
    with get(
        URL.format(day=day),
        headers=dict(STATS_HEADERS),
        timeout=READ_TIMEOUT_SECONDS,
        allow_redirects=False,
        stream=True,
    ) as response:
        if response.is_redirect or response.status_code == 404:
            return None
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body += chunk
            if monotonic() > deadline:
                raise requests.Timeout(f"daily lineups download took longer than {timeout} s")
    return json.loads(body)


def parse_daily_lineups(payload: Any) -> list[TeamLineup]:
    """Every team of the document, away team first in each game.

    A document that is not the expected shape is refused whole, so a partial
    read never replaces a stored lineup.
    """

    games = payload.get("games") if isinstance(payload, Mapping) else None
    if not isinstance(games, list):
        raise ProviderResponseError("daily lineups document has no games list")
    try:
        return [_team(str(game["gameId"]), game[side]) for game in games for side in ("awayTeam", "homeTeam")]
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderResponseError(f"daily lineups document is malformed: {error!r}") from error


def _team(game_id: str, team: Mapping[str, Any]) -> TeamLineup:
    starters: dict[int, Starter] = {}
    inactive: dict[int, InactivePlayer] = {}
    stamps = []
    for row in team["players"]:
        player_id = int(row["personId"])
        name = str(row["playerName"])
        if row.get("timestamp"):
            stamps.append(datetime.fromisoformat(row["timestamp"]).replace(tzinfo=EASTERN))
        position = (row.get("position") or "").strip()
        if position:
            starters.setdefault(player_id, Starter(player_id, name, position, row.get("lineupStatus")))
        elif row.get("rosterStatus") == "Inactive":
            inactive.setdefault(player_id, InactivePlayer(player_id, name))
    return TeamLineup(
        game_id=game_id,
        team_id=int(team["teamId"]),
        tricode=str(team["teamAbbreviation"]),
        feed_timestamp=max(stamps, default=None),
        starters=tuple(starters.values()),
        inactive=tuple(player for player_id, player in inactive.items() if player_id not in starters),
    )
