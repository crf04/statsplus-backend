"""2025-26 published, 2026-27 held: the world a Selected Season reads.

The real control plane of ``tests.services.test_serving_season_matchup``
(``tests/support/season_cutover``): game logs, the traditional-opponent
windows and a shot-type Diet are active on 2025-26.  ``hold_new`` composes
2026-27 candidates the way the nightly jobs do while the cutover waits, so
every read runs through the real Publication reader, repositories,
``GameService`` and ``MatchupService``.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    MatchupScoreSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.slate_service import SlateService
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_serving_season_matchup import (
    ATL,
    GAME_ID,
    PLAYER_ID,
    World,
    _defense_payload,
    _game_event,
    _log_payload,
)
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NEW_PROVENANCE,
    NOW,
    PLAYER_LOGS,
    SEASON_STREAM,
    THIN_CUTOFF,
    THIN_GAMES,
    THIN_PROVENANCE,
    accept_ledger,
    open_opening_week,
    payload_for,
)
from tests.test_published_season_reads import ScheduleCatalog

PLAYER_NAME = "Player 0"


def settings() -> RuntimeSettings:
    """Unpinned, uncached: the game-log pointer decides the default season."""

    return RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings.model_construct(_fields_set=set(), current_season=NEW),
        matchup_scores=MatchupScoreSettings(),
    )


class Catalog:
    """The athlete catalog game logs resolve ``Player 0`` from, every season."""

    def get_catalog(self, season, *, active_only=False):
        return [
            {
                "season": season,
                "player_id": PLAYER_ID,
                "display_name": PLAYER_NAME,
                "is_active_for_season": True,
                "team_id": ATL,
            }
        ]


class SelectedWorld(World):
    def __init__(self, tmp_path, *, engine=None):
        super().__init__(tmp_path, engine=engine)
        self.reader = DatabaseFirstPublicationReader(self.engine, clock=lambda: NOW)
        self.logs = PlayerGameLogRepository(
            self.engine,
            statistic_catalog=StatisticCatalog.load_default(),
            stats_surface_season=NEW,
            clock=lambda: NOW,
            stats_surface_max_age=timedelta(hours=30),
            publication_reader=self.reader,
        )
        self.games = GameService(
            self.engine,
            redis_client=SimpleNamespace(),
            settings=settings(),
            game_logs_source=StoredGameLogsSource(self.logs),
            publication_reader=self.reader,
            athlete_catalog=Catalog(),
            game_log_seasons=self.logs,
        )
        self.slates = SlateService(
            ScheduleCatalog({NEW: [_game_event()]}), settings=settings(), clock=lambda: NOW
        )

    def hold_opening_week(self):
        """Hold 2026-27 candidates from the opening week: one game a team."""

        open_opening_week(self.engine)
        logs = self.publications.recompose_ledger(
            PLAYER_LOGS,
            season=NEW,
            cutoff=THIN_CUTOFF,
            payload=_log_payload(NEW, THIN_GAMES),
            provenance=THIN_PROVENANCE,
            reason="2026-27 ledger composition",
        )
        defense = self.compose_new_defense(THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE)
        return logs, defense

    def hold_ten_games(self):
        """Hold 2026-27 candidates once every team has played ten games."""

        accept_ledger(self.engine, NEW_GAMES, "pbp")
        logs = self.publications.recompose_ledger(
            PLAYER_LOGS,
            season=NEW,
            cutoff=NEW_CUTOFF,
            payload=_log_payload(NEW, NEW_GAMES),
            provenance=NEW_PROVENANCE,
            reason="2026-27 ledger composition",
        )
        defense = self.compose_new_defense(NEW_GAMES, cutoff=NEW_CUTOFF, provenance=NEW_PROVENANCE)
        return logs, defense

    def compose_new_defense(self, games, *, cutoff, provenance):
        return self.publications.recompose_ledger(
            SEASON_STREAM,
            season=NEW,
            cutoff=cutoff,
            payload=_defense_payload(NEW, games),
            provenance=provenance,
            reason="2026-27 ledger composition",
        )

    def compose_new_l15(self, games, *, cutoff, provenance):
        return self.publications.recompose_ledger(
            L15_STREAM,
            season=NEW,
            cutoff=cutoff,
            payload=payload_for(L15_STREAM, NEW, games=games),
            provenance=provenance,
            reason="2026-27 ledger composition",
        )


__all__ = ["GAME_ID", "NEW", "PLAYER_ID", "PLAYER_NAME", "SelectedWorld", "settings"]
