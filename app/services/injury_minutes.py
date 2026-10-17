"""Regular-season minutes per game for the players on an injury report.

The season is the published one (the one ``research_season`` names) when the
player has at least ``MIN_PUBLISHED_GAMES`` regular-season games in it, else
the season before it when he has any games there, else none.  Games are
matched on the player alone, so a player traded mid-season is averaged over
every team he played for.  The two seasons are each read with one query for
all the listed players.

A season whose game logs cannot be read (``season_unavailable``) gives no
minutes for the rows that need it; the injury list itself still answers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.domain.nba_events import REGULAR_SEASON_TYPE
from app.errors import SeasonUnavailableError
from app.services.backtest_season import SEASON_REASON_PUBLISHED, BacktestSeason, BacktestSeasons
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.research_season import previous_nba_season, published_capture

MIN_PUBLISHED_GAMES = 5
_GAME_LOGS = ("player_game_logs",)
_PROJECTION_ONLY = frozenset(_GAME_LOGS)


@dataclass(frozen=True, slots=True)
class SeasonMinutes:
    average_minutes: float
    games_played: int
    season: str


class InjuryMinutesService:
    def __init__(self, *, player_logs: Any, settings: Any, publication_reader: Any | None = None):
        self.player_logs = player_logs
        self.settings = settings
        self.publication_reader = publication_reader
        self.seasons = BacktestSeasons(settings, publication_reader)

    def minutes(self, player_ids: Iterable[int]) -> dict[int, SeasonMinutes]:
        """Minutes for each player who has any; absent players have none."""

        wanted = set(player_ids)
        if not wanted:
            return {}
        try:
            published, live = published_capture(
                self.settings,
                self.publication_reader,
                lambda season: self._live(season),
            )
            choice = BacktestSeason(published, SEASON_REASON_PUBLISHED, published)
            snapshot = self.seasons.evidence(
                choice, live, _GAME_LOGS, projection_only_keys=_PROJECTION_ONLY
            )
            current = self._averages(published, wanted, snapshot)
        except SeasonUnavailableError:
            # Without the published count the season rule cannot be applied.
            return {}
        result = {
            player_id: SeasonMinutes(average, games, published)
            for player_id, (average, games) in current.items()
            if games >= MIN_PUBLISHED_GAMES
        }
        needing_prior = wanted - result.keys()
        if not needing_prior:
            return result
        prior = previous_nba_season(published)
        try:
            retained = BacktestSeason(prior, SEASON_REASON_PUBLISHED, published)
            snapshot = self.seasons.evidence(
                retained, None, _GAME_LOGS, projection_only_keys=_PROJECTION_ONLY
            )
            earlier = self._averages(prior, needing_prior, snapshot)
        except SeasonUnavailableError:
            return result
        result.update(
            (player_id, SeasonMinutes(average, games, prior))
            for player_id, (average, games) in earlier.items()
        )
        return result

    def _live(self, season: Any) -> Any:
        capture = getattr(self.publication_reader, "snapshot", None)
        if not callable(capture):
            return None
        return capture(_GAME_LOGS, season=season, projection_only_keys=_PROJECTION_ONLY)

    def _averages(
        self, season: str, player_ids: set[int], snapshot: Any
    ) -> dict[int, tuple[float, int]]:
        totals: dict[int, list[float]] = {}
        for row in call_with_read_scope(
            self.player_logs.list_players_rows,
            season,
            sorted(player_ids),
            publication_snapshot=snapshot,
        ):
            if row.season_type != REGULAR_SEASON_TYPE:
                continue
            total = totals.setdefault(row.player_id, [0.0, 0])
            total[0] += row.minutes
            total[1] += 1
        return {
            player_id: (round(minutes / games, 6), int(games))
            for player_id, (minutes, games) in totals.items()
        }


__all__ = ["InjuryMinutesService", "MIN_PUBLISHED_GAMES", "SeasonMinutes"]
