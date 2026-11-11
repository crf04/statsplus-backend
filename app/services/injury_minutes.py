"""Regular-season minutes per game for the players on an injury report.

Two seasons are read for every listed player (crf04/statsplus#488): the
season of the report date's games and the season before it, with no minimum
game count.  Each is read where its Publications live, so the report date's
season counts before its Season Cutover, from its held candidates.  Games are
matched on the player alone, so a player traded mid-season is averaged over
every team he played for.  Each season is read with one query for all the
listed players.

A season whose game logs cannot be read (``season_unavailable``) gives no
minutes in that season; the other season and the injury list still answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.domain.nba_events import REGULAR_SEASON_TYPE
from app.errors import SeasonUnavailableError
from app.services.backtest_season import (
    SEASON_REASON_PUBLISHED,
    BacktestSeason,
    BacktestSeasons,
)
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.research_season import previous_nba_season, published_capture

_GAME_LOGS = ("player_game_logs",)
_PROJECTION_ONLY = frozenset(_GAME_LOGS)


@dataclass(frozen=True, slots=True)
class SeasonMinutes:
    average_minutes: float
    games_played: int
    season: str


@dataclass(frozen=True, slots=True)
class InjuryMinutes:
    """Minutes by player in ``season`` (``current``) and the season before (``prior``).

    ``published_season`` is ``None`` when it could not be read; then neither
    season is read.
    """

    season: str
    current: dict[int, SeasonMinutes]
    prior: dict[int, SeasonMinutes]
    published_season: str | None


class InjuryMinutesService:
    def __init__(self, *, player_logs: Any, settings: Any, publication_reader: Any | None = None):
        self.player_logs = player_logs
        self.settings = settings
        self.publication_reader = publication_reader
        self.seasons = BacktestSeasons(settings, publication_reader)

    def minutes(self, player_ids: Iterable[int], season: str) -> InjuryMinutes:
        """Minutes in ``season`` and the season before for each player who has any.

        A player whose rows in a season cannot be read (corrupt) has no
        minutes in that season: an unknown count is not zero.
        """

        wanted = set(player_ids)
        try:
            published, live = published_capture(
                self.settings,
                self.publication_reader,
                lambda capture_season: self._live(capture_season),
            )
        except SeasonUnavailableError:
            return InjuryMinutes(season, {}, {}, None)
        return InjuryMinutes(
            season,
            self._season(season, published, live, wanted),
            self._season(previous_nba_season(season), published, live, wanted),
            published,
        )

    def _season(
        self, season: str, published: str, live: Any, player_ids: set[int]
    ) -> dict[int, SeasonMinutes]:
        if not player_ids:
            return {}
        choice = BacktestSeason(
            season, SEASON_REASON_PUBLISHED, published, BacktestSeasons.mode(season, published, live)
        )
        try:
            snapshot = self.seasons.evidence(choice, live, _GAME_LOGS, projection_only_keys=_PROJECTION_ONLY)
        except SeasonUnavailableError:
            return {}
        rows = call_with_read_scope(
            self.player_logs.list_players_rows,
            season,
            sorted(player_ids),
            publication_snapshot=snapshot,
        )
        minutes: dict[int, SeasonMinutes] = {}
        for player_id, records in rows.items():
            if records is None:
                continue
            regular = [r.minutes for r in records if r.season_type == REGULAR_SEASON_TYPE]
            if regular:
                minutes[player_id] = SeasonMinutes(
                    round(sum(regular) / len(regular), 6), len(regular), season
                )
        return minutes

    def _live(self, season: Any) -> Any:
        capture = getattr(self.publication_reader, "snapshot", None)
        if not callable(capture):
            return None
        return capture(_GAME_LOGS, season=season, projection_only_keys=_PROJECTION_ONLY)


__all__ = ["InjuryMinutes", "InjuryMinutesService", "SeasonMinutes"]
