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


@dataclass(frozen=True, slots=True)
class InjuryMinutes:
    """Minutes by player, and the published season the rule compared against.

    ``published_season`` is ``None`` when it could not be read.
    """

    by_player: dict[int, SeasonMinutes]
    published_season: str | None


class InjuryMinutesService:
    def __init__(self, *, player_logs: Any, settings: Any, publication_reader: Any | None = None):
        self.player_logs = player_logs
        self.settings = settings
        self.publication_reader = publication_reader
        self.seasons = BacktestSeasons(settings, publication_reader)

    def minutes(self, player_ids: Iterable[int]) -> InjuryMinutes:
        """Minutes for each player who has any; absent players have none.

        A player whose rows in a season cannot be read (corrupt) is left out
        and never falls back to the other season: an unknown count is not zero.
        """

        wanted = set(player_ids)
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
            return InjuryMinutes({}, None)
        result = {
            player_id: SeasonMinutes(found[0], found[1], published)
            for player_id, found in current.items()
            if found is not None and found[1] >= MIN_PUBLISHED_GAMES
        }
        needing_prior = {
            player_id
            for player_id in wanted - result.keys()
            if player_id not in current or current[player_id] is not None
        }
        if not needing_prior:
            return InjuryMinutes(result, published)
        prior = previous_nba_season(published)
        try:
            retained = BacktestSeason(prior, SEASON_REASON_PUBLISHED, published)
            snapshot = self.seasons.evidence(
                retained, None, _GAME_LOGS, projection_only_keys=_PROJECTION_ONLY
            )
            earlier = self._averages(prior, needing_prior, snapshot)
        except SeasonUnavailableError:
            return InjuryMinutes(result, published)
        result.update(
            (player_id, SeasonMinutes(found[0], found[1], prior))
            for player_id, found in earlier.items()
            if found is not None
        )
        return InjuryMinutes(result, published)

    def _live(self, season: Any) -> Any:
        capture = getattr(self.publication_reader, "snapshot", None)
        if not callable(capture):
            return None
        return capture(_GAME_LOGS, season=season, projection_only_keys=_PROJECTION_ONLY)

    def _averages(
        self, season: str, player_ids: set[int], snapshot: Any
    ) -> dict[int, tuple[float, int] | None]:
        """Per player ``(average, games)``; ``None`` when his rows are unreadable.

        A player with rows but no regular-season game is absent.
        """

        if not player_ids:
            return {}
        rows = call_with_read_scope(
            self.player_logs.list_players_rows,
            season,
            sorted(player_ids),
            publication_snapshot=snapshot,
        )
        averages: dict[int, tuple[float, int] | None] = {}
        for player_id, records in rows.items():
            if records is None:
                averages[player_id] = None
                continue
            regular = [r.minutes for r in records if r.season_type == REGULAR_SEASON_TYPE]
            if regular:
                averages[player_id] = (round(sum(regular) / len(regular), 6), len(regular))
        return averages


__all__ = ["InjuryMinutes", "InjuryMinutesService", "MIN_PUBLISHED_GAMES", "SeasonMinutes"]
