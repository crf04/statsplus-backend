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

from sqlalchemy import text

from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    MatchupScoreSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.domain.player_diet_taxonomy import PLAYER_DIET_QUALIFIER_SLICES
from app.domain.team_matchup_taxonomy import NBA_PUBLICATION_TAXONOMY
from app.services.diet_baselines import DietBaselinesService
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.matchup_injuries import unavailable_injury_result
from app.services.matchup_reuse import ComposedMatchupCache, PublicationSnapshotReuse
from app.services.matchup_selection import MatchupSelectionService
from app.services.matchup_snapshot import capture_matchup_snapshot
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.player_service import PlayerProfileReader, PlayerService
from app.services.slate_edges import SlateEdgesService
from app.services.slate_service import SlateService
from app.services.statistic_catalog import StatisticCatalog
from app.services.team_filter_rankings import TeamFilterRankingService
from app.services.team_service import TeamService
from tests.services.test_serving_season_matchup import (
    ATL,
    BOS,
    GAME_ID,
    PLAYER_ID,
    TRICODES,
    World,
    _Catalog,
    _game_event,
    _log_payload,
    _Pool,
)
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NEW_PROVENANCE,
    NOW,
    OLD,
    OLD_CUTOFF,
    OLD_GAMES,
    OLD_PROVENANCE,
    PLAYER_LOGS,
    SEASON_STREAM,
    THIN_CUTOFF,
    THIN_GAMES,
    THIN_PROVENANCE,
    accept_ledger,
    open_opening_week,
    payload_for,
)
from tests.test_game_log_preview import FakeRedis
from tests.test_published_season_reads import ScheduleCatalog

PLAYER_NAME = "Player 0"
PLAY_TYPE_DEFENSE = "synergy_play_types_opponent_season"
PLAYER_SYNERGY = "synergy_play_types"
#: A 2025-26 ATL @ BOS game, final: a 2026-27 selection for it is refused.
OLD_GAME_ID = "0022500900"
OLD_SLATE_DATE = "2026-03-10"


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


def _old_game_event():
    event = {
        **_game_event(),
        "nba_game_id": OLD_GAME_ID,
        "season": OLD,
        "scheduled_at": "2026-03-11T00:00:00+00:00",
        "status_text": "Final",
        "status_code": 3,
    }
    for side, team_id in (("away", ATL), ("home", BOS)):
        event[f"{side}_team"] = {
            "id": team_id,
            "name": f"{TRICODES[team_id]} team",
            "tricode": TRICODES[team_id],
        }
    return event


def _play_type_defense_payload(games):
    by_team = {}
    for game_id, home, away in games:
        by_team.setdefault(int(home), []).append(game_id)
        by_team.setdefault(int(away), []).append(game_id)
    metrics = dict.fromkeys(sorted(NBA_PUBLICATION_TAXONOMY["play_types"]), 1.0)
    return {
        "rows": [
            {
                "team_id": team_id,
                "team_tricode": TRICODES[team_id],
                "game_ids": sorted(game_ids),
                "game_count": len(game_ids),
                "per48": metrics,
            }
            for team_id, game_ids in sorted(by_team.items())
        ]
    }


def _player_synergy_payload(_games):
    slices = PLAYER_DIET_QUALIFIER_SLICES["play_types"]
    return {
        "base": "play_types",
        "rows": [
            {
                "player_id": player_id,
                "slice_key": slice_key,
                "share": 1 / len(slices),
                "volume": 100.0,
                "games_played": 10,
                "volume_unit": "possessions",
                "provider": "nba_synergy",
            }
            for player_id in (PLAYER_ID, PLAYER_ID + 1)
            for slice_key in slices
        ],
    }


_PLAY_TYPE_PAYLOADS = {PLAY_TYPE_DEFENSE: _play_type_defense_payload, PLAYER_SYNERGY: _player_synergy_payload}


class _NoPeers:
    def list_peer_ids(self, player_id, *, connection=None):
        return ()


class _StoredInjuries:
    """A stored-only injury reader holding no report."""

    def get_injuries(self, *, event, season, pool_players):
        return unavailable_injury_result("disabled")


class SelectedWorld(World):
    def __init__(self, tmp_path, *, engine=None):
        super().__init__(tmp_path, engine=engine)
        self.reader = self.service.publication_reader
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
            ScheduleCatalog({NEW: [_game_event()], OLD: [_old_game_event()]}),
            settings=settings(),
            clock=lambda: NOW,
            publication_reader=self.reader,
            matchup_snapshot=lambda reader: capture_matchup_snapshot(settings(), reader),
        )
        self.selections = MatchupSelectionService(
            event_catalog=self.service.event_catalog,
            player_pool=_Pool(),
            player_logs=self.logs,
            archetypes=_NoPeers(),
            statistic_catalog=StatisticCatalog.load_default(),
            settings=settings(),
            publication_reader=self.reader,
            engine=self.engine,
        )
        self.redis = FakeRedis()
        self.edges = SlateEdgesService(
            slates=self.slates,
            matchups=self.service,
            publication_reader=self.reader,
            injuries=_StoredInjuries(),
            settings=settings(),
            redis_client=self.redis,
            snapshot_reuse=PublicationSnapshotReuse(),
            matchup_cache=ComposedMatchupCache(self.redis),
            engine=self.engine,
        )
        self.profiles = PlayerService(
            self.engine,
            profile_reader=PlayerProfileReader(_Catalog(), self.service.player_diets),
            settings=settings(),
            publication_reader=self.reader,
        )
        self.baselines = DietBaselinesService(
            player_diets=self.service.player_diets, settings=settings(), publication_reader=self.reader
        )
        self.teams = TeamService(
            settings=settings(),
            publication_reader=self.reader,
            season_publications=TeamFilterRankingService(
                self.reader,
                governance_resolver=ActiveManifestLedgerGovernanceReader(self.engine, clock=lambda: NOW),
                clock=lambda: NOW,
            ),
        )

    def live(self, stream_key):
        """The publication the live pointer of ``stream_key`` names."""

        with self.engine.connect() as connection:
            return connection.scalar(
                text("SELECT active_publication_id FROM publication_pointers WHERE stream_key = :k"),
                {"k": stream_key},
            )

    def hold_opening_week(self, *, defense=True):
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
        if not defense:
            return logs, None
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
            payload=payload_for(SEASON_STREAM, NEW, games=games),
            provenance=provenance,
            reason="2026-27 ledger composition",
        )

    def publish_play_types(self):
        """Activate 2025-26 play-type defense and player Synergy.

        Both stand in as ledger compositions, like the fixture's other
        streams: a Readiness Rule reads only the stream key, so NBA's
        observation pipeline is left out.  An NBA stream's read verifies its
        manifest authority, which an active ledger composition does not bind,
        so each takes ``manifest-old``'s.
        """

        for stream_key in _PLAY_TYPE_PAYLOADS:
            self.publications.register_stream(
                stream_key,
                provider="ledger",
                owner="railway",
                required_observations=["canonical_game_ledger"],
                publication_strategy="ledger_compose",
                enabled=True,
            )
            self.publications.recompose_ledger(
                stream_key,
                season=OLD,
                cutoff=OLD_CUTOFF,
                payload=_PLAY_TYPE_PAYLOADS[stream_key](OLD_GAMES),
                provenance=OLD_PROVENANCE,
                reason="2025-26 ledger composition",
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE publication_versions SET manifest_id = m.manifest_id,"
                    " event_catalog_publication_id = m.event_catalog_publication_id,"
                    " event_catalog_checksum = m.event_catalog_checksum"
                    " FROM collection_manifests AS m"
                    " WHERE m.manifest_id = 'manifest-old' AND publication_versions.stream_key IN (:a, :b)"
                ),
                {"a": PLAY_TYPE_DEFENSE, "b": PLAYER_SYNERGY},
            )

    def compose_new_play_types(self, stream_key, games, *, cutoff, provenance):
        return self.publications.recompose_ledger(
            stream_key,
            season=NEW,
            cutoff=cutoff,
            payload=_PLAY_TYPE_PAYLOADS[stream_key](games),
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


__all__ = [
    "GAME_ID",
    "NEW",
    "OLD_GAME_ID",
    "OLD_SLATE_DATE",
    "PLAYER_ID",
    "PLAYER_NAME",
    "PLAYER_SYNERGY",
    "PLAY_TYPE_DEFENSE",
    "SelectedWorld",
    "settings",
]
