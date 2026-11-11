"""NBA.com per-game feeds: player tracking and the official box score.

Three ``stats.nba.com`` endpoints, all sent with ``nba_api``'s standard
``STATS_HEADERS`` (hand-written headers time out from Railway):

* ``boxscoreplayertrackv3``, one call per game: ``reboundChances*`` for every
  player the box score lists, 0-rebound players included;
* ``leaguedashptstats`` (Passing, Totals), one call per game day: one GP=1 row
  per player who played that day, with ``POTENTIAL_AST``;
* ``boxscoretraditionalv3``, one call per game: NBA's official box score, the
  counts sportsbooks settle on, with next-day stat corrections that PBP Stats,
  NBA LiveData and even ``playergamelogs`` can miss (crf04/statsplus-backend#763).

The day feed's ``Rebounding`` sibling is deliberately not used: it leaves out
every player with 0 rebounds.  Empty bodies and timeouts of the tracking feeds
are retried; the box score is fetched once.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import requests

from app.utils.telemetry import ProviderResponseError

ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
TIMEOUT_SECONDS = 30

#: Ledger stat name -> ``boxscoretraditionalv3`` player statistic.
BOX_SCORE_STATS = {
    "PTS": "points",
    "REB": "reboundsTotal",
    "OREB": "reboundsOffensive",
    "DREB": "reboundsDefensive",
    "AST": "assists",
    "STL": "steals",
    "BLK": "blocks",
    "TOV": "turnovers",
    "FGM": "fieldGoalsMade",
    "FGA": "fieldGoalsAttempted",
    "FG3M": "threePointersMade",
    "FG3A": "threePointersAttempted",
    "FTM": "freeThrowsMade",
    "FTA": "freeThrowsAttempted",
    "PF": "foulsPersonal",
}


def _standard_headers() -> dict[str, str]:
    from nba_api.stats.library.http import STATS_HEADERS

    return dict(STATS_HEADERS)


class NBATrackingProvider:
    """Fetch the two tracking feeds, retrying empty bodies and timeouts."""

    def __init__(
        self,
        *,
        attempts: int = ATTEMPTS,
        backoff_seconds: float = BACKOFF_SECONDS,
        timeout: float = TIMEOUT_SECONDS,
        pace_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout
        self.pace_seconds = pace_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._pace_lock = threading.Lock()
        self._next_box_score: float | None = None

    def fetch_game_rebound_chances(self, game_id: str) -> dict[int, dict[str, int | None]]:
        """``{player_id: {"minutes", "total", "offensive", "defensive"}}`` for one game.

        An empty mapping means the game's tracking is not published yet.
        """

        from nba_api.stats.library.http import NBAStatsHTTP

        # The raw response, not the BoxScorePlayerTrackV3 endpoint class: that
        # class parses on construction and raises AttributeError on an
        # unpublished game (no players, null team statistics), which this
        # parser reads as "not published yet".
        payload = self._call(
            lambda: NBAStatsHTTP()
            .send_api_request(
                endpoint="boxscoreplayertrackv3",
                parameters={"GameID": game_id},
                headers=_standard_headers(),
                timeout=self.timeout,
            )
            .get_dict()
        )
        return parse_box_score_player_track(payload)

    def fetch_day_potential_assists(self, season: str, day: date) -> dict[int, int | None]:
        """``{player_id: POTENTIAL_AST}`` for every player who played on ``day``."""

        from nba_api.stats.endpoints import LeagueDashPtStats

        stamp = day.strftime("%m/%d/%Y")
        payload = self._call(
            lambda: LeagueDashPtStats(
                pt_measure_type="Passing",
                per_mode_simple="Totals",
                player_or_team="Player",
                season=season,
                season_type_all_star="Regular Season",
                date_from_nullable=stamp,
                date_to_nullable=stamp,
                headers=_standard_headers(),
                timeout=self.timeout,
            ).get_dict()
        )
        return parse_day_passing(payload)

    def fetch_game_box_score(self, game_id: str, *, timeout: float) -> dict[int, dict[str, int]]:
        """``{player_id: {stat: count}}`` from NBA's official box score of one game.

        One request, never retried: the ledger's next run is the retry.  Requests
        start at least ``pace_seconds`` apart, like the tracking refresh's
        throttle between games, but a slow request never holds up the next.
        """

        from nba_api.stats.library.http import NBAStatsHTTP

        with self._pace_lock:
            now = self._monotonic()
            start = now if self._next_box_score is None else max(now, self._next_box_score)
            self._next_box_score = start + self.pace_seconds
        if start > now:
            self._sleep(start - now)
        payload = (
            NBAStatsHTTP()
            .send_api_request(
                endpoint="boxscoretraditionalv3",
                parameters={
                    "GameID": game_id,
                    "StartPeriod": 0,
                    "EndPeriod": 0,
                    "StartRange": 0,
                    "EndRange": 0,
                    "RangeType": 0,
                },
                headers=_standard_headers(),
                timeout=timeout,
            )
            .get_dict()
        )
        return parse_box_score_traditional(payload)

    def _call(self, request: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.attempts):
            if attempt:
                self._sleep(self.backoff_seconds * attempt)
            try:
                return request()
            except (requests.RequestException, ValueError, KeyError) as error:
                # An empty or truncated body surfaces as a JSON decode error.
                last = error
        raise ProviderResponseError(f"NBA tracking feed failed after {self.attempts} attempts: {last}")


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0 or value != int(value):
        return None
    return int(value)


def parse_box_score_player_track(payload: Any) -> dict[int, dict[str, int | None]]:
    """Per-player rebound chances from a ``boxscoreplayertrackv3`` document."""

    box = payload.get("boxScorePlayerTrack") if isinstance(payload, dict) else None
    if not isinstance(box, dict):
        raise ProviderResponseError("boxscoreplayertrackv3 has no boxScorePlayerTrack document")
    players: dict[int, dict[str, int | None]] = {}
    for side in ("homeTeam", "awayTeam"):
        for player in (box.get(side) or {}).get("players") or []:
            stats = player.get("statistics") or {}
            players[int(player["personId"])] = {
                "minutes": _minutes_played(stats.get("minutes")),
                "total": _count(stats.get("reboundChancesTotal")),
                "offensive": _count(stats.get("reboundChancesOffensive")),
                "defensive": _count(stats.get("reboundChancesDefensive")),
            }
    return players


def parse_box_score_traditional(payload: Any) -> dict[int, dict[str, int]]:
    """Per-player counting stats from a ``boxscoretraditionalv3`` document."""

    box = payload.get("boxScoreTraditional") if isinstance(payload, dict) else None
    if not isinstance(box, dict):
        raise ProviderResponseError("boxscoretraditionalv3 has no boxScoreTraditional document")
    players: dict[int, dict[str, int]] = {}
    for side in ("homeTeam", "awayTeam"):
        for player in (box.get(side) or {}).get("players") or []:
            stats = player.get("statistics") or {}
            counts = {stat: _count(stats.get(field)) for stat, field in BOX_SCORE_STATS.items()}
            if None in counts.values() or not isinstance(player.get("personId"), int):
                raise ProviderResponseError("boxscoretraditionalv3 has a malformed player row")
            players[player["personId"]] = counts
    if not players:
        raise ProviderResponseError("boxscoretraditionalv3 lists no players yet")
    return players


def _minutes_played(value: Any) -> int:
    """Whole seconds played from ``"38:48"``; 0 for a DNP or a blank clock."""

    try:
        minutes, seconds = str(value).split(":")
        return int(minutes) * 60 + int(seconds)
    except ValueError:
        return 0


def parse_day_passing(payload: Any) -> dict[int, int | None]:
    """``{player_id: POTENTIAL_AST}`` from a ``leaguedashptstats`` Passing document."""

    sets = payload.get("resultSets") if isinstance(payload, dict) else None
    if not sets:
        raise ProviderResponseError("leaguedashptstats has no result set")
    headers = sets[0]["headers"]
    try:
        id_index, pot_index = headers.index("PLAYER_ID"), headers.index("POTENTIAL_AST")
    except ValueError as error:
        raise ProviderResponseError("leaguedashptstats Passing lacks PLAYER_ID or POTENTIAL_AST") from error
    return {int(row[id_index]): _count(row[pot_index]) for row in sets[0]["rowSet"]}


__all__ = [
    "BOX_SCORE_STATS",
    "NBATrackingProvider",
    "parse_box_score_player_track",
    "parse_box_score_traditional",
    "parse_day_passing",
]
