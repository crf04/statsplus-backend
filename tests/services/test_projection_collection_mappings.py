"""The collector archives each line with the player and game the board resolved."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, select, update

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.migrations import run_migrations
from app.models.projection_archive import LatestPlayerProjection, ProjectionObservation
from app.models.projection_collection import ProjectionCollectionProviderState
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    Selection,
    SelectionDirection,
    SnapshotStatus,
    StatisticEvidence,
)
from app.services.athlete_mapping_repository import BoardMappingOutcome
from app.services.athlete_resolver import AthleteResolution, MappingResolutionState
from app.services.projection_archive import (
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
)
from app.services.projection_collection import (
    ProjectionCollectionCoordinator,
    ProjectionCollectionSettings,
)
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_player_pool import _athlete_outcome, _event_outcome

SEASON = "2025-26"
NOW = datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc)
GAME_ID = "0022500001"
CATALOG = StatisticCatalog.load_default()


def _market(athlete_id: str, market_id: str) -> PlayerProjectionMarket:
    statistic = CATALOG.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", label="Points")
    return PlayerProjectionMarket(
        provider="dabble",
        market_id=market_id,
        athlete=AthleteEvidence(provider_id=athlete_id, name="Provider Spelling"),
        event=EventEvidence(provider_id="event-9"),
        statistic=evidence,
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=statistic,
            provider="dabble",
        ),
        threshold=MarketThreshold("27.5", "count"),
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
        selections=(
            Selection(
                selection_id=f"higher-{market_id}",
                direction=SelectionDirection.HIGHER,
                american_price=-110,
            ),
        ),
    )


def _snapshot(retrieved_at: datetime) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(_market("a-1", "m-1"), _market("a-2", "m-2")),
        coverage=CoverageEvidence(
            fetched_count=2, eligible_count=2, normalized_count=2, expected_total=2
        ),
        retrieved_at=retrieved_at,
    )


def _ambiguous_outcome(provider_id: str) -> BoardMappingOutcome:
    return BoardMappingOutcome(
        resolution=AthleteResolution(
            provider="dabble",
            provider_evidence=AthleteEvidence(provider_id=provider_id),
            season=SEASON,
            state=MappingResolutionState.AMBIGUOUS,
        ),
        state=MappingResolutionState.AMBIGUOUS,
        persisted=True,
    )


class _Board:
    def __init__(self, clock, *, with_outcomes: bool):
        self.clock = clock
        self.with_outcomes = with_outcomes

    def get_board(self, query, *, providers):
        provider_outcome = SimpleNamespace(
            provider="dabble",
            status="complete",
            snapshot=_snapshot(self.clock()),
            reason=None,
        )
        athletes = (
            _athlete_outcome("dabble", "a-1", 2544, 1610612747, "LeBron James"),
            _ambiguous_outcome("a-2"),
        )
        events = (
            _event_outcome("dabble", "event-9", GAME_ID, (1610612747, 1610612738)),
        )
        return SimpleNamespace(
            provider_outcomes=(provider_outcome,),
            mapping_outcomes=athletes if self.with_outcomes else (),
            event_mapping_outcomes=events if self.with_outcomes else (),
        )


def _collector(tmp_path, clock, *, with_outcomes: bool):
    engine = create_engine(f"sqlite:///{tmp_path / 'collector-mappings.sqlite3'}")
    run_migrations(engine)
    query = NBAMarketQuery(season=SEASON)
    recorder = ProjectionRecordingService(
        ProjectionArchive(engine, CATALOG),
        ProjectionArchiveReadScope(provider="dabble", query=query),
    )
    event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": NOW + timedelta(hours=1),
        "status_text": "Scheduled",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
    }
    coordinator = ProjectionCollectionCoordinator(
        engine,
        board_service=_Board(clock, with_outcomes=with_outcomes),
        recording_service=recorder,
        event_reader=lambda _season: (event,),
        season=SEASON,
        settings=ProjectionCollectionSettings(
            slow_interval=timedelta(minutes=30),
            fast_interval=timedelta(minutes=5),
            pregame_horizon=timedelta(hours=24),
            fast_window=timedelta(hours=2),
            lease_duration=timedelta(minutes=1),
            backoff_base=timedelta(minutes=1),
            backoff_max=timedelta(hours=1),
        ),
        providers=("dabble",),
        clock=clock,
        owner="mapping-collector",
    )
    return coordinator, engine


def _rows(engine):
    with engine.connect() as connection:
        return {
            row.provider_market_id: row
            for row in connection.execute(select(ProjectionObservation)).all()
        }


def test_collector_archives_auto_mapped_lines_as_targetable(tmp_path):
    coordinator, engine = _collector(tmp_path, lambda: NOW, with_outcomes=True)

    assert coordinator.run().status == "complete"

    rows = _rows(engine)
    mapped = rows["m-1"]
    assert (
        mapped.targetable,
        mapped.canonical_player_id,
        mapped.canonical_player_name,
        mapped.canonical_team_id,
        mapped.canonical_game_id,
        mapped.unresolved_identities,
    ) == (True, 2544, "LeBron James", 1610612747, GAME_ID, "")
    with engine.connect() as connection:
        latest = connection.execute(select(LatestPlayerProjection)).all()
    assert [(row.canonical_player_id, row.canonical_game_id) for row in latest] == [
        (2544, GAME_ID)
    ]


def test_collector_leaves_an_ambiguous_athlete_untargetable(tmp_path):
    coordinator, engine = _collector(tmp_path, lambda: NOW, with_outcomes=True)

    coordinator.run()

    ambiguous = _rows(engine)["m-2"]
    assert (
        ambiguous.targetable,
        ambiguous.canonical_player_id,
        ambiguous.canonical_game_id,
        ambiguous.unresolved_identities,
    ) == (False, None, GAME_ID, "athlete")


def test_next_poll_promotes_a_resolved_generation_over_an_unresolved_one(tmp_path):
    clock_value = [NOW]
    coordinator, engine = _collector(
        tmp_path, lambda: clock_value[0], with_outcomes=False
    )
    assert coordinator.run().status == "complete"
    with engine.connect() as connection:
        assert connection.execute(select(LatestPlayerProjection)).all() == []

    # Cadence reads the database clock; rewind it so the provider is due again.
    with engine.begin() as connection:
        connection.execute(
            update(ProjectionCollectionProviderState.__table__).values(
                last_poll_at=NOW
            )
        )
    clock_value[0] = NOW + timedelta(minutes=31)
    coordinator.board_service.with_outcomes = True
    assert coordinator.run().status == "complete"

    with engine.connect() as connection:
        latest = connection.execute(select(LatestPlayerProjection)).all()
    assert [row.canonical_player_id for row in latest] == [2544]
