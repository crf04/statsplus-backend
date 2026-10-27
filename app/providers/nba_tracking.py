"""NBA.com player-tracking feeds for rebound chances and potential assists.

Two ``stats.nba.com`` endpoints, both sent with ``nba_api``'s standard
``STATS_HEADERS`` (hand-written headers time out from Railway):

* ``boxscoreplayertrackv3``, one call per game: ``reboundChances*`` for every
  player the box score lists, 0-rebound players included;
* ``leaguedashptstats`` (Passing, Totals), one call per game day: one GP=1 row
  per player who played that day, with ``POTENTIAL_AST``.

The day feed's ``Rebounding`` sibling is deliberately not used: it leaves out
every player with 0 rebounds.  Empty bodies and timeouts are retried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Any

import requests

from app.utils.telemetry import ProviderResponseError

ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
TIMEOUT_SECONDS = 30


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout
        self._sleep = sleep

    def fetch_game_rebound_chances(self, game_id: str) -> dict[int, dict[str, int | None]]:
        """``{player_id: {"minutes", "total", "offensive", "defensive"}}`` for one game.

        An empty mapping means the game's tracking is not published yet.
        """

        from nba_api.stats.endpoints import BoxScorePlayerTrackV3

        payload = self._call(
            lambda: BoxScorePlayerTrackV3(
                game_id=game_id, headers=_standard_headers(), timeout=self.timeout
            ).get_dict()
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


__all__ = ["NBATrackingProvider", "parse_box_score_player_track", "parse_day_passing"]
