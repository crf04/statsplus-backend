"""Offline persisted-fixture proof for the complete matchup endpoint."""

from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select

from app import create_app
from app.dependencies import build_dependencies
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    FeatureSettings,
    NBASeasonSettings,
    ProviderSettings,
    RuntimeSettings,
)
from app.migrations import run_migrations
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationObservation,
    PublicationVersion,
)
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    LedgerObservationEvidence,
    LedgerParityArtifact,
)
from app.models.projection_archive import (
    LatestPlayerProjection,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    raw_rows_from_facts,
)
from app.providers.rotowire import InjuryEntryEvidence, InjuryProviderSnapshot
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.event_catalog_service import EventCatalogService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
)
from app.services.collection_control import CollectionOperationsService, PublicationService
from app.services.matchup import MatchupService
from app.services.matchup_selection import MatchupSelectionService
from app.services.game_service import GameService
from app.services.game_logs_source import StoredGameLogsSource
from app.services.ledger_materialization import LedgerMaterializationService
from app.services.ledger_matchup_materialization import (
    LedgerMatchupMaterializationService,
)
from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.matchup_parity import MatchupParityRunner, StoredLegacyMatchupSource
from app.services.player_archetype_repository import PlayerArchetypeRepository
from app.services.injury_snapshot_repository import InjurySnapshotRepository
from app.services.matchup_injuries import MatchupInjuryService
from app.services.player_diet import (
    PlayerDietFact,
    PlayerDietObservation,
    PlayerDietService,
)
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)
from app.services.player_pool import StoredPlayerPoolReader
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionRecordingService,
    ProjectionSelectionPlayerPoolReader,
)
from app.services.player_pool_snapshot_repository import (
    PlayerPoolSnapshotRepository,
    PlayerPoolSnapshotScope,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshnessRepository
from app.services.slate_service import SlateService
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_refresh import (
    TeamMatchupProvenance,
    TeamMatchupRefreshService,
)
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository as _ProductionTeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from tests.services.test_ledger_derivations import _league_games
from tests.services.test_matchup_parity import (
    _runner_world,
)
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE


TEAM_FIXTURE = Path(__file__).parents[1] / "fixtures/team_matchups/thirty_teams.json"
NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738
COMMON_POSTED_MARKETS = (
    "PTS",
    "REB",
    "AST",
    "3PM",
    "FGA",
    "FG2A",
    "FG3A",
    "PRA",
    "PA",
    "PR",
    "RA",
    "TOV",
    "STL",
    "BLK",
    "STKS",
)


class _AllowFixtureLegacyWrites:
    def assert_writable(self, stream_key, *, connection=None):
        return None


class TeamMatchupRepository(_ProductionTeamMatchupRepository):
    """Route fixtures explicitly opt into legacy writes before activation."""

    def __init__(self, engine, **kwargs):
        kwargs.setdefault("write_fence", _AllowFixtureLegacyWrites())
        super().__init__(engine, **kwargs)


def _recorded_projection_snapshot(catalog, *, provider="dabble"):
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(
        provider_id="pts",
        canonical_id=statistic.id,
        label="Points",
        components=statistic.components,
    )
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=(
            PlayerProjectionMarket(
                provider=provider,
                market_id="fixture-market-points",
                athlete=AthleteEvidence(
                    provider_id="fixture-lebron",
                    canonical_id=2544,
                    name="LeBron James",
                    team=TeamEvidence(canonical_id=LAL, abbreviation="LAL"),
                ),
                event=EventEvidence(
                    provider_id="fixture-event",
                    canonical_id=GAME_ID,
                ),
                team=TeamEvidence(canonical_id=LAL, abbreviation="LAL"),
                statistic=evidence,
                statistic_match=StatisticMatch(
                    state=MatchState.CANONICAL,
                    evidence=evidence,
                    scoring_period=ScoringPeriod.FULL_GAME,
                    canonical=statistic,
                    provider=provider,
                ),
                status=MarketStatus.AVAILABLE,
                variant=MarketVariant.STANDARD,
                scoring_period=ScoringPeriod.FULL_GAME,
            ),
        ),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=1,
        ),
        retrieved_at=NOW,
    )


def _source_independent_matchup_contract(response):
    """Compare documented source-independent fields.

    Provenance and additive coverage are source metadata; the journey asserts
    those envelopes explicitly at each side of the transition before excluding
    them from the byte-compatible contract.
    """

    payload = json.loads(response.data)
    payload.pop("provenance", None)
    payload.pop("coverage", None)
    return payload


class _NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"request-time provider access is forbidden: {name}")


def _route_ledger_games(governance):
    """Re-key complete production-shaped ledger fixtures to catalog IDs."""
    source_games = _league_games()
    events = governance.events
    assert len(source_games) == len(events)
    provider_values = {
        int(row["team_id"]): int(row["allowed"])
        for row in json.loads(TEAM_FIXTURE.read_text(encoding="utf-8"))
    }
    games = []
    for source, event in zip(source_games, events):
        team_map = {
            source.home_team_id: int(event["home_team_id"]),
            source.away_team_id: int(event["away_team_id"]),
        }
        tricode_map = {
            team_id: NBA_TEAM_ID_TO_TRICODE[actual_id]
            for team_id, actual_id in team_map.items()
        }
        team_facts = tuple(
            replace(
                fact,
                team_id=team_map[fact.team_id],
                team_tricode=tricode_map[fact.team_id],
                opponent_team_id=team_map[fact.opponent_team_id],
                opponent_team_tricode=tricode_map[fact.opponent_team_id],
                offensive_rebounds=provider_values[team_map[fact.opponent_team_id]],
                defensive_rebounds=0,
                rebounds=provider_values[team_map[fact.opponent_team_id]],
                turnovers=provider_values[team_map[fact.opponent_team_id]],
                steals=provider_values[team_map[fact.opponent_team_id]],
                blocks=provider_values[team_map[fact.opponent_team_id]],
                assists=provider_values[team_map[fact.opponent_team_id]] * 5,
            )
            for fact in source.team_facts
        )
        assist_value_by_team = {
            team_fact.team_id: provider_values[team_fact.opponent_team_id]
            for team_fact in team_facts
        }
        assist_total_by_team = {
            team_id: value * 5 for team_id, value in assist_value_by_team.items()
        }
        player_facts = tuple(
            replace(
                fact,
                team_id=team_map[fact.team_id],
                team_tricode=tricode_map[fact.team_id],
                offensive_rebounds=0,
                defensive_rebounds=0,
                rebounds=0,
                turnovers=0,
                steals=0,
                blocks=0,
                assists=assist_total_by_team[team_map[fact.team_id]],
                two_point_assists=0,
                three_point_assists=0,
                arc3_assists=assist_value_by_team[team_map[fact.team_id]],
                corner3_assists=assist_value_by_team[team_map[fact.team_id]],
                at_rim_assists=assist_value_by_team[team_map[fact.team_id]],
                short_mid_range_assists=assist_value_by_team[team_map[fact.team_id]],
                long_mid_range_assists=assist_value_by_team[team_map[fact.team_id]],
            )
            for fact in source.player_facts
        )
        participant_ids = tuple(
            (team_map[team_id], player_ids)
            for team_id, player_ids in source.participant_ids_by_team
        )
        candidate = replace(
            source,
            game_id=str(event["nba_game_id"]),
            game_date=event["scheduled_at"].date(),
            home_team_id=team_map[source.home_team_id],
            home_team_tricode=tricode_map[source.home_team_id],
            away_team_id=team_map[source.away_team_id],
            away_team_tricode=tricode_map[source.away_team_id],
            team_facts=team_facts,
            player_facts=player_facts,
            source_observation_id=f"route-ledger:{event['nba_game_id']}",
            participant_ids_by_team=participant_ids,
        )
        candidate = replace(candidate, raw_rows=raw_rows_from_facts(candidate))
        games.append(candidate.with_checksum())
    return tuple(games)


class _RecordedLegacyCatalog:
    def __init__(self, events):
        self.events = tuple(events)

    def get_events(self, season):
        assert season == SEASON
        return list(self.events)


# Immutable provider captures.  These literals are independently authored
# response evidence: they are not derived from the Event Catalog, ledger
# CanonicalGame rows, or the route's expected-value fixture.
_RECORDED_NBA_MEMBERSHIP = {
    1610612737: ("game-00-00", "game-01-00", "game-02-00", "game-03-00", "game-04-00", "game-05-00", "game-06-00", "game-07-00", "game-08-00", "game-09-00", "game-10-00", "game-11-00", "game-12-00", "game-13-00", "game-14-00"),
    1610612738: ("game-00-01", "game-01-02", "game-02-03", "game-03-04", "game-04-05", "game-05-06", "game-06-07", "game-07-08", "game-08-09", "game-09-10", "game-10-11", "game-11-12", "game-12-13", "game-13-14", "game-14-14"),
    1610612739: ("game-00-02", "game-01-03", "game-02-04", "game-03-05", "game-04-06", "game-05-07", "game-06-08", "game-07-09", "game-08-10", "game-09-11", "game-10-12", "game-11-13", "game-12-14", "game-13-14", "game-14-13"),
    1610612740: ("game-00-03", "game-01-04", "game-02-05", "game-03-06", "game-04-07", "game-05-08", "game-06-09", "game-07-10", "game-08-11", "game-09-12", "game-10-13", "game-11-14", "game-12-14", "game-13-13", "game-14-12"),
    1610612741: ("game-00-04", "game-01-05", "game-02-06", "game-03-07", "game-04-08", "game-05-09", "game-06-10", "game-07-11", "game-08-12", "game-09-13", "game-10-14", "game-11-14", "game-12-13", "game-13-12", "game-14-11"),
    1610612742: ("game-00-05", "game-01-06", "game-02-07", "game-03-08", "game-04-09", "game-05-10", "game-06-11", "game-07-12", "game-08-13", "game-09-14", "game-10-14", "game-11-13", "game-12-12", "game-13-11", "game-14-10"),
    1610612743: ("game-00-06", "game-01-07", "game-02-08", "game-03-09", "game-04-10", "game-05-11", "game-06-12", "game-07-13", "game-08-14", "game-09-14", "game-10-13", "game-11-12", "game-12-11", "game-13-10", "game-14-09"),
    1610612744: ("game-00-07", "game-01-08", "game-02-09", "game-03-10", "game-04-11", "game-05-12", "game-06-13", "game-07-14", "game-08-14", "game-09-13", "game-10-12", "game-11-11", "game-12-10", "game-13-09", "game-14-08"),
    1610612745: ("game-00-08", "game-01-09", "game-02-10", "game-03-11", "game-04-12", "game-05-13", "game-06-14", "game-07-14", "game-08-13", "game-09-12", "game-10-11", "game-11-10", "game-12-09", "game-13-08", "game-14-07"),
    1610612746: ("game-00-09", "game-01-10", "game-02-11", "game-03-12", "game-04-13", "game-05-14", "game-06-14", "game-07-13", "game-08-12", "game-09-11", "game-10-10", "game-11-09", "game-12-08", "game-13-07", "game-14-06"),
    1610612747: ("game-00-10", "game-01-11", "game-02-12", "game-03-13", "game-04-14", "game-05-14", "game-06-13", "game-07-12", "game-08-11", "game-09-10", "game-10-09", "game-11-08", "game-12-07", "game-13-06", "game-14-05"),
    1610612748: ("game-00-11", "game-01-12", "game-02-13", "game-03-14", "game-04-14", "game-05-13", "game-06-12", "game-07-11", "game-08-10", "game-09-09", "game-10-08", "game-11-07", "game-12-06", "game-13-05", "game-14-04"),
    1610612749: ("game-00-12", "game-01-13", "game-02-14", "game-03-14", "game-04-13", "game-05-12", "game-06-11", "game-07-10", "game-08-09", "game-09-08", "game-10-07", "game-11-06", "game-12-05", "game-13-04", "game-14-03"),
    1610612750: ("game-00-13", "game-01-14", "game-02-14", "game-03-13", "game-04-12", "game-05-11", "game-06-10", "game-07-09", "game-08-08", "game-09-07", "game-10-06", "game-11-05", "game-12-04", "game-13-03", "game-14-02"),
    1610612751: ("game-00-14", "game-01-14", "game-02-13", "game-03-12", "game-04-11", "game-05-10", "game-06-09", "game-07-08", "game-08-07", "game-09-06", "game-10-05", "game-11-04", "game-12-03", "game-13-02", "game-14-01"),
    1610612752: ("game-00-14", "game-01-13", "game-02-12", "game-03-11", "game-04-10", "game-05-09", "game-06-08", "game-07-07", "game-08-06", "game-09-05", "game-10-04", "game-11-03", "game-12-02", "game-13-01", "game-14-00"),
    1610612753: ("game-00-13", "game-01-12", "game-02-11", "game-03-10", "game-04-09", "game-05-08", "game-06-07", "game-07-06", "game-08-05", "game-09-04", "game-10-03", "game-11-02", "game-12-01", "game-13-00", "game-14-01"),
    1610612754: ("game-00-12", "game-01-11", "game-02-10", "game-03-09", "game-04-08", "game-05-07", "game-06-06", "game-07-05", "game-08-04", "game-09-03", "game-10-02", "game-11-01", "game-12-00", "game-13-01", "game-14-02"),
    1610612755: ("game-00-11", "game-01-10", "game-02-09", "game-03-08", "game-04-07", "game-05-06", "game-06-05", "game-07-04", "game-08-03", "game-09-02", "game-10-01", "game-11-00", "game-12-01", "game-13-02", "game-14-03"),
    1610612756: ("game-00-10", "game-01-09", "game-02-08", "game-03-07", "game-04-06", "game-05-05", "game-06-04", "game-07-03", "game-08-02", "game-09-01", "game-10-00", "game-11-01", "game-12-02", "game-13-03", "game-14-04"),
    1610612757: ("game-00-09", "game-01-08", "game-02-07", "game-03-06", "game-04-05", "game-05-04", "game-06-03", "game-07-02", "game-08-01", "game-09-00", "game-10-01", "game-11-02", "game-12-03", "game-13-04", "game-14-05"),
    1610612758: ("game-00-08", "game-01-07", "game-02-06", "game-03-05", "game-04-04", "game-05-03", "game-06-02", "game-07-01", "game-08-00", "game-09-01", "game-10-02", "game-11-03", "game-12-04", "game-13-05", "game-14-06"),
    1610612759: ("game-00-07", "game-01-06", "game-02-05", "game-03-04", "game-04-03", "game-05-02", "game-06-01", "game-07-00", "game-08-01", "game-09-02", "game-10-03", "game-11-04", "game-12-05", "game-13-06", "game-14-07"),
    1610612760: ("game-00-06", "game-01-05", "game-02-04", "game-03-03", "game-04-02", "game-05-01", "game-06-00", "game-07-01", "game-08-02", "game-09-03", "game-10-04", "game-11-05", "game-12-06", "game-13-07", "game-14-08"),
    1610612761: ("game-00-05", "game-01-04", "game-02-03", "game-03-02", "game-04-01", "game-05-00", "game-06-01", "game-07-02", "game-08-03", "game-09-04", "game-10-05", "game-11-06", "game-12-07", "game-13-08", "game-14-09"),
    1610612762: ("game-00-04", "game-01-03", "game-02-02", "game-03-01", "game-04-00", "game-05-01", "game-06-02", "game-07-03", "game-08-04", "game-09-05", "game-10-06", "game-11-07", "game-12-08", "game-13-09", "game-14-10"),
    1610612763: ("game-00-03", "game-01-02", "game-02-01", "game-03-00", "game-04-01", "game-05-02", "game-06-03", "game-07-04", "game-08-05", "game-09-06", "game-10-07", "game-11-08", "game-12-09", "game-13-10", "game-14-11"),
    1610612764: ("game-00-02", "game-01-01", "game-02-00", "game-03-01", "game-04-02", "game-05-03", "game-06-04", "game-07-05", "game-08-06", "game-09-07", "game-10-08", "game-11-09", "game-12-10", "game-13-11", "game-14-12"),
    1610612765: ("game-00-01", "game-01-00", "game-02-01", "game-03-02", "game-04-03", "game-05-04", "game-06-05", "game-07-06", "game-08-07", "game-09-08", "game-10-09", "game-11-10", "game-12-11", "game-13-12", "game-14-13"),
    1610612766: ("game-00-00", "game-01-01", "game-02-02", "game-03-03", "game-04-04", "game-05-05", "game-06-06", "game-07-07", "game-08-08", "game-09-09", "game-10-10", "game-11-11", "game-12-12", "game-13-13", "game-14-14"),
}

_RECORDED_PBP_MEMBERSHIP = {
    1610612737: ("game-00-00", "game-01-00", "game-02-00", "game-03-00", "game-04-00", "game-05-00", "game-06-00", "game-07-00", "game-08-00", "game-09-00", "game-10-00", "game-11-00", "game-12-00", "game-13-00", "game-14-00"),
    1610612738: ("game-00-01", "game-01-02", "game-02-03", "game-03-04", "game-04-05", "game-05-06", "game-06-07", "game-07-08", "game-08-09", "game-09-10", "game-10-11", "game-11-12", "game-12-13", "game-13-14", "game-14-14"),
    1610612739: ("game-00-02", "game-01-03", "game-02-04", "game-03-05", "game-04-06", "game-05-07", "game-06-08", "game-07-09", "game-08-10", "game-09-11", "game-10-12", "game-11-13", "game-12-14", "game-13-14", "game-14-13"),
    1610612740: ("game-00-03", "game-01-04", "game-02-05", "game-03-06", "game-04-07", "game-05-08", "game-06-09", "game-07-10", "game-08-11", "game-09-12", "game-10-13", "game-11-14", "game-12-14", "game-13-13", "game-14-12"),
    1610612741: ("game-00-04", "game-01-05", "game-02-06", "game-03-07", "game-04-08", "game-05-09", "game-06-10", "game-07-11", "game-08-12", "game-09-13", "game-10-14", "game-11-14", "game-12-13", "game-13-12", "game-14-11"),
    1610612742: ("game-00-05", "game-01-06", "game-02-07", "game-03-08", "game-04-09", "game-05-10", "game-06-11", "game-07-12", "game-08-13", "game-09-14", "game-10-14", "game-11-13", "game-12-12", "game-13-11", "game-14-10"),
    1610612743: ("game-00-06", "game-01-07", "game-02-08", "game-03-09", "game-04-10", "game-05-11", "game-06-12", "game-07-13", "game-08-14", "game-09-14", "game-10-13", "game-11-12", "game-12-11", "game-13-10", "game-14-09"),
    1610612744: ("game-00-07", "game-01-08", "game-02-09", "game-03-10", "game-04-11", "game-05-12", "game-06-13", "game-07-14", "game-08-14", "game-09-13", "game-10-12", "game-11-11", "game-12-10", "game-13-09", "game-14-08"),
    1610612745: ("game-00-08", "game-01-09", "game-02-10", "game-03-11", "game-04-12", "game-05-13", "game-06-14", "game-07-14", "game-08-13", "game-09-12", "game-10-11", "game-11-10", "game-12-09", "game-13-08", "game-14-07"),
    1610612746: ("game-00-09", "game-01-10", "game-02-11", "game-03-12", "game-04-13", "game-05-14", "game-06-14", "game-07-13", "game-08-12", "game-09-11", "game-10-10", "game-11-09", "game-12-08", "game-13-07", "game-14-06"),
    1610612747: ("game-00-10", "game-01-11", "game-02-12", "game-03-13", "game-04-14", "game-05-14", "game-06-13", "game-07-12", "game-08-11", "game-09-10", "game-10-09", "game-11-08", "game-12-07", "game-13-06", "game-14-05"),
    1610612748: ("game-00-11", "game-01-12", "game-02-13", "game-03-14", "game-04-14", "game-05-13", "game-06-12", "game-07-11", "game-08-10", "game-09-09", "game-10-08", "game-11-07", "game-12-06", "game-13-05", "game-14-04"),
    1610612749: ("game-00-12", "game-01-13", "game-02-14", "game-03-14", "game-04-13", "game-05-12", "game-06-11", "game-07-10", "game-08-09", "game-09-08", "game-10-07", "game-11-06", "game-12-05", "game-13-04", "game-14-03"),
    1610612750: ("game-00-13", "game-01-14", "game-02-14", "game-03-13", "game-04-12", "game-05-11", "game-06-10", "game-07-09", "game-08-08", "game-09-07", "game-10-06", "game-11-05", "game-12-04", "game-13-03", "game-14-02"),
    1610612751: ("game-00-14", "game-01-14", "game-02-13", "game-03-12", "game-04-11", "game-05-10", "game-06-09", "game-07-08", "game-08-07", "game-09-06", "game-10-05", "game-11-04", "game-12-03", "game-13-02", "game-14-01"),
    1610612752: ("game-00-14", "game-01-13", "game-02-12", "game-03-11", "game-04-10", "game-05-09", "game-06-08", "game-07-07", "game-08-06", "game-09-05", "game-10-04", "game-11-03", "game-12-02", "game-13-01", "game-14-00"),
    1610612753: ("game-00-13", "game-01-12", "game-02-11", "game-03-10", "game-04-09", "game-05-08", "game-06-07", "game-07-06", "game-08-05", "game-09-04", "game-10-03", "game-11-02", "game-12-01", "game-13-00", "game-14-01"),
    1610612754: ("game-00-12", "game-01-11", "game-02-10", "game-03-09", "game-04-08", "game-05-07", "game-06-06", "game-07-05", "game-08-04", "game-09-03", "game-10-02", "game-11-01", "game-12-00", "game-13-01", "game-14-02"),
    1610612755: ("game-00-11", "game-01-10", "game-02-09", "game-03-08", "game-04-07", "game-05-06", "game-06-05", "game-07-04", "game-08-03", "game-09-02", "game-10-01", "game-11-00", "game-12-01", "game-13-02", "game-14-03"),
    1610612756: ("game-00-10", "game-01-09", "game-02-08", "game-03-07", "game-04-06", "game-05-05", "game-06-04", "game-07-03", "game-08-02", "game-09-01", "game-10-00", "game-11-01", "game-12-02", "game-13-03", "game-14-04"),
    1610612757: ("game-00-09", "game-01-08", "game-02-07", "game-03-06", "game-04-05", "game-05-04", "game-06-03", "game-07-02", "game-08-01", "game-09-00", "game-10-01", "game-11-02", "game-12-03", "game-13-04", "game-14-05"),
    1610612758: ("game-00-08", "game-01-07", "game-02-06", "game-03-05", "game-04-04", "game-05-03", "game-06-02", "game-07-01", "game-08-00", "game-09-01", "game-10-02", "game-11-03", "game-12-04", "game-13-05", "game-14-06"),
    1610612759: ("game-00-07", "game-01-06", "game-02-05", "game-03-04", "game-04-03", "game-05-02", "game-06-01", "game-07-00", "game-08-01", "game-09-02", "game-10-03", "game-11-04", "game-12-05", "game-13-06", "game-14-07"),
    1610612760: ("game-00-06", "game-01-05", "game-02-04", "game-03-03", "game-04-02", "game-05-01", "game-06-00", "game-07-01", "game-08-02", "game-09-03", "game-10-04", "game-11-05", "game-12-06", "game-13-07", "game-14-08"),
    1610612761: ("game-00-05", "game-01-04", "game-02-03", "game-03-02", "game-04-01", "game-05-00", "game-06-01", "game-07-02", "game-08-03", "game-09-04", "game-10-05", "game-11-06", "game-12-07", "game-13-08", "game-14-09"),
    1610612762: ("game-00-04", "game-01-03", "game-02-02", "game-03-01", "game-04-00", "game-05-01", "game-06-02", "game-07-03", "game-08-04", "game-09-05", "game-10-06", "game-11-07", "game-12-08", "game-13-09", "game-14-10"),
    1610612763: ("game-00-03", "game-01-02", "game-02-01", "game-03-00", "game-04-01", "game-05-02", "game-06-03", "game-07-04", "game-08-05", "game-09-06", "game-10-07", "game-11-08", "game-12-09", "game-13-10", "game-14-11"),
    1610612764: ("game-00-02", "game-01-01", "game-02-00", "game-03-01", "game-04-02", "game-05-03", "game-06-04", "game-07-05", "game-08-06", "game-09-07", "game-10-08", "game-11-09", "game-12-10", "game-13-11", "game-14-12"),
    1610612765: ("game-00-01", "game-01-00", "game-02-01", "game-03-02", "game-04-03", "game-05-04", "game-06-05", "game-07-06", "game-08-07", "game-09-08", "game-10-09", "game-11-10", "game-12-11", "game-13-12", "game-14-13"),
    1610612766: ("game-00-00", "game-01-01", "game-02-02", "game-03-03", "game-04-04", "game-05-05", "game-06-06", "game-07-07", "game-08-08", "game-09-09", "game-10-10", "game-11-11", "game-12-12", "game-13-13", "game-14-14"),
}

_RECORDED_NBA_AGGREGATES = {
    1610612737: {"TEAM_ID": 1610612737, "TEAM_NAME": "Team 1610612737", "GP": 15, "MIN": 720, "OPP_REB": 15, "OPP_TOV": 15, "OPP_STL": 15, "OPP_BLK": 15},
    1610612738: {"TEAM_ID": 1610612738, "TEAM_NAME": "Team 1610612738", "GP": 15, "MIN": 720, "OPP_REB": 30, "OPP_TOV": 30, "OPP_STL": 30, "OPP_BLK": 30},
    1610612739: {"TEAM_ID": 1610612739, "TEAM_NAME": "Team 1610612739", "GP": 15, "MIN": 720, "OPP_REB": 45, "OPP_TOV": 45, "OPP_STL": 45, "OPP_BLK": 45},
    1610612740: {"TEAM_ID": 1610612740, "TEAM_NAME": "Team 1610612740", "GP": 15, "MIN": 720, "OPP_REB": 60, "OPP_TOV": 60, "OPP_STL": 60, "OPP_BLK": 60},
    1610612741: {"TEAM_ID": 1610612741, "TEAM_NAME": "Team 1610612741", "GP": 15, "MIN": 720, "OPP_REB": 75, "OPP_TOV": 75, "OPP_STL": 75, "OPP_BLK": 75},
    1610612742: {"TEAM_ID": 1610612742, "TEAM_NAME": "Team 1610612742", "GP": 15, "MIN": 720, "OPP_REB": 90, "OPP_TOV": 90, "OPP_STL": 90, "OPP_BLK": 90},
    1610612743: {"TEAM_ID": 1610612743, "TEAM_NAME": "Team 1610612743", "GP": 15, "MIN": 720, "OPP_REB": 105, "OPP_TOV": 105, "OPP_STL": 105, "OPP_BLK": 105},
    1610612744: {"TEAM_ID": 1610612744, "TEAM_NAME": "Team 1610612744", "GP": 15, "MIN": 720, "OPP_REB": 120, "OPP_TOV": 120, "OPP_STL": 120, "OPP_BLK": 120},
    1610612745: {"TEAM_ID": 1610612745, "TEAM_NAME": "Team 1610612745", "GP": 15, "MIN": 720, "OPP_REB": 135, "OPP_TOV": 135, "OPP_STL": 135, "OPP_BLK": 135},
    1610612746: {"TEAM_ID": 1610612746, "TEAM_NAME": "Team 1610612746", "GP": 15, "MIN": 720, "OPP_REB": 150, "OPP_TOV": 150, "OPP_STL": 150, "OPP_BLK": 150},
    1610612747: {"TEAM_ID": 1610612747, "TEAM_NAME": "Team 1610612747", "GP": 15, "MIN": 720, "OPP_REB": 165, "OPP_TOV": 165, "OPP_STL": 165, "OPP_BLK": 165},
    1610612748: {"TEAM_ID": 1610612748, "TEAM_NAME": "Team 1610612748", "GP": 15, "MIN": 720, "OPP_REB": 180, "OPP_TOV": 180, "OPP_STL": 180, "OPP_BLK": 180},
    1610612749: {"TEAM_ID": 1610612749, "TEAM_NAME": "Team 1610612749", "GP": 15, "MIN": 720, "OPP_REB": 195, "OPP_TOV": 195, "OPP_STL": 195, "OPP_BLK": 195},
    1610612750: {"TEAM_ID": 1610612750, "TEAM_NAME": "Team 1610612750", "GP": 15, "MIN": 720, "OPP_REB": 210, "OPP_TOV": 210, "OPP_STL": 210, "OPP_BLK": 210},
    1610612751: {"TEAM_ID": 1610612751, "TEAM_NAME": "Team 1610612751", "GP": 15, "MIN": 720, "OPP_REB": 225, "OPP_TOV": 225, "OPP_STL": 225, "OPP_BLK": 225},
    1610612752: {"TEAM_ID": 1610612752, "TEAM_NAME": "Team 1610612752", "GP": 15, "MIN": 720, "OPP_REB": 240, "OPP_TOV": 240, "OPP_STL": 240, "OPP_BLK": 240},
    1610612753: {"TEAM_ID": 1610612753, "TEAM_NAME": "Team 1610612753", "GP": 15, "MIN": 720, "OPP_REB": 255, "OPP_TOV": 255, "OPP_STL": 255, "OPP_BLK": 255},
    1610612754: {"TEAM_ID": 1610612754, "TEAM_NAME": "Team 1610612754", "GP": 15, "MIN": 720, "OPP_REB": 270, "OPP_TOV": 270, "OPP_STL": 270, "OPP_BLK": 270},
    1610612755: {"TEAM_ID": 1610612755, "TEAM_NAME": "Team 1610612755", "GP": 15, "MIN": 720, "OPP_REB": 285, "OPP_TOV": 285, "OPP_STL": 285, "OPP_BLK": 285},
    1610612756: {"TEAM_ID": 1610612756, "TEAM_NAME": "Team 1610612756", "GP": 15, "MIN": 720, "OPP_REB": 300, "OPP_TOV": 300, "OPP_STL": 300, "OPP_BLK": 300},
    1610612757: {"TEAM_ID": 1610612757, "TEAM_NAME": "Team 1610612757", "GP": 15, "MIN": 720, "OPP_REB": 315, "OPP_TOV": 315, "OPP_STL": 315, "OPP_BLK": 315},
    1610612758: {"TEAM_ID": 1610612758, "TEAM_NAME": "Team 1610612758", "GP": 15, "MIN": 720, "OPP_REB": 330, "OPP_TOV": 330, "OPP_STL": 330, "OPP_BLK": 330},
    1610612759: {"TEAM_ID": 1610612759, "TEAM_NAME": "Team 1610612759", "GP": 15, "MIN": 720, "OPP_REB": 345, "OPP_TOV": 345, "OPP_STL": 345, "OPP_BLK": 345},
    1610612760: {"TEAM_ID": 1610612760, "TEAM_NAME": "Team 1610612760", "GP": 15, "MIN": 720, "OPP_REB": 360, "OPP_TOV": 360, "OPP_STL": 360, "OPP_BLK": 360},
    1610612761: {"TEAM_ID": 1610612761, "TEAM_NAME": "Team 1610612761", "GP": 15, "MIN": 720, "OPP_REB": 375, "OPP_TOV": 375, "OPP_STL": 375, "OPP_BLK": 375},
    1610612762: {"TEAM_ID": 1610612762, "TEAM_NAME": "Team 1610612762", "GP": 15, "MIN": 720, "OPP_REB": 390, "OPP_TOV": 390, "OPP_STL": 390, "OPP_BLK": 390},
    1610612763: {"TEAM_ID": 1610612763, "TEAM_NAME": "Team 1610612763", "GP": 15, "MIN": 720, "OPP_REB": 405, "OPP_TOV": 405, "OPP_STL": 405, "OPP_BLK": 405},
    1610612764: {"TEAM_ID": 1610612764, "TEAM_NAME": "Team 1610612764", "GP": 15, "MIN": 720, "OPP_REB": 420, "OPP_TOV": 420, "OPP_STL": 420, "OPP_BLK": 420},
    1610612765: {"TEAM_ID": 1610612765, "TEAM_NAME": "Team 1610612765", "GP": 15, "MIN": 720, "OPP_REB": 435, "OPP_TOV": 435, "OPP_STL": 435, "OPP_BLK": 435},
    1610612766: {"TEAM_ID": 1610612766, "TEAM_NAME": "Team 1610612766", "GP": 15, "MIN": 720, "OPP_REB": 450, "OPP_TOV": 450, "OPP_STL": 450, "OPP_BLK": 450},
}

_RECORDED_PBP_AGGREGATES = {
    1610612737: {"TeamId": 1610612737, "Name": "Team 1610612737", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 75, "Arc3Assists": 15, "Corner3Assists": 15, "AtRimAssists": 15, "ShortMidRangeAssists": 15, "LongMidRangeAssists": 15},
    1610612738: {"TeamId": 1610612738, "Name": "Team 1610612738", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 150, "Arc3Assists": 30, "Corner3Assists": 30, "AtRimAssists": 30, "ShortMidRangeAssists": 30, "LongMidRangeAssists": 30},
    1610612739: {"TeamId": 1610612739, "Name": "Team 1610612739", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 225, "Arc3Assists": 45, "Corner3Assists": 45, "AtRimAssists": 45, "ShortMidRangeAssists": 45, "LongMidRangeAssists": 45},
    1610612740: {"TeamId": 1610612740, "Name": "Team 1610612740", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 300, "Arc3Assists": 60, "Corner3Assists": 60, "AtRimAssists": 60, "ShortMidRangeAssists": 60, "LongMidRangeAssists": 60},
    1610612741: {"TeamId": 1610612741, "Name": "Team 1610612741", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 375, "Arc3Assists": 75, "Corner3Assists": 75, "AtRimAssists": 75, "ShortMidRangeAssists": 75, "LongMidRangeAssists": 75},
    1610612742: {"TeamId": 1610612742, "Name": "Team 1610612742", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 450, "Arc3Assists": 90, "Corner3Assists": 90, "AtRimAssists": 90, "ShortMidRangeAssists": 90, "LongMidRangeAssists": 90},
    1610612743: {"TeamId": 1610612743, "Name": "Team 1610612743", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 525, "Arc3Assists": 105, "Corner3Assists": 105, "AtRimAssists": 105, "ShortMidRangeAssists": 105, "LongMidRangeAssists": 105},
    1610612744: {"TeamId": 1610612744, "Name": "Team 1610612744", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 600, "Arc3Assists": 120, "Corner3Assists": 120, "AtRimAssists": 120, "ShortMidRangeAssists": 120, "LongMidRangeAssists": 120},
    1610612745: {"TeamId": 1610612745, "Name": "Team 1610612745", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 675, "Arc3Assists": 135, "Corner3Assists": 135, "AtRimAssists": 135, "ShortMidRangeAssists": 135, "LongMidRangeAssists": 135},
    1610612746: {"TeamId": 1610612746, "Name": "Team 1610612746", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 750, "Arc3Assists": 150, "Corner3Assists": 150, "AtRimAssists": 150, "ShortMidRangeAssists": 150, "LongMidRangeAssists": 150},
    1610612747: {"TeamId": 1610612747, "Name": "Team 1610612747", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 825, "Arc3Assists": 165, "Corner3Assists": 165, "AtRimAssists": 165, "ShortMidRangeAssists": 165, "LongMidRangeAssists": 165},
    1610612748: {"TeamId": 1610612748, "Name": "Team 1610612748", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 900, "Arc3Assists": 180, "Corner3Assists": 180, "AtRimAssists": 180, "ShortMidRangeAssists": 180, "LongMidRangeAssists": 180},
    1610612749: {"TeamId": 1610612749, "Name": "Team 1610612749", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 975, "Arc3Assists": 195, "Corner3Assists": 195, "AtRimAssists": 195, "ShortMidRangeAssists": 195, "LongMidRangeAssists": 195},
    1610612750: {"TeamId": 1610612750, "Name": "Team 1610612750", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1050, "Arc3Assists": 210, "Corner3Assists": 210, "AtRimAssists": 210, "ShortMidRangeAssists": 210, "LongMidRangeAssists": 210},
    1610612751: {"TeamId": 1610612751, "Name": "Team 1610612751", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1125, "Arc3Assists": 225, "Corner3Assists": 225, "AtRimAssists": 225, "ShortMidRangeAssists": 225, "LongMidRangeAssists": 225},
    1610612752: {"TeamId": 1610612752, "Name": "Team 1610612752", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1200, "Arc3Assists": 240, "Corner3Assists": 240, "AtRimAssists": 240, "ShortMidRangeAssists": 240, "LongMidRangeAssists": 240},
    1610612753: {"TeamId": 1610612753, "Name": "Team 1610612753", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1275, "Arc3Assists": 255, "Corner3Assists": 255, "AtRimAssists": 255, "ShortMidRangeAssists": 255, "LongMidRangeAssists": 255},
    1610612754: {"TeamId": 1610612754, "Name": "Team 1610612754", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1350, "Arc3Assists": 270, "Corner3Assists": 270, "AtRimAssists": 270, "ShortMidRangeAssists": 270, "LongMidRangeAssists": 270},
    1610612755: {"TeamId": 1610612755, "Name": "Team 1610612755", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1425, "Arc3Assists": 285, "Corner3Assists": 285, "AtRimAssists": 285, "ShortMidRangeAssists": 285, "LongMidRangeAssists": 285},
    1610612756: {"TeamId": 1610612756, "Name": "Team 1610612756", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1500, "Arc3Assists": 300, "Corner3Assists": 300, "AtRimAssists": 300, "ShortMidRangeAssists": 300, "LongMidRangeAssists": 300},
    1610612757: {"TeamId": 1610612757, "Name": "Team 1610612757", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1575, "Arc3Assists": 315, "Corner3Assists": 315, "AtRimAssists": 315, "ShortMidRangeAssists": 315, "LongMidRangeAssists": 315},
    1610612758: {"TeamId": 1610612758, "Name": "Team 1610612758", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1650, "Arc3Assists": 330, "Corner3Assists": 330, "AtRimAssists": 330, "ShortMidRangeAssists": 330, "LongMidRangeAssists": 330},
    1610612759: {"TeamId": 1610612759, "Name": "Team 1610612759", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1725, "Arc3Assists": 345, "Corner3Assists": 345, "AtRimAssists": 345, "ShortMidRangeAssists": 345, "LongMidRangeAssists": 345},
    1610612760: {"TeamId": 1610612760, "Name": "Team 1610612760", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1800, "Arc3Assists": 360, "Corner3Assists": 360, "AtRimAssists": 360, "ShortMidRangeAssists": 360, "LongMidRangeAssists": 360},
    1610612761: {"TeamId": 1610612761, "Name": "Team 1610612761", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1875, "Arc3Assists": 375, "Corner3Assists": 375, "AtRimAssists": 375, "ShortMidRangeAssists": 375, "LongMidRangeAssists": 375},
    1610612762: {"TeamId": 1610612762, "Name": "Team 1610612762", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 1950, "Arc3Assists": 390, "Corner3Assists": 390, "AtRimAssists": 390, "ShortMidRangeAssists": 390, "LongMidRangeAssists": 390},
    1610612763: {"TeamId": 1610612763, "Name": "Team 1610612763", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 2025, "Arc3Assists": 405, "Corner3Assists": 405, "AtRimAssists": 405, "ShortMidRangeAssists": 405, "LongMidRangeAssists": 405},
    1610612764: {"TeamId": 1610612764, "Name": "Team 1610612764", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 2100, "Arc3Assists": 420, "Corner3Assists": 420, "AtRimAssists": 420, "ShortMidRangeAssists": 420, "LongMidRangeAssists": 420},
    1610612765: {"TeamId": 1610612765, "Name": "Team 1610612765", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 2175, "Arc3Assists": 435, "Corner3Assists": 435, "AtRimAssists": 435, "ShortMidRangeAssists": 435, "LongMidRangeAssists": 435},
    1610612766: {"TeamId": 1610612766, "Name": "Team 1610612766", "GamesPlayed": 15, "SecondsPlayed": 43200, "Assists": 2250, "Arc3Assists": 450, "Corner3Assists": 450, "AtRimAssists": 450, "ShortMidRangeAssists": 450, "LongMidRangeAssists": 450},
}

_RECORDED_NBA_SHOT_CHART = {
    team_id: {
        "TEAM_ID": team_id, "TEAM_NAME": f"Team {team_id}", "GP": 15,
        "FG2M": 1, "FG2A": 1, "FG3M": 1, "FG3A": 1,
    }
    for team_id in _RECORDED_NBA_MEMBERSHIP
}
_RECORDED_NBA_SHOT_ZONES = {
    team_id: {
        "TEAM_ID": team_id, "TEAM_NAME": f"Team {team_id}",
        "Restricted Area_OPP_FGM": 1, "Restricted Area_OPP_FGA": 1,
    }
    for team_id in _RECORDED_NBA_MEMBERSHIP
}


class _RecordedLegacyNBA:
    def __init__(self):
        self.team_ids = tuple(_RECORDED_NBA_MEMBERSHIP)
        self.membership = _RECORDED_NBA_MEMBERSHIP
        self.aggregate_calls = []
        self.detail_calls = []

    def fetch_team_game_ids(self, *, team_id, season, season_type, date_from, date_to):
        self.detail_calls.append((team_id, season, season_type, date_from, date_to))
        return self.membership[team_id]

    def fetch_opponent_team_stats(self, date_from, **kwargs):
        self.aggregate_calls.append(("traditional", date_from, dict(kwargs)))
        team_ids = self.team_ids if kwargs["team_id"] is None else (kwargs["team_id"],)
        return pd.DataFrame([_RECORDED_NBA_AGGREGATES[team_id] for team_id in team_ids])

    def fetch_opponent_shot_chart(self, general_range, date_from, **kwargs):
        team_ids = self.team_ids if kwargs["team_id"] is None else (kwargs["team_id"],)
        return pd.DataFrame([_RECORDED_NBA_SHOT_CHART[team_id] for team_id in team_ids])

    def fetch_opponent_shooting_zone(self, date_from, **kwargs):
        team_ids = self.team_ids if kwargs["team_id"] is None else (kwargs["team_id"],)
        return pd.DataFrame([_RECORDED_NBA_SHOT_ZONES[team_id] for team_id in team_ids])

    def fetch_synergy_play_types(self, play_type, **kwargs):
        return pd.DataFrame()


class _RecordedLegacyPBP:
    def __init__(self):
        self.team_ids = tuple(_RECORDED_PBP_MEMBERSHIP)
        self.membership = _RECORDED_PBP_MEMBERSHIP
        self.aggregate_calls = []
        self.detail_calls = []

    def fetch_team_game_ids(self, *, team_id, season, season_type, date_from, date_to):
        self.detail_calls.append((team_id, season, season_type, date_from, date_to))
        return self.membership[team_id]

    def fetch_totals_frame(self, data_type, **kwargs):
        assert data_type == "opponent"
        self.aggregate_calls.append(dict(kwargs))
        team_ids = self.team_ids if kwargs["team_id"] is None else (kwargs["team_id"],)
        return pd.DataFrame([_RECORDED_PBP_AGGREGATES[team_id] for team_id in team_ids])


def _refresh_isolated_legacy_output(legacy_engine, source_engine, governance):
    """Run the production legacy writer against recorded provider responses."""

    team_ids = tuple(sorted(governance.team_ids))
    nba = _RecordedLegacyNBA()
    pbp = _RecordedLegacyPBP()
    assert nba.team_ids == team_ids
    assert pbp.team_ids == team_ids
    assert nba.membership is not pbp.membership
    assert nba.membership == pbp.membership
    with source_engine.connect() as connection:
        manifest = connection.execute(
            select(CollectionManifest.__table__).where(
                CollectionManifest.manifest_id == governance.manifest_id
            )
        ).mappings().one()
        catalog = connection.execute(
            select(CatalogPublication.__table__).where(
                CatalogPublication.publication_id
                == governance.event_catalog_publication_id
            )
            ).mappings().one()
    catalog_events = tuple(json.loads(catalog["payload"])["events"])
    with legacy_engine.begin() as connection:
        # The isolated writer needs the same immutable authority rows, but it
        # owns the completed legacy snapshots independently of the route DB.
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(CollectionManifest.__table__.insert().values(**manifest))
    provenance = TeamMatchupProvenance(
        cutoff=governance.cutoff,
        manifest_id=governance.manifest_id,
        event_catalog_publication_id=governance.event_catalog_publication_id,
        event_catalog_checksum=governance.event_catalog_checksum,
        manifest_checksum=manifest["checksum"],
        collect_before=governance.collect_before,
    )
    TeamMatchupRefreshService(
        repository=TeamMatchupRepository(legacy_engine),
        event_catalog=_RecordedLegacyCatalog(catalog_events),
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        # Keep collect_before in the future while preserving a real writer
        # timestamp on the completed isolated snapshot.
        clock=lambda: governance.cutoff - timedelta(minutes=1),
    ).refresh(SEASON, as_of=governance.cutoff.date(), provenance=provenance)
    repository = TeamMatchupRepository(legacy_engine)
    snapshots = tuple(
        repository.get_snapshot(
            TeamMatchupSnapshotScope(SEASON, governance.cutoff.date(), window_games)
        )
        for window_games in (None, 15)
    )
    assert len(nba.detail_calls) == len(team_ids) * 2
    assert len(pbp.detail_calls) == len(team_ids) * 2
    return snapshots, nba, pbp


def _event_catalog(engine, settings):
    service = EventCatalogService(
        engine,
        nba_stats_provider=_NoProvider(),
        settings=settings,
        clock=lambda: NOW,
    )
    service.repository.publish(
        SEASON,
        pd.DataFrame(
            [
                {
                    "nba_game_id": GAME_ID,
                    "season": SEASON,
                    "home_team_id": BOS,
                    "home_team_name": "Boston Celtics",
                    "home_team_tricode": "BOS",
                    "away_team_id": LAL,
                    "away_team_name": "Los Angeles Lakers",
                    "away_team_tricode": "LAL",
                    "scheduled_at": datetime(2026, 1, 16, 0, 30, tzinfo=timezone.utc),
                    "status_text": "Scheduled",
                    "status_code": 1,
                    "postponed_status": None,
                    "postponement_evidence": None,
                    "classification": "Regular Season",
                }
            ]
        ),
        NOW,
    )
    return service


def _team_matchups(
    engine,
    *,
    asymmetric_shot_zones=False,
    missing_rebound_window=None,
    extra_traditional_window=None,
):
    repository = TeamMatchupRepository(engine)
    teams = json.loads(TEAM_FIXTURE.read_text(encoding="utf-8"))
    metrics = (
        ("play_types", "Transition", "PTS"),
        ("shot_zones", "Restricted Area", "FGA"),
        ("shot_zones", "Restricted Area", "FGM"),
        ("shot_zones", "In The Paint (Non-RA)", "FGA"),
        ("shot_zones", "In The Paint (Non-RA)", "FGM"),
        ("shot_zones", "Mid-Range", "FGA"),
        ("shot_zones", "Mid-Range", "FGM"),
        ("shot_zones", "Corner 3", "FGA"),
        ("shot_zones", "Corner 3", "FGM"),
        ("shot_zones", "Above the Break 3", "FGA"),
        ("shot_zones", "Above the Break 3", "FGM"),
        ("shot_zones", "Left Corner 3", "FGA"),
        ("shot_zones", "Left Corner 3", "FGM"),
        ("shot_zones", "Right Corner 3", "FGA"),
        ("shot_zones", "Right Corner 3", "FGM"),
        ("shot_zones", "Backcourt", "FGA"),
        ("shot_zones", "Backcourt", "FGM"),
        ("shot_types", "catch_and_shoot", "FG2M"),
        ("shot_types", "catch_and_shoot", "FG2A"),
        ("shot_types", "catch_and_shoot", "FG3M"),
        ("shot_types", "catch_and_shoot", "FG3A"),
        ("shot_types", "pullups", "FG2M"),
        ("shot_types", "pullups", "FG2A"),
        ("shot_types", "pullups", "FG3M"),
        ("shot_types", "pullups", "FG3A"),
        ("shot_types", "less_than_10_ft", "FG2M"),
        ("shot_types", "less_than_10_ft", "FG2A"),
        ("shot_types", "less_than_10_ft", "FG3M"),
        ("shot_types", "less_than_10_ft", "FG3A"),
        ("assist_locations", "Assists", "Assists"),
        ("assist_locations", "Arc3Assists", "Arc3Assists"),
        ("assist_locations", "Corner3Assists", "Corner3Assists"),
        ("assist_locations", "AtRimAssists", "AtRimAssists"),
        ("assist_locations", "ShortMidRangeAssists", "ShortMidRangeAssists"),
        ("assist_locations", "LongMidRangeAssists", "LongMidRangeAssists"),
        ("traditional", "OPP_REB", "OPP_REB"),
        ("traditional", "OPP_TOV", "OPP_TOV"),
        ("traditional", "OPP_STL", "OPP_STL"),
        ("traditional", "OPP_BLK", "OPP_BLK"),
    )

    def facts(
        *,
        omit_play_types=False,
        omit_paint=False,
        omit_rebounds=False,
        include_opponent_pf=False,
    ):
        stored_metrics = (
            *metrics,
            *(
                (("traditional", "OPP_PF", "OPP_PF"),)
                if include_opponent_pf
                else ()
            ),
        )
        return tuple(
            TeamMatchupFact(
                row["team_id"],
                base,
                slice_key,
                stat_key,
                (
                    0.0
                    if base == "shot_types"
                    and slice_key == "less_than_10_ft"
                    and stat_key in {"FG3M", "FG3A"}
                    else float(row["allowed"])
                ),
                48.0,
                "minutes",
                "recorded",
            )
            for row in teams
            for base, slice_key, stat_key in stored_metrics
            if not (omit_play_types and base == "play_types")
            and not (
                omit_rebounds
                and base == "traditional"
                and slice_key == stat_key == "OPP_REB"
            )
            and not (
                omit_paint
                and base == "shot_zones"
                and slice_key == "In The Paint (Non-RA)"
                and stat_key == "FGA"
            )
        )

    season_scope = TeamMatchupSnapshotScope(SEASON, date(2026, 1, 15))
    last_scope = TeamMatchupSnapshotScope(SEASON, date(2026, 1, 15), 15)
    bases = (
        "assist_locations",
        "play_types",
        "shot_types",
        "shot_zones",
        "traditional",
    )
    repository.replace_snapshots(
        (
            (
                season_scope,
                facts(
                    omit_rebounds=missing_rebound_window == "season",
                    include_opponent_pf=extra_traditional_window == "season",
                ),
                tuple(TeamMatchupObservation(base, "available") for base in bases),
            ),
            (
                last_scope,
                facts(
                    omit_play_types=True,
                    omit_paint=asymmetric_shot_zones,
                    omit_rebounds=missing_rebound_window == "last_15",
                    include_opponent_pf=extra_traditional_window == "last_15",
                ),
                tuple(
                    TeamMatchupObservation(
                        base,
                        "unavailable" if base == "play_types" else "available",
                        "provider_window_unsupported" if base == "play_types" else None,
                    )
                    for base in bases
                ),
            ),
        ),
        retrieved_at=NOW,
    )
    return TeamMatchupQueryService(repository, clock=lambda: NOW)


def _player_pool(engine):
    repository = PlayerPoolSnapshotRepository(engine)
    scope = PlayerPoolSnapshotScope.create(SEASON, (GAME_ID,))
    assert repository.try_acquire_refresh(
        scope, owner="fixture", now=NOW, lease_seconds=60
    )
    assert repository.replace_owned(
        scope,
        owner="fixture",
        payload={
            "players": [
                {
                    "canonical_player_id": 2544,
                    "name": "LeBron James",
                    "team_id": LAL,
                    "market_categories": list(COMMON_POSTED_MARKETS),
                    "provenance": {
                        "prizepicks": list(COMMON_POSTED_MARKETS),
                        "underdog": ["PTS"],
                    },
                }
            ],
            "team_counts": {str(LAL): 1},
            "freshness": {
                "status": "fresh",
                "retrieved_at": NOW.isoformat(),
                "providers": {
                    "prizepicks": {
                        "status": "fresh",
                        "retrieved_at": NOW.isoformat(),
                    }
                },
            },
        },
        retrieved_at=NOW,
        now=NOW,
    )
    return StoredPlayerPoolReader(repository, clock=lambda: NOW)


def _player_logs(engine, catalog):
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=catalog,
        stats_surface_max_age=timedelta(hours=30),
        stats_surface_season=SEASON,
        clock=lambda: NOW,
    )
    repository.publish(
        SEASON,
        (
            PlayerGameLogRecord(
                season=SEASON,
                season_type="Regular Season",
                player_id=2544,
                game_id="0022500500",
                player_name="LeBron James",
                game_date=date(2026, 1, 10),
                team_id=LAL,
                team_tricode="LAL",
                opponent_team_id=BOS,
                opponent_team_tricode="BOS",
                is_home=True,
                minutes=35.0,
                points=25,
                rebounds=8,
                assists=7,
                field_goals_made=10,
                field_goals_attempted=18,
                three_pointers_made=3,
                three_pointers_attempted=7,
                turnovers=3,
                steals=1,
                blocks=1,
            ),
        ),
        retrieved_at=NOW,
        source_provider="recorded",
        source_row_count=1,
    )
    return repository


def _player_diets(engine):
    service = PlayerDietService(
        engine,
        athlete_catalog=_NoProvider(),
        nba_stats_provider=_NoProvider(),
        pbp_stats_provider=_NoProvider(),
        clock=lambda: NOW,
    )
    service.repository.publish(
        SEASON,
        (
            PlayerDietFact(
                2544,
                "play_types",
                "Transition",
                1.0,
                95.0,
                20,
                "possessions",
                "nba_synergy",
            ),
            *(
                PlayerDietFact(
                    2544,
                    "play_types",
                    slice_key,
                    0.0,
                    0.0,
                    20,
                    "possessions",
                    "nba_synergy",
                )
                for slice_key in (
                    "Isolation",
                    "PRBallHandler",
                    "PRRollMan",
                    "OffRebound",
                    "Spotup",
                    "Cut",
                    "Handoff",
                    "OffScreen",
                    "Misc",
                    "Postup",
                )
            ),
            PlayerDietFact(
                2544,
                "shot_zones",
                "Restricted Area",
                0.2,
                20.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            *(
                PlayerDietFact(
                    2544,
                    "shot_zones",
                    slice_key,
                    0.2,
                    20.0,
                    20,
                    "field_goal_attempts",
                    "nba_stats",
                )
                for slice_key in (
                    "In The Paint (Non-RA)",
                    "Mid-Range",
                    "Corner 3",
                    "Above the Break 3",
                )
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Catch and Shoot",
                0.4,
                40.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Pullups",
                0.3,
                30.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Less Than 10 ft",
                0.3,
                30.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "assist_locations",
                "AtRimAssists",
                0.2,
                20.0,
                20,
                "assists",
                "pbp_stats",
            ),
            *(
                PlayerDietFact(
                    2544,
                    "assist_locations",
                    slice_key,
                    0.2,
                    20.0,
                    20,
                    "assists",
                    "pbp_stats",
                )
                for slice_key in (
                    "Arc3Assists",
                    "Corner3Assists",
                    "ShortMidRangeAssists",
                    "LongMidRangeAssists",
                )
            ),
        ),
        tuple(
            PlayerDietObservation(base, "available")
            for base in (
                "assist_locations",
                "play_types",
                "shot_types",
                "shot_zones",
            )
        ),
        retrieved_at=NOW,
    )
    return service


def _injuries(engine):
    class RecordedProvider:
        def get_snapshot(self):
            return InjuryProviderSnapshot(
                raw_payload=[
                    {
                        "ID": "6504",
                        "player": "LeBron James",
                        "team": "LAL",
                        "status": "Questionable",
                    }
                ],
                entries=(
                    InjuryEntryEvidence(
                        "rotowire:6504",
                        "6504",
                        "LeBron James",
                        "LAL",
                        "Questionable",
                        "Questionable",
                        "Left ankle soreness",
                        "https://www.rotowire.com/basketball/player/lebron-james-2344",
                    ),
                ),
                retrieved_at=NOW,
            )

    return MatchupInjuryService(
        provider=RecordedProvider(),
        snapshot_repository=InjurySnapshotRepository(engine),
        athlete_catalog=SimpleNamespace(
            get_catalog=lambda season, active_only=False: [
                {
                    "season": season,
                    "player_id": 2544,
                    "display_name": "LeBron James",
                    "team_id": LAL,
                    "team_abbreviation": "LAL",
                }
            ]
        ),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    )


def test_persisted_matchup_fixture_serves_exact_windows_and_raw_player_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'matchup.sqlite3'}")
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    service = MatchupService(
        event_catalog=_event_catalog(engine, settings),
        player_pool=_player_pool(engine),
        player_logs=_player_logs(engine, catalog),
        player_diets=_player_diets(engine),
        team_matchups=_team_matchups(engine),
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=_injuries(engine),
        clock=lambda: NOW,
    )
    dependencies = SimpleNamespace(
        settings=settings,
        matchup_service=service,
        user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["league"]["surface_availability"]["play_types"]["last_15"] == {
        "status": "unavailable",
        "unavailable_reason": "provider_window_unsupported",
    }
    assert payload["league"]["defense_sheet"]["play_types"][0]["last_15"] is None
    assert payload["teams"][0]["defense_sheet"]["play_types"][0]["last_15"] is None
    # LAL is the 11th deterministic fixture team; derived values come from the
    # stored 30-team facts, not request-time provider work.
    season_row = payload["teams"][0]["defense_sheet"]["play_types"][0]["season"]
    assert season_row["allowed_per_48"] == 11.0
    assert season_row["percent_vs_league_average"] == -29.032258
    assert season_row["rank"] == 11
    assert payload["players"][0]["canonical_id"] == 2544
    assert payload["players"][0]["season_scoring"] == 25.0
    assert payload["players"][0]["last_10_minutes"] == [35.0]
    play_diet = payload["players"][0]["diet_shares"]["play_types"]
    assert {row["key"] for row in play_diet} == {
        "Transition",
        "Isolation",
        "PRBallHandler",
        "PRRollMan",
        "OffRebound",
        "Spotup",
        "Cut",
        "Handoff",
        "OffScreen",
        "Misc",
        "Postup",
    }
    assert next(row for row in play_diet if row["key"] == "Transition") == {
        "key": "Transition",
        "season": {
            "share": 1.0,
            "volume": 95.0,
            "games_played": 20,
            "volume_unit": "possessions",
        },
    }
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["injuries"]["status"] == "fresh"
    assert payload["injuries"]["teams"][0]["submission_state"] == "unknown"
    assert payload["injuries"]["teams"][0]["entries"][0][
        "canonical_player_id"
    ] == 2544
    scores = payload["players"][0]["scores"]
    assert set(scores) == set(COMMON_POSTED_MARKETS)
    for market, score in scores.items():
        for window_name in ("season", "last_15"):
            window = score[window_name]
            assert isinstance(window["components"], dict)
            if market in {"TOV", "STL", "BLK", "STKS"}:
                assert "blend" not in window
            else:
                assert window["components"]
                assert set(window["blend"]) == {"value", "thin"}
                assert isinstance(window["blend"]["value"], float)
                assert isinstance(window["blend"]["thin"], bool)
    for market in ("3PM", "FG3A"):
        for window_name in ("season", "last_15"):
            assert scores[market][window_name]["components"]["shot_types"] == {
                "value": -0.609677,
                "thin": True,
            }
    assert "shot_types" in scores["PA"]["season"]["components"]
    assert {
        row["key"]: row["markets"]
        for row in payload["teams"][0]["defense_sheet"]["shot_zones"]
    } == {
        "Above the Break 3:FGA": ["FGA", "FG3A"],
        "Above the Break 3:FGM": ["PTS", "3PM"],
        "Corner 3:FGA": ["FGA", "FG3A"],
        "Corner 3:FGM": ["PTS", "3PM"],
        "In The Paint (Non-RA):FGA": ["FGA", "FG2A"],
        "In The Paint (Non-RA):FGM": ["PTS"],
        "Mid-Range:FGA": ["FGA", "FG2A"],
        "Mid-Range:FGM": ["PTS"],
        "Restricted Area:FGA": ["FGA", "FG2A"],
        "Restricted Area:FGM": ["PTS"],
    }
    player_shot_types = {
        row["key"] for row in payload["players"][0]["diet_shares"]["shot_types"]
    }
    assert player_shot_types == {
        "Catch and Shoot",
        "Pullups",
        "Less Than 10 ft",
    }
    assert {
        row["key"].rsplit(":", 1)[0]
        for row in payload["league"]["defense_sheet"]["shot_types"]
    } == player_shot_types
    for team in payload["teams"]:
        assert {
            row["key"].rsplit(":", 1)[0]
            for row in team["defense_sheet"]["shot_types"]
        } == player_shot_types


def test_persisted_matchup_degrades_only_an_asymmetric_available_surface(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'asymmetric-matchup.sqlite3'}")
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    service = MatchupService(
        event_catalog=_event_catalog(engine, settings),
        player_pool=_player_pool(engine),
        player_logs=_player_logs(engine, catalog),
        player_diets=_player_diets(engine),
        team_matchups=_team_matchups(engine, asymmetric_shot_zones=True),
        stats_freshness=stats_freshness,
        settings=settings,
        clock=lambda: NOW,
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                matchup_service=service,
                user_service=SimpleNamespace(
                    create_or_update_user=lambda _user: None
                ),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["league"]["surface_availability"]["shot_zones"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    assert payload["freshness"]["team_matchups"]["last_15"]["surfaces"][
        "shot_zones"
    ] == {
        "status": "unavailable",
        "unavailable_reason": "legacy_surface_incomplete",
        "retrieved_at": NOW.isoformat(),
    }
    league_rows = payload["league"]["defense_sheet"]["shot_zones"]
    assert {
        "In The Paint (Non-RA):FGA",
        "In The Paint (Non-RA):FGM",
    } <= {row["key"] for row in league_rows}
    assert all(row["season"] is not None for row in league_rows)
    assert all(row["last_15"] is None for row in league_rows)
    for team in payload["teams"]:
        assert [row["key"] for row in team["defense_sheet"]["shot_zones"]] == [
            row["key"] for row in league_rows
        ]
        assert all(row["season"] is not None for row in team["defense_sheet"]["shot_zones"])
        assert all(row["last_15"] is None for row in team["defense_sheet"]["shot_zones"])
    assert payload["league"]["defense_sheet"]["shot_types"][0]["last_15"] is not None


@pytest.mark.parametrize("missing_rebound_window", ("season", "last_15"))
def test_persisted_authenticated_matchup_keeps_legacy_traditional_available(
    tmp_path,
    missing_rebound_window,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'legacy-rebounds-{missing_rebound_window}.sqlite3'}"
    )
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    service = MatchupService(
        event_catalog=_event_catalog(engine, settings),
        player_pool=_player_pool(engine),
        player_logs=_player_logs(engine, catalog),
        player_diets=_player_diets(engine),
        team_matchups=_team_matchups(
            engine,
            missing_rebound_window=missing_rebound_window,
        ),
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=_injuries(engine),
        clock=lambda: NOW,
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                matchup_service=service,
                user_service=SimpleNamespace(
                    create_or_update_user=lambda _user: None
                ),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["league"]["surface_availability"]["traditional"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {"status": "available", "unavailable_reason": None},
    }
    present_rebound_window = (
        "last_15" if missing_rebound_window == "season" else "season"
    )
    league_rows = {
        row["key"]: row
        for row in payload["league"]["defense_sheet"]["traditional"]
    }
    assert league_rows["OPP_REB"][missing_rebound_window] is None
    assert league_rows["OPP_REB"][present_rebound_window] is not None
    for key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
        assert league_rows[key]["season"] is not None
        assert league_rows[key]["last_15"] is not None
        assert payload["league"]["defensive_columns"][key]["season"] is not None
        assert payload["league"]["defensive_columns"][key]["last_15"] is not None
    for team in payload["teams"]:
        team_rows = {row["key"]: row for row in team["defense_sheet"]["traditional"]}
        assert team_rows["OPP_REB"][missing_rebound_window] is None
        assert team_rows["OPP_REB"][present_rebound_window] is not None
        for key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
            assert team_rows[key]["season"] is not None
            assert team_rows[key]["last_15"] is not None

    scores = payload["players"][0]["scores"]
    assert scores["REB"][missing_rebound_window] == {
        "components": {},
        "blend": None,
    }
    assert scores["REB"][present_rebound_window]["components"][
        "traditional"
    ] is not None
    assert scores["REB"][present_rebound_window]["blend"] is not None
    for market in ("TOV", "STL", "BLK"):
        assert scores[market]["season"]["components"]["traditional"] is not None
        assert scores[market]["last_15"]["components"]["traditional"] is not None
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["injuries"]["status"] == "fresh"


@pytest.mark.parametrize("extra_traditional_window", ("season", "last_15"))
def test_persisted_matchup_degrades_non_rebound_traditional_identity_divergence(
    tmp_path,
    extra_traditional_window,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'traditional-divergence-{extra_traditional_window}.sqlite3'}"
    )
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    service = MatchupService(
        event_catalog=_event_catalog(engine, settings),
        player_pool=_player_pool(engine),
        player_logs=_player_logs(engine, catalog),
        player_diets=_player_diets(engine),
        team_matchups=_team_matchups(
            engine,
            extra_traditional_window=extra_traditional_window,
        ),
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=_injuries(engine),
        clock=lambda: NOW,
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                matchup_service=service,
                user_service=SimpleNamespace(
                    create_or_update_user=lambda _user: None
                ),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    missing_window = (
        "last_15" if extra_traditional_window == "season" else "season"
    )
    assert payload["league"]["surface_availability"]["traditional"] == {
        extra_traditional_window: {
            "status": "available",
            "unavailable_reason": None,
        },
        missing_window: {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    league_rows = payload["league"]["defense_sheet"]["traditional"]
    assert {row["key"] for row in league_rows} == {
        "OPP_BLK",
        "OPP_PF",
        "OPP_REB",
        "OPP_STL",
        "OPP_TOV",
    }
    assert all(row[extra_traditional_window] is not None for row in league_rows)
    assert all(row[missing_window] is None for row in league_rows)
    assert all(
        row[extra_traditional_window] is not None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
    )
    assert all(
        row[missing_window] is None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
    )
    assert all(
        row["season"] is not None and row["last_15"] is not None
        for row in payload["league"]["defense_sheet"]["shot_zones"]
    )
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["injuries"]["status"] == "fresh"


def test_authenticated_slate_matchup_selection_journey_uses_one_activated_generation(
    tmp_path,
    monkeypatch,
):
    """Exercise authenticated bytes across legacy and ledger activation states."""

    engine, matchup_governance, _ = _runner_world(tmp_path)
    settings = RuntimeSettings(
        environment="testing",
        # This journey must traverse the real Bearer-token path.  The Firebase
        # SDK boundary is patched below with a deterministic verified fixture.
        auth=AuthenticationSettings(firebase_admin_disabled=False),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    event_catalog = _event_catalog(engine, settings)
    pool = _player_pool(engine)
    player_logs = _player_logs(engine, catalog)
    player_diets = _player_diets(engine)
    log_facts = player_logs.list_player_rows(SEASON, 2544)
    diet_facts = player_diets.repository.get_for_players(SEASON, (2544,)).players[2544]
    # This journey proves the public HTTP contract across activation, not the
    # governed parity evidence behind it.  The cross-stream cohort gate needs a
    # complete 82-game authority plus a validated artifact for every sibling
    # ledger stream, which this bounded fixture cannot supply; that gate is
    # covered by tests/services/test_matchup_parity.py.  Relax it here only,
    # so the production activation path keeps requiring it.  The activated
    # stream's own parity artifact is still checked.
    import app.services.ledger_parity as _ledger_parity

    monkeypatch.setattr(
        _ledger_parity,
        "matchup_parity_cohort_is_activatable",
        lambda *args, **kwargs: True,
    )
    publication_service = PublicationService(engine, clock=lambda: NOW)
    publication_service.register_default_streams()
    operations = CollectionOperationsService(
        engine,
        publication_service=publication_service,
        l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine),
        clock=lambda: NOW,
    )

    ledger_manifest_id = "journey-ledger-manifest"
    ledger_cutoff = NOW - timedelta(days=1)
    ledger_observations = (
        "journey-ledger-observation-first",
        "journey-ledger-observation-second",
    )
    ledger_observation_checksum = hashlib.sha256(b"{}").hexdigest()
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=ledger_manifest_id,
            season=SEASON,
            cutoff=ledger_cutoff,
            collect_before=NOW + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="a" * 64,
            status="active",
            created_at=NOW - timedelta(days=2),
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": observation_id,
                "client_observation_id": observation_id,
                "collector_id": "journey-collector",
                "manifest_id": ledger_manifest_id,
                "environment": "server",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({
                    "game_id": GAME_ID,
                    "surface": "canonical_game_ledger",
                }, sort_keys=True),
                "season": SEASON,
                "cutoff": ledger_cutoff,
                "schema_version": 1,
                "checksum": ledger_observation_checksum,
                "payload": "{}",
                "payload_bytes": 2,
                "retrieved_at": NOW,
                "accepted_at": NOW,
            }
            for observation_id in ledger_observations
        ])
        connection.execute(CanonicalGameLedgerGame.__table__.insert().values(
            game_id=GAME_ID,
            season=SEASON,
            season_type="Regular Season",
            game_date=date(2026, 1, 14),
            home_team_id=BOS,
            home_team_tricode="BOS",
            away_team_id=LAL,
            away_team_tricode="LAL",
            status="final",
            source_observation_id=ledger_observations[0],
            checksum="b" * 64,
            raw_checksum="c" * 64,
            retrieved_at=NOW,
            updated_at=NOW,
        ))
        connection.execute(LedgerObservationEvidence.__table__.insert(), [
            {
                "observation_id": observation_id,
                "game_id": GAME_ID,
                "created_at": NOW,
            }
            for observation_id in ledger_observations
        ])

    def candidate(
        stream_key,
        payload,
        *,
        publication_id,
        observation_id,
        provider="nba",
        manifest_id=None,
        observation_type=None,
        scope=None,
        role="accepted_candidate",
        slice_key=None,
        insert_observation=True,
    ):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(encoded.encode()).hexdigest()
        with engine.begin() as connection:
            connection.execute(PublicationVersion.__table__.insert().values(
                publication_id=publication_id,
                stream_key=stream_key,
                season=SEASON,
                cutoff=NOW - timedelta(days=1),
                version=1,
                status="candidate",
                checksum=checksum,
                payload=encoded,
                manifest_id=manifest_id,
                created_at=NOW,
                reason="authenticated journey candidate",
                fence=0,
            ))
            if insert_observation:
                connection.execute(CollectionObservation.__table__.insert().values(
                    observation_id=observation_id,
                    client_observation_id=observation_id,
                    collector_id="journey-collector",
                    manifest_id=manifest_id,
                    environment="testing",
                    provider=provider,
                    observation_type=observation_type or stream_key,
                    scope=json.dumps(scope or {"stream": stream_key}),
                    season=SEASON,
                    cutoff=ledger_cutoff,
                    schema_version=1,
                    checksum=checksum,
                    payload=encoded,
                    payload_bytes=len(encoded.encode()),
                    retrieved_at=NOW,
                    accepted_at=NOW,
                ))
            connection.execute(PublicationObservation.__table__.insert().values(
                publication_id=publication_id,
                observation_id=observation_id,
                role=role,
                slice_key=slice_key,
                created_at=NOW,
            ))
        return checksum

    def log_payload(points):
        row = asdict(log_facts[0])
        row["game_date"] = row["game_date"].isoformat()
        row["points"] = points
        return {"rows": [row]}

    first_log = "journey-log-first"
    second_log = "journey-log-second"
    first_log_payload = log_payload(25)
    second_log_payload = log_payload(26)
    first_log_checksum = candidate(
        "player_game_logs", first_log_payload, publication_id=first_log,
        observation_id=ledger_observations[0],
        provider="pbp",
        manifest_id=ledger_manifest_id,
        observation_type="canonical_game_ledger",
        scope={"game_id": GAME_ID, "surface": "canonical_game_ledger"},
        role="ledger_game",
        slice_key=GAME_ID,
        insert_observation=False,
    )
    second_log_checksum = candidate(
        "player_game_logs", second_log_payload, publication_id=second_log,
        observation_id=ledger_observations[1],
        provider="pbp",
        manifest_id=ledger_manifest_id,
        observation_type="canonical_game_ledger",
        scope={"game_id": GAME_ID, "surface": "canonical_game_ledger"},
        role="ledger_game",
        slice_key=GAME_ID,
        insert_observation=False,
    )
    first_parity = "journey-parity-first"
    second_parity = "journey-parity-second"
    with engine.begin() as connection:
        connection.execute(LedgerParityArtifact.__table__.insert(), [
            {
                "artifact_id": first_parity,
                "publication_id": first_log,
                "payload_checksum": first_log_checksum,
                "stream_key": "player_game_logs",
                "season": SEASON,
                "cutoff": NOW - timedelta(days=1),
                "status": "exact",
                "report": "{}",
                "created_at": NOW,
            },
            {
                "artifact_id": second_parity,
                "publication_id": second_log,
                "payload_checksum": second_log_checksum,
                "stream_key": "player_game_logs",
                "season": SEASON,
                "cutoff": NOW - timedelta(days=1),
                "status": "exact",
                "report": "{}",
                "created_at": NOW,
            },
        ])
    diet_rows = [
        {
            key: value
            for key, value in asdict(fact).items()
            if key not in {"base", "retrieved_at"}
        }
        for fact in diet_facts
        if fact.base == "play_types"
    ]
    diet_candidate = "journey-diet"
    candidate(
        "synergy_play_types", {"base": "play_types", "rows": diet_rows},
        publication_id=diet_candidate, observation_id="journey-observation-diet",
    )

    # Build complete production-shaped PBP games for the ledger side.  The
    # legacy side invokes the production writer against independently
    # recorded provider aggregate/detail responses in its own database.
    ledger_games = _route_ledger_games(matchup_governance)
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    ledger_engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(legacy_engine)
    run_migrations(ledger_engine)
    legacy_snapshots, legacy_nba, legacy_pbp = _refresh_isolated_legacy_output(
        legacy_engine,
        engine,
        matchup_governance,
    )
    # The refresh writer requests the league Season aggregate once and one
    # bounded per-team aggregate for exact L15 (for each independent source).
    assert len(legacy_nba.aggregate_calls) == len(matchup_governance.team_ids) + 1
    assert len(legacy_pbp.aggregate_calls) == len(matchup_governance.team_ids) + 1
    assert len(legacy_nba.detail_calls) == len(matchup_governance.team_ids) * 2
    assert len(legacy_pbp.detail_calls) == len(matchup_governance.team_ids) * 2
    legacy_retrieved_at = matchup_governance.cutoff - timedelta(minutes=1)
    for snapshot in legacy_snapshots:
        assert {
            surface: {
                fact.retrieved_at
                for fact in snapshot.facts
                if fact.base == surface
            }
            for surface in ("traditional", "assist_locations")
        } == {
            "traditional": {legacy_retrieved_at},
            "assist_locations": {legacy_retrieved_at},
        }
        assert {
            surface: {
                observation.retrieved_at
                for observation in snapshot.observations
                if observation.surface == surface
            }
            for surface in ("traditional", "assist_locations")
        } == {
            "traditional": {legacy_retrieved_at},
            "assist_locations": {legacy_retrieved_at},
        }
    TeamMatchupRepository(engine).replace_snapshots(
        tuple(
            (snapshot.scope, snapshot.facts, snapshot.observations)
            for snapshot in legacy_snapshots
        ),
        # Preserve the completed isolated writer's exact observation time;
        # copying must not relabel the legacy snapshot as a later read.
        retrieved_at=legacy_retrieved_at,
    )
    team_matchups = TeamMatchupQueryService(
        TeamMatchupRepository(engine), clock=lambda: NOW
    )

    # Activation's ledger writer fence requires every canonical game to retain
    # accepted immutable observation evidence, and each candidate must cite
    # that exact evidence rather than a synthetic aggregate checksum.  These
    # observations are accepted before the actual ledger materializer runs.
    canonical_observation_rows = [
        {
            "observation_id": game.source_observation_id,
            "client_observation_id": game.source_observation_id,
            "collector_id": "route-ledger-collector",
            "manifest_id": matchup_governance.manifest_id,
            "environment": "testing",
            "provider": "pbp",
            "observation_type": "canonical_game_ledger",
            "scope": json.dumps({
                "game_id": game.game_id,
                "surface": "canonical_game_ledger",
            }, sort_keys=True),
            "season": SEASON,
            "cutoff": matchup_governance.cutoff,
            "schema_version": 1,
            "checksum": hashlib.sha256(b"{}").hexdigest(),
            "payload": "{}",
            "payload_bytes": 2,
            "retrieved_at": matchup_governance.cutoff,
            "accepted_at": matchup_governance.cutoff,
        }
        for game in ledger_games
    ]
    with engine.begin() as connection:
        connection.execute(
            CollectionObservation.__table__.insert(), canonical_observation_rows
        )
    CanonicalGameLedgerRepository(engine).replace_games_atomic(ledger_games)

    class _NoLegacyDiagnostic:
        def read(self, *_args, **_kwargs):
            raise ValueError("route legacy diagnostic is isolated from ledger")

    ledger_repository = CanonicalGameLedgerRepository(ledger_engine)
    ledger_repository.replace_games_atomic(ledger_games)
    LedgerMatchupMaterializationService(
        ledger_repository,
        TeamMatchupRepository(ledger_engine),
        clock=lambda: matchup_governance.cutoff,
    ).materialize(
        SEASON,
        as_of=matchup_governance.cutoff.date(),
        expected_game_ids=matchup_governance.expected_game_ids,
        expected_l15_game_ids=matchup_governance.expected_l15_game_ids,
        team_ids=matchup_governance.team_ids,
    )
    LedgerMaterializationService(
        ledger_repository,
        # Publications are staged in the route control plane, so their
        # durable parity artifacts must reference that same publication FK;
        # derivation itself remains isolated in ``ledger_engine``.
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_NoLegacyDiagnostic(),
        publication_service=publication_service,
        clock=lambda: matchup_governance.cutoff,
    ).compose(
        ledger_games,
        season=SEASON,
        as_of=matchup_governance.cutoff.date(),
        cutoff=matchup_governance.cutoff,
        expected_game_ids=matchup_governance.expected_game_ids,
        expected_l15_game_ids=matchup_governance.expected_l15_game_ids,
        team_ids=matchup_governance.team_ids,
        require_assist_locations=True,
    )
    matchup_publications = {}
    with engine.connect() as connection:
        for window in ("season", "l15"):
            matchup_publications[window] = {}
            for stream_key in (
                f"traditional_opponent_{window}",
                f"assist_locations_{window}",
            ):
                publication = connection.execute(
                    select(PublicationVersion.publication_id)
                    .where(
                        PublicationVersion.stream_key == stream_key,
                        PublicationVersion.season == SEASON,
                        PublicationVersion.cutoff == matchup_governance.cutoff,
                        PublicationVersion.status == "candidate",
                    )
                    .order_by(PublicationVersion.version.desc())
                    .limit(1)
                ).scalar_one()
                matchup_publications[window][stream_key] = publication
    parity_runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    parity_runner.run(
        SEASON,
        "season",
        cutoff=matchup_governance.cutoff,
        publications=matchup_publications["season"],
    )
    parity_runner.run(
        SEASON,
        "l15",
        cutoff=matchup_governance.cutoff,
        publications=matchup_publications["l15"],
    )
    with engine.connect() as connection:
        matchup_artifacts = {
            stream: connection.execute(
                select(LedgerParityArtifact.artifact_id)
                .where(
                    LedgerParityArtifact.stream_key == stream,
                    LedgerParityArtifact.season == SEASON,
                    LedgerParityArtifact.cutoff == matchup_governance.cutoff,
                )
                .order_by(LedgerParityArtifact.created_at.desc())
                .limit(1)
            ).scalar_one()
            for stream in (
                *matchup_publications["season"].keys(),
                *matchup_publications["l15"].keys(),
            )
        }
    operations.activate_stream(
        "player_game_logs", actor="journey-operator", reason="activate first logs",
        season=SEASON, cutoff=NOW - timedelta(days=1),
        parity_artifact_id=first_parity, candidate_publication_id=first_log,
    )
    with engine.begin() as connection:
        connection.execute(CanonicalGameLedgerGame.__table__.update().where(
            CanonicalGameLedgerGame.game_id == GAME_ID,
        ).values(
            source_observation_id=ledger_observations[1],
            updated_at=NOW,
        ))
    operations.activate_stream(
        "player_game_logs", actor="journey-operator", reason="advance logs",
        season=SEASON, cutoff=NOW - timedelta(days=1),
        parity_artifact_id=second_parity, candidate_publication_id=second_log,
    )
    operations.activate_stream(
        "synergy_play_types", actor="journey-operator", reason="activate diet",
        season=SEASON, cutoff=NOW - timedelta(days=1), candidate_publication_id=diet_candidate,
    )
    rollback = operations.rollback_publication(
        "player_game_logs", actor="journey-operator", reason="restore first log payload"
    )
    assert rollback.resource.payload == json.dumps(
        first_log_payload, sort_keys=True, separators=(",", ":")
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    player_logs._publication_reader = reader
    player_diets.repository._publication_reader = reader
    team_matchups._publication_reader = reader
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    matchup_service = MatchupService(
        event_catalog=event_catalog,
        player_pool=pool,
        player_logs=player_logs,
        player_diets=player_diets,
        team_matchups=team_matchups,
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=None,
        clock=lambda: NOW,
        publication_reader=reader,
    )
    selection_service = MatchupSelectionService(
        event_catalog=event_catalog,
        player_pool=pool,
        player_logs=player_logs,
        archetypes=PlayerArchetypeRepository(engine),
        statistic_catalog=catalog,
        settings=settings,
        publication_reader=reader,
    )
    game_service = GameService(
        engine,
        settings=settings,
        nba_stats_adapter=_NoProvider(),
        game_logs_source=StoredGameLogsSource(player_logs),
    )
    game_service.get_player_id = lambda _player_name: 2544
    provider_calls = {"nba": 0, "pbp": 0}

    class ProviderCounter:
        def __init__(self, name):
            self.name = name

        def __getattr__(self, operation):
            provider_calls[self.name] += 1
            raise AssertionError(f"unexpected {self.name}.{operation} request")

    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())
    monkeypatch.setattr(
        "app.utils.auth.verify_firebase_token",
        lambda token: {
            "uid": "fixture-user",
            "email": "fixture@example.com",
            "email_verified": True,
            "admin": False,
            "token": token,
        },
    )

    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                game_service=game_service,
                slate_service=SlateService(
                    event_catalog,
                    settings=settings,
                    player_pool=None,
                    injuries=None,
                    clock=lambda: NOW,
                ),
                matchup_service=matchup_service,
                matchup_selection_service=selection_service,
                nba_stats_provider=ProviderCounter("nba"),
                pbp_stats_provider=ProviderCounter("pbp"),
                user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    client = app.test_client()
    auth_headers = {"Authorization": "Bearer authenticated-fixture-token"}

    # Capture authenticated legacy bytes before any ledger pointer changes.
    legacy_snapshot = reader.snapshot(
        tuple(
            stream_key
            for publications in matchup_publications.values()
            for stream_key in publications
        ),
        season=SEASON,
    )
    assert all(
        read.source == "legacy_database"
        and read.status == "inactive"
        and read.publication_id is None
        for read in legacy_snapshot.reads.values()
    )
    pre_matchup_response = client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=auth_headers
    )
    pre_player_game_log_response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2025-26",
        headers=auth_headers,
    )
    assert pre_matchup_response.status_code == 200
    assert pre_player_game_log_response.status_code == 200
    pre_matchup = pre_matchup_response.get_json()
    def assert_matchup_freshness_envelope(payload, *, source, expected_retrieved_at):
        for window in ("season", "last_15"):
            for surface in ("traditional", "assist_locations"):
                envelope = payload["freshness"]["team_matchups"][window]["surfaces"][surface]
                assert envelope["status"] == "available"
                assert envelope["unavailable_reason"] is None
                assert envelope["retrieved_at"] == expected_retrieved_at, source

    assert_matchup_freshness_envelope(
        pre_matchup,
        source="legacy",
        expected_retrieved_at=legacy_retrieved_at.isoformat(),
    )
    for stream_key in (
        *matchup_publications["season"].keys(),
        *matchup_publications["l15"].keys(),
    ):
        provenance = pre_matchup["provenance"][stream_key]
        assert provenance["source"] == "legacy_database"
        assert provenance["legacy_fallback_allowed"] is True
        assert provenance["publication_id"] is None
        assert provenance["retrieved_at"] is None

    # The four ledger-owned Season/L15 surfaces are activated as one governed
    # cohort.  This is deliberately between two real HTTP reads; repeated
    # reads of one state cannot prove the public byte contract.
    for window in ("season", "l15"):
        for stream_key, publication_id in matchup_publications[window].items():
            operations.activate_stream(
                stream_key,
                actor="journey-operator",
                reason="activate governed matchup cohort",
                season=SEASON,
                cutoff=matchup_governance.cutoff,
                parity_artifact_id=matchup_artifacts[stream_key],
                candidate_publication_id=publication_id,
            )
    activated_snapshot = reader.snapshot(
        tuple(
            stream_key
            for publications in matchup_publications.values()
            for stream_key in publications
        ),
        season=SEASON,
    )
    assert {
        stream_key: activated_snapshot.read(stream_key).publication_id
        for publications in matchup_publications.values()
        for stream_key in publications
    } == {
        stream_key: publication_id
        for publications in matchup_publications.values()
        for stream_key, publication_id in publications.items()
    }
    assert all(
        activated_snapshot.read(stream_key).status in {"active", "rollback"}
        for publications in matchup_publications.values()
        for stream_key in publications
    )
    with engine.connect() as connection:
        for publications in matchup_publications.values():
            for stream_key, publication_id in publications.items():
                created_at = connection.execute(
                    select(PublicationVersion.created_at).where(
                        PublicationVersion.publication_id == publication_id,
                        PublicationVersion.stream_key == stream_key,
                    )
                ).scalar_one()
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                else:
                    created_at = created_at.astimezone(timezone.utc)
                assert created_at == NOW

    slate_response = client.get("/api/games/slate?date=2026-01-15", headers=auth_headers)
    matchup_response = client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=auth_headers
    )
    # The route is the authenticated byte-contract seam: it includes the
    # persisted player-game-log fixture and all matchup surfaces after the
    # candidate activation/rollback journey.  Repeated reads must remain byte
    # stable; a checksum-only repository assertion would miss serializer drift.
    matchup_bytes = matchup_response.data
    repeated_matchup_response = client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=auth_headers
    )
    selection_response = client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=2544",
        headers=auth_headers,
    )
    player_game_log_response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2025-26",
        headers=auth_headers,
    )
    repeated_player_game_log_response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2025-26",
        headers=auth_headers,
    )

    restored_snapshot = reader.snapshot(
        ("player_game_logs", "synergy_play_types"), season=SEASON
    )
    assert restored_snapshot.read("player_game_logs").decoded[0].points == 25
    assert restored_snapshot.read("player_game_logs").publication_id == rollback.resource.publication_id
    assert restored_snapshot.generation

    assert slate_response.status_code == 200
    assert matchup_response.status_code == 200
    assert repeated_matchup_response.status_code == 200
    assert _source_independent_matchup_contract(matchup_response) == (
        _source_independent_matchup_contract(pre_matchup_response)
    )
    assert repeated_matchup_response.data == matchup_bytes
    assert selection_response.status_code == 200
    assert player_game_log_response.status_code == 200
    assert player_game_log_response.data == pre_player_game_log_response.data
    assert repeated_player_game_log_response.status_code == 200
    assert repeated_player_game_log_response.data == player_game_log_response.data
    matchup = matchup_response.get_json()
    assert_matchup_freshness_envelope(
        matchup,
        source="ledger",
        expected_retrieved_at=legacy_retrieved_at.isoformat(),
    )
    for stream_key in (
        *matchup_publications["season"].keys(),
        *matchup_publications["l15"].keys(),
    ):
        provenance = matchup["provenance"][stream_key]
        assert provenance["source"] == "database"
        assert provenance["legacy_fallback_allowed"] is False
        assert provenance["publication_id"] == {
            **matchup_publications["season"],
            **matchup_publications["l15"],
        }[stream_key]
        assert provenance["manifest_id"] == matchup_governance.manifest_id
        assert provenance["event_catalog_publication_id"] == (
            matchup_governance.event_catalog_publication_id
        )
        assert provenance["event_catalog_checksum"] == (
            matchup_governance.event_catalog_checksum
        )
        assert provenance["retrieved_at"] == NOW.isoformat()
    assert matchup["provenance"]["player_game_logs"]["status"] == "rollback"
    assert matchup["provenance"]["player_game_logs"]["publication_id"] == rollback.resource.publication_id
    assert matchup["provenance"]["synergy_play_types"]["publication_id"] == diet_candidate
    assert matchup["provenance"]["synergy:l15"]["unavailable_reason"] == "provider_window_unsupported"
    assert matchup["coverage"]["mixed_cutoff"]
    assert matchup["coverage"]["mixed_freshness"]
    assert matchup["league"]["surface_availability"]["play_types"]["last_15"] == {
        "status": "unavailable",
        "unavailable_reason": "provider_window_unsupported",
    }
    assert matchup["freshness"]["team_matchups"]["last_15"]["surfaces"][
        "play_types"
    ] == {
        "status": "unavailable",
        "unavailable_reason": "provider_window_unsupported",
        "retrieved_at": legacy_retrieved_at.isoformat(),
    }
    assert selection_response.get_json()["h2h"]["rows"]
    assert player_game_log_response.get_json()["game_logs"][0]["PTS"] == 25
    assert provider_calls == {"nba": 0, "pbp": 0}


@pytest.fixture
def projection_route_context(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'projection-route.sqlite3'}"
    engine = create_engine(database_url)
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        database={"url": database_url},
        features=FeatureSettings(projection_archive_read_enabled=True),
        providers=ProviderSettings(
            dfs_enabled_providers=("dabble", "prizepicks"),
        ),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    monkeypatch.setattr("app.utils.db.get_engine", lambda _settings=None: engine)
    monkeypatch.setattr(
        "app.providers.nba_stats.NBAStatsAdapter",
        lambda **_kwargs: _NoProvider(),
    )
    monkeypatch.setattr(
        "app.providers.pbp_stats.PBPStatsAdapter",
        lambda **_kwargs: _NoProvider(),
    )
    monkeypatch.setattr(
        "app.providers.pbp_game_logs.PBPGameLogAdapter",
        lambda **_kwargs: _NoProvider(),
    )
    assembled = build_dependencies(settings)
    assert isinstance(
        assembled.projection_player_pool_reader,
        LatestProjectionPlayerPoolReader,
    )
    assert assembled.slate_service.player_pool is assembled.projection_player_pool_reader
    assert assembled.matchup_service.player_pool is assembled.projection_player_pool_reader

    catalog = StatisticCatalog.load_default()
    event_catalog = _event_catalog(engine, settings)
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    player_logs = _player_logs(engine, catalog)
    player_diets = _player_diets(engine)
    team_matchups = _team_matchups(engine)
    archetypes = assembled.matchup_selection_service.archetypes
    route_now = [NOW]

    def route_client(dependencies):
        template = dependencies.projection_player_pool_reader
        assert isinstance(template, LatestProjectionPlayerPoolReader)
        reader = LatestProjectionPlayerPoolReader(
            engine,
            template.scopes,
            clock=lambda: route_now[0],
            required_providers=template.required_providers,
        )
        route_settings = dependencies.settings
        route_dependencies = replace(
            dependencies,
            projection_player_pool_reader=reader,
            slate_service=SlateService(
                event_catalog,
                settings=route_settings,
                player_pool=reader,
                injuries=None,
                clock=lambda: route_now[0],
            ),
            matchup_service=MatchupService(
                event_catalog=event_catalog,
                player_pool=reader,
                player_logs=player_logs,
                player_diets=player_diets,
                team_matchups=team_matchups,
                stats_freshness=stats_freshness,
                settings=route_settings,
                injuries=None,
                clock=lambda: route_now[0],
            ),
            matchup_selection_service=MatchupSelectionService(
                event_catalog=event_catalog,
                player_pool=ProjectionSelectionPlayerPoolReader(reader),
                player_logs=player_logs,
                archetypes=archetypes,
                statistic_catalog=catalog,
                settings=route_settings,
                publication_reader=dependencies.publication_reader,
            ),
        )
        route_app = create_app(
            {
                "TESTING": True,
                "RUNTIME_SETTINGS": route_settings,
                "DEPENDENCIES": route_dependencies,
                "SKIP_FIREBASE_INIT": True,
                "SKIP_TABLE_CREATE": True,
            }
        )
        return route_app.test_client(), reader

    client, pool = route_client(assembled)

    return SimpleNamespace(
        engine=engine,
        settings=settings,
        assembled=assembled,
        catalog=catalog,
        route_now=route_now,
        route_client=route_client,
        client=client,
        pool=pool,
    )


def test_authenticated_projection_routes_distinguish_missing_and_complete_empty(
    projection_route_context,
):
    context = projection_route_context
    settings = context.settings
    assembled = context.assembled
    route_now = context.route_now
    route_client = context.route_client
    client = context.client
    missing_selection = client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=2544"
    )
    assert missing_selection.status_code == 503
    assert missing_selection.get_json()["error"]["code"] == "provider_unavailable"

    query = NBAMarketQuery(season=SEASON)
    preflight_empty_at = NOW - timedelta(minutes=1)
    assembled.projection_recorder.record_complete_snapshot(
        ProviderSnapshot(
            provider="prizepicks",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(
                fetched_count=0,
                eligible_count=0,
                normalized_count=0,
                expected_total=0,
            ),
            retrieved_at=preflight_empty_at,
        ),
        query=query,
        accepted_at=preflight_empty_at,
    )
    dabble_only_settings = settings.model_copy(
        update={
            "providers": settings.providers.model_copy(
                update={"dfs_enabled_providers": ("dabble",)}
            )
        }
    )
    dabble_only_dependencies = build_dependencies(dabble_only_settings)
    dabble_only_client, dabble_only_reader = route_client(dabble_only_dependencies)
    assert isinstance(dabble_only_reader, LatestProjectionPlayerPoolReader)
    disabled_empty_matchup = dabble_only_client.get(
        f"/api/games/matchup?game_id={GAME_ID}"
    )
    disabled_empty_slate = dabble_only_client.get(
        "/api/games/slate?date=2026-01-15"
    )
    assert disabled_empty_matchup.get_json()["freshness"]["pool"] == {
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {
            "dabble": {"status": "missing", "retrieved_at": None},
            "prizepicks": {
                "status": "fresh",
                "retrieved_at": preflight_empty_at.isoformat(),
            },
        },
    }
    assert disabled_empty_slate.get_json()["games"][0]["projection_state"] == {
        "state": "missing",
        "observed_at": None,
    }

    mixed_empty_matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    mixed_empty_slate = client.get("/api/games/slate?date=2026-01-15")
    assert mixed_empty_matchup.get_json()["freshness"]["pool"]["state"] == "missing"
    assert "status" not in mixed_empty_matchup.get_json()["freshness"]["pool"]
    assert mixed_empty_slate.get_json()["games"][0]["projection_state"] == {
        "state": "missing",
        "observed_at": None,
    }

    empty_registry_settings = settings.model_copy(
        update={
            "providers": settings.providers.model_copy(
                update={"dfs_enabled_providers": ()}
            )
        }
    )
    empty_registry_dependencies = build_dependencies(empty_registry_settings)
    empty_registry_client, empty_registry_reader = route_client(
        empty_registry_dependencies
    )
    assert isinstance(empty_registry_reader, LatestProjectionPlayerPoolReader)
    all_disabled_empty_matchup = empty_registry_client.get(
        f"/api/games/matchup?game_id={GAME_ID}"
    )
    all_disabled_empty_slate = empty_registry_client.get(
        "/api/games/slate?date=2026-01-15"
    )
    assert all_disabled_empty_matchup.get_json()["freshness"]["pool"]["state"] == "live"
    assert all_disabled_empty_matchup.get_json()["players"] == []
    assert all_disabled_empty_slate.get_json()["games"][0]["projection_state"] == {
        "state": "live",
        "observed_at": preflight_empty_at.isoformat(),
    }
    route_now[0] = preflight_empty_at + timedelta(
        minutes=15,
        microseconds=1,
    )
    expired_empty_matchup = empty_registry_client.get(
        f"/api/games/matchup?game_id={GAME_ID}"
    )
    expired_empty_slate = empty_registry_client.get(
        "/api/games/slate?date=2026-01-15"
    )
    assert expired_empty_matchup.get_json()["freshness"]["pool"] == {
        "status": "unavailable",
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {},
    }
    assert expired_empty_slate.get_json()["games"][0]["projection_state"] == {
        "state": "missing",
        "observed_at": None,
    }


def test_authenticated_projection_routes_cover_partial_failure_and_recovery(
    projection_route_context,
):
    context = projection_route_context
    engine = context.engine
    assembled = context.assembled
    catalog = context.catalog
    route_now = context.route_now
    client = context.client
    query = NBAMarketQuery(season=SEASON)
    route_now[0] = NOW
    recorded_snapshot = _recorded_projection_snapshot(catalog)
    prizepicks_snapshot = _recorded_projection_snapshot(catalog, provider="prizepicks")
    for snapshot in (recorded_snapshot, prizepicks_snapshot):
        assembled.projection_recorder.record_complete_snapshot(
            snapshot,
            query=query,
            accepted_at=NOW,
        )

    unchanged_at = NOW + timedelta(minutes=1)
    unchanged_snapshots = tuple(
        replace(snapshot, retrieved_at=unchanged_at)
        for snapshot in (recorded_snapshot, prizepicks_snapshot)
    )
    for snapshot in unchanged_snapshots:
        assembled.projection_recorder.record_complete_snapshot(
            snapshot,
            query=query,
            accepted_at=unchanged_at,
        )
    assembled.projection_recorder.record_complete_snapshot(
        unchanged_snapshots[0],
        query=query,
        accepted_at=unchanged_at,
    )

    rematerialized_at = NOW + timedelta(minutes=2)
    route_now[0] = rematerialized_at
    rematerialized_catalog = StatisticCatalog(
        statistics=tuple(
            replace(statistic, market_category="PRA")
            if statistic.id == "points"
            else statistic
            for statistic in catalog.statistics
        )
    )
    rematerializing_recorder = ProjectionRecordingService(
        ProjectionArchive(engine, rematerialized_catalog),
        tuple(assembled.projection_recorder.scopes.values()),
        default_scope=assembled.projection_recorder.default_scope,
    )
    for snapshot in (recorded_snapshot, prizepicks_snapshot):
        rematerializing_recorder.record_complete_snapshot(
            replace(snapshot, retrieved_at=rematerialized_at),
            query=query,
            accepted_at=rematerialized_at,
        )

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one() == 6
        assert connection.execute(
            select(func.count()).select_from(ProjectionProviderSnapshot)
        ).scalar_one() == 2
        assert connection.execute(
            select(func.count()).select_from(ProjectionMaterializationGeneration)
        ).scalar_one() == 4
        assert connection.execute(
            select(func.count()).select_from(ProjectionObservation)
        ).scalar_one() == 4

    slate = client.get("/api/games/slate?date=2026-01-15")
    matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")

    assert slate.status_code == 200
    assert matchup.status_code == 200
    assert slate.get_json()["games"][0]["projection_state"] == {
        "state": "live",
        "observed_at": rematerialized_at.isoformat(),
    }
    assert matchup.get_json()["freshness"]["pool"] == {
        "status": "fresh",
        "state": "live",
        "observed_at": rematerialized_at.isoformat(),
        "retrieved_at": rematerialized_at.isoformat(),
        "providers": {
            "dabble": {
                "status": "fresh",
                "retrieved_at": rematerialized_at.isoformat(),
            },
            "prizepicks": {
                "status": "fresh",
                "retrieved_at": rematerialized_at.isoformat(),
            },
        },
    }
    assert [player["canonical_id"] for player in matchup.get_json()["players"]] == [
        2544
    ]

    with engine.connect() as connection:
        before_rejection = (
            connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one(),
            connection.execute(
                select(func.count()).select_from(ProjectionMaterializationGeneration)
            ).scalar_one(),
            connection.execute(
                select(func.count()).select_from(ProjectionObservation)
            ).scalar_one(),
            tuple(
                connection.execute(
                    select(
                        LatestPlayerProjection.provider,
                        LatestPlayerProjection.generation_id,
                    ).order_by(LatestPlayerProjection.provider)
                ).all()
            ),
        )
    with pytest.raises(ValueError, match="outside the configured recording scope"):
        assembled.projection_recorder.record_complete_snapshot(
            _recorded_projection_snapshot(catalog, provider="underdog"),
            query=query,
            accepted_at=rematerialized_at,
        )
    with engine.connect() as connection:
        after_rejection = (
            connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one(),
            connection.execute(
                select(func.count()).select_from(ProjectionMaterializationGeneration)
            ).scalar_one(),
            connection.execute(
                select(func.count()).select_from(ProjectionObservation)
            ).scalar_one(),
            tuple(
                connection.execute(
                    select(
                        LatestPlayerProjection.provider,
                        LatestPlayerProjection.generation_id,
                    ).order_by(LatestPlayerProjection.provider)
                ).all()
            ),
        )
    assert after_rejection == before_rejection

    partial_at = rematerialized_at + timedelta(minutes=1)
    partial = replace(
        recorded_snapshot,
        status=SnapshotStatus.PARTIAL,
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=2,
            warning_codes=("page_fetch_failed",),
        ),
        retrieved_at=partial_at,
    )
    assembled.projection_recorder.record_snapshot(
        partial,
        query=query,
        accepted_at=partial_at,
    )
    route_now[0] = partial_at
    assert client.get(f"/api/games/matchup?game_id={GAME_ID}").get_json()[
        "freshness"
    ]["pool"]["status"] == "fresh"

    failed_at = partial_at + timedelta(minutes=16)
    failure_started_at = failed_at - timedelta(minutes=1)
    assembled.projection_recorder.record_failed_poll(
        provider="dabble",
        query=query,
        poll_started_at=failure_started_at,
        completed_at=failed_at,
        failure_reason="access_denied",
    )
    route_now[0] = failed_at
    failed_matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    assert failed_matchup.status_code == 200
    failed_freshness = failed_matchup.get_json()["freshness"]["pool"]
    assert "status" not in failed_freshness
    assert failed_freshness["providers"] == {
        "dabble": {
            "status": "stale-served",
            "retrieved_at": partial_at.isoformat(),
        },
        "prizepicks": {"status": "missing", "retrieved_at": None},
    }
    assert [
        player["canonical_id"] for player in failed_matchup.get_json()["players"]
    ] == [2544]

    late_accepted_at = failed_at + timedelta(seconds=1)
    late_retrieved_at = partial_at + timedelta(minutes=10)
    assembled.projection_recorder.record_complete_snapshot(
        replace(
            recorded_snapshot,
            markets=(
                replace(recorded_snapshot.markets[0], market_id="late-market"),
            ),
            retrieved_at=late_retrieved_at,
        ),
        query=query,
        accepted_at=late_accepted_at,
    )
    route_now[0] = late_accepted_at
    late_matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    assert "status" not in late_matchup.get_json()["freshness"]["pool"]
    assert [player["canonical_id"] for player in late_matchup.get_json()["players"]] == [2544]

    assembled.projection_recorder.record_complete_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(
                fetched_count=0,
                eligible_count=0,
                normalized_count=0,
                expected_total=0,
            ),
            retrieved_at=late_retrieved_at,
        ),
        query=query,
        accepted_at=late_accepted_at + timedelta(seconds=1),
    )
    route_now[0] = late_accepted_at + timedelta(seconds=1)
    late_empty_matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    late_empty_slate = client.get("/api/games/slate?date=2026-01-15")
    assert [
        player["canonical_id"]
        for player in late_empty_matchup.get_json()["players"]
    ] == [2544]
    assert late_empty_matchup.get_json()["freshness"]["pool"]["providers"][
        "dabble"
    ]["status"] == "stale-served"
    assert late_empty_slate.get_json()["games"][0]["projection_state"] == {
        "state": "live",
        "observed_at": partial_at.isoformat(),
    }

    recovered_at = failed_at + timedelta(minutes=1)
    assembled.projection_recorder.record_complete_snapshot(
        replace(recorded_snapshot, retrieved_at=recovered_at),
        query=query,
        accepted_at=recovered_at,
    )
    route_now[0] = recovered_at
    recovered_matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    assert [
        player["canonical_id"] for player in recovered_matchup.get_json()["players"]
    ] == [2544]
    assert recovered_matchup.get_json()["freshness"]["pool"]["providers"][
        "dabble"
    ] == {
        "status": "fresh",
        "retrieved_at": recovered_at.isoformat(),
    }

    empty_at = failed_at + timedelta(minutes=2)
    for provider in ("dabble", "prizepicks"):
        assembled.projection_recorder.record_complete_snapshot(
            ProviderSnapshot(
                provider=provider,
                status=SnapshotStatus.COMPLETE,
                markets=(),
                coverage=CoverageEvidence(
                    fetched_count=0,
                    eligible_count=0,
                    normalized_count=0,
                    expected_total=0,
                ),
                retrieved_at=empty_at,
            ),
            query=query,
            accepted_at=empty_at,
        )
    route_now[0] = empty_at
    empty_slate = client.get("/api/games/slate?date=2026-01-15")
    empty_matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    assert empty_slate.status_code == 200
    assert empty_matchup.status_code == 200
    empty_game = empty_slate.get_json()["games"][0]
    assert empty_game["projection_state"] == {
        "state": "live",
        "observed_at": empty_at.isoformat(),
    }
    assert empty_game["away_team"]["targetable_player_count"] == 0
    assert empty_game["home_team"]["targetable_player_count"] == 0
    assert empty_matchup.get_json()["players"] == []
    assert empty_matchup.get_json()["freshness"]["pool"] == {
        "status": "fresh",
        "state": "live",
        "observed_at": empty_at.isoformat(),
        "retrieved_at": empty_at.isoformat(),
        "providers": {
            provider: {
                "status": "fresh",
                "retrieved_at": empty_at.isoformat(),
            }
            for provider in ("dabble", "prizepicks")
        },
    }
    empty_selection = client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=2544"
    )
    assert empty_selection.status_code == 404
    assert empty_selection.get_json()["error"]["code"] == "resource_not_found"


def test_authenticated_projection_routes_preserve_disabled_history_and_expiry(
    projection_route_context,
):
    context = projection_route_context
    engine = context.engine
    settings = context.settings
    assembled = context.assembled
    catalog = context.catalog
    route_now = context.route_now
    route_client = context.route_client
    query = NBAMarketQuery(season=SEASON)
    recorded_snapshot = _recorded_projection_snapshot(catalog)
    prizepicks_snapshot = _recorded_projection_snapshot(
        catalog,
        provider="prizepicks",
    )
    disabled_at = NOW
    for snapshot in (recorded_snapshot, prizepicks_snapshot):
        assembled.projection_recorder.record_complete_snapshot(
            snapshot,
            query=query,
            accepted_at=disabled_at,
        )
    all_disabled_settings = settings.model_copy(
        update={
            "providers": settings.providers.model_copy(
                update={"dfs_enabled_providers": ()}
            )
        }
    )
    all_disabled_dependencies = build_dependencies(all_disabled_settings)
    all_disabled_pool = all_disabled_dependencies.projection_player_pool_reader
    all_disabled_recorder = all_disabled_dependencies.projection_recorder
    assert isinstance(all_disabled_pool, LatestProjectionPlayerPoolReader)
    assert isinstance(all_disabled_recorder, ProjectionRecordingService)
    assert all_disabled_recorder.scopes == {}
    assert {scope.provider for scope in all_disabled_pool.scopes} == {
        "dabble",
        "prizepicks",
        "underdog",
    }
    assert all_disabled_pool.required_providers == frozenset()
    with engine.connect() as connection:
        before_disabled_rejections = (
            connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one(),
            tuple(
                connection.execute(
                    select(
                        LatestPlayerProjection.provider,
                        LatestPlayerProjection.generation_id,
                        LatestPlayerProjection.confirmed_at,
                    ).order_by(LatestPlayerProjection.provider)
                ).all()
            ),
        )
    rejected_at = disabled_at + timedelta(minutes=14)
    for snapshot in (recorded_snapshot, prizepicks_snapshot):
        for _attempt in range(2):
            with pytest.raises(
                ValueError,
                match="outside the configured recording scope",
            ):
                all_disabled_recorder.record_complete_snapshot(
                    replace(snapshot, retrieved_at=rejected_at),
                    query=query,
                    accepted_at=rejected_at,
                )
            with pytest.raises(
                ValueError,
                match="outside the configured recording scope",
            ):
                all_disabled_recorder.record_failed_poll(
                    provider=snapshot.provider,
                    query=query,
                    completed_at=rejected_at,
                    failure_reason="access_denied",
                )
    with engine.connect() as connection:
        after_disabled_rejections = (
            connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one(),
            tuple(
                connection.execute(
                    select(
                        LatestPlayerProjection.provider,
                        LatestPlayerProjection.generation_id,
                        LatestPlayerProjection.confirmed_at,
                    ).order_by(LatestPlayerProjection.provider)
                ).all()
            ),
        )
    assert after_disabled_rejections == before_disabled_rejections
    all_disabled_client, controlled_all_disabled_pool = route_client(
        all_disabled_dependencies
    )
    assert controlled_all_disabled_pool.required_providers == frozenset()
    route_now[0] = disabled_at + timedelta(minutes=14)
    disabled_live_matchup = all_disabled_client.get(
        f"/api/games/matchup?game_id={GAME_ID}"
    )
    assert disabled_live_matchup.status_code == 200
    assert [
        player["canonical_id"]
        for player in disabled_live_matchup.get_json()["players"]
    ] == [2544]
    assert disabled_live_matchup.get_json()["freshness"]["pool"] == {
        "status": "fresh",
        "state": "live",
        "observed_at": disabled_at.isoformat(),
        "retrieved_at": disabled_at.isoformat(),
        "providers": {
            "dabble": {
                "status": "fresh",
                "retrieved_at": disabled_at.isoformat(),
            },
            "prizepicks": {
                "status": "fresh",
                "retrieved_at": disabled_at.isoformat(),
            },
        },
    }
    all_disabled_at = disabled_at + timedelta(minutes=16)
    route_now[0] = all_disabled_at
    all_disabled_matchup = all_disabled_client.get(
        f"/api/games/matchup?game_id={GAME_ID}"
    )
    assert all_disabled_matchup.status_code == 200
    assert all_disabled_matchup.get_json()["players"] == []
    assert all_disabled_matchup.get_json()["freshness"]["pool"] == {
        "status": "unavailable",
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {},
    }
    all_disabled_selection = all_disabled_client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=2544"
    )
    assert all_disabled_selection.status_code == 503
    assert all_disabled_selection.get_json()["error"]["code"] == "provider_unavailable"

    partially_enabled_settings = settings.model_copy(
        update={
            "providers": settings.providers.model_copy(
                update={"dfs_enabled_providers": ("dabble",)}
            )
        }
    )
    partially_enabled_dependencies = build_dependencies(partially_enabled_settings)
    partially_enabled_pool = partially_enabled_dependencies.projection_player_pool_reader
    partially_enabled_recorder = partially_enabled_dependencies.projection_recorder
    assert isinstance(partially_enabled_pool, LatestProjectionPlayerPoolReader)
    assert isinstance(partially_enabled_recorder, ProjectionRecordingService)
    assert set(partially_enabled_recorder.scopes) == {"dabble"}
    assert {scope.provider for scope in partially_enabled_pool.scopes} == {
        "dabble",
        "prizepicks",
        "underdog",
    }
    assert partially_enabled_pool.required_providers == frozenset({"dabble"})
    partially_enabled_recorder.record_complete_snapshot(
        replace(recorded_snapshot, retrieved_at=all_disabled_at),
        query=query,
        accepted_at=all_disabled_at,
    )
    with pytest.raises(ValueError, match="outside the configured recording scope"):
        partially_enabled_recorder.record_complete_snapshot(
            replace(prizepicks_snapshot, retrieved_at=all_disabled_at),
            query=query,
            accepted_at=all_disabled_at,
        )
    with pytest.raises(ValueError, match="outside the configured recording scope"):
        partially_enabled_recorder.record_failed_poll(
            provider="prizepicks",
            query=query,
            completed_at=all_disabled_at,
            failure_reason="access_denied",
        )
    partially_enabled_client, controlled_partially_enabled_pool = route_client(
        partially_enabled_dependencies
    )
    assert controlled_partially_enabled_pool.required_providers == frozenset(
        {"dabble"}
    )
    route_now[0] = all_disabled_at
    partially_enabled_matchup = partially_enabled_client.get(
        f"/api/games/matchup?game_id={GAME_ID}"
    )
    assert partially_enabled_matchup.status_code == 200
    assert partially_enabled_matchup.get_json()["freshness"]["pool"] == {
        "status": "fresh",
        "state": "live",
        "observed_at": all_disabled_at.isoformat(),
        "retrieved_at": all_disabled_at.isoformat(),
        "providers": {
            "dabble": {
                "status": "fresh",
                "retrieved_at": all_disabled_at.isoformat(),
            }
        },
    }
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(LatestPlayerProjection).where(
                LatestPlayerProjection.provider == "prizepicks"
            )
        ).scalar_one() == 1
