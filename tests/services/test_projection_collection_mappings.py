"""The collector archives each line with the player and game the board resolved."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

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
from app.services.mapping_outcomes import (
    apply_mapping_outcomes,
    athlete_mappings,
    event_mappings,
)
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
NOW = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
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
        coverage=CoverageEvidence(fetched_count=2, eligible_count=2, normalized_count=2, expected_total=2),
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
    def __init__(self, clock, *, with_outcomes: bool, athletes=None, events=None, snapshot=None):
        self.snapshot = snapshot
        self.clock = clock
        self.with_outcomes = with_outcomes
        self.athletes = athletes
        self.events = events

    def get_board(self, query, *, providers):
        provider_outcome = SimpleNamespace(
            provider="dabble",
            status="complete",
            snapshot=self.snapshot or _snapshot(self.clock()),
            reason=None,
        )
        athletes = self.athletes or (
            _athlete_outcome("dabble", "a-1", 2544, 1610612747, "LeBron James"),
            _ambiguous_outcome("a-2"),
        )
        events = self.events or (_event_outcome("dabble", "event-9", GAME_ID, (1610612747, 1610612738)),)
        return SimpleNamespace(
            provider_outcomes=(provider_outcome,),
            mapping_outcomes=athletes if self.with_outcomes else (),
            event_mapping_outcomes=events if self.with_outcomes else (),
        )


def _collector(tmp_path, clock, *, with_outcomes: bool, athletes=None, events=None, snapshot=None):
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
        board_service=_Board(
            clock,
            with_outcomes=with_outcomes,
            athletes=athletes,
            events=events,
            snapshot=snapshot,
        ),
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
            row.provider_market_id: row for row in connection.execute(select(ProjectionObservation)).all()
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
    assert [(row.canonical_player_id, row.canonical_game_id) for row in latest] == [(2544, GAME_ID)]


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
    coordinator, engine = _collector(tmp_path, lambda: clock_value[0], with_outcomes=False)
    assert coordinator.run().status == "complete"
    with engine.connect() as connection:
        assert connection.execute(select(LatestPlayerProjection)).all() == []

    # Cadence reads the database clock; rewind it so the provider is due again.
    with engine.begin() as connection:
        connection.execute(update(ProjectionCollectionProviderState.__table__).values(last_poll_at=NOW))
    clock_value[0] = NOW + timedelta(minutes=31)
    coordinator.board_service.with_outcomes = True
    assert coordinator.run().status == "complete"

    with engine.connect() as connection:
        latest = connection.execute(select(LatestPlayerProjection)).all()
    assert [row.canonical_player_id for row in latest] == [2544]


def _first_row_and_latest(tmp_path, **collector_args):
    coordinator, engine = _collector(tmp_path, lambda: NOW, with_outcomes=True, **collector_args)
    assert coordinator.run().status == "complete"
    with engine.connect() as connection:
        latest = connection.execute(select(LatestPlayerProjection)).all()
    return _rows(engine)["m-1"], latest


def test_player_whose_team_is_not_in_the_game_is_not_targetable(tmp_path):
    row, latest = _first_row_and_latest(
        tmp_path,
        athletes=(_athlete_outcome("dabble", "a-1", 303, 3, "Traded Player"),),
    )

    assert (
        row.targetable,
        row.canonical_player_id,
        row.canonical_team_id,
        row.canonical_game_id,
        row.unresolved_identities,
    ) == (False, 303, 3, None, "event")
    assert latest == []


def test_governed_player_that_disagrees_with_its_resolution_is_not_applied(tmp_path):
    outcome = _athlete_outcome("dabble", "a-1", 999, 1610612747, "Someone Else")
    outcome = replace(outcome, mapping=replace(outcome.mapping, canonical_player_id=2544))

    row, latest = _first_row_and_latest(tmp_path, athletes=(outcome,))

    assert (row.targetable, row.canonical_player_id, row.unresolved_identities) == (
        False,
        None,
        "athlete",
    )
    assert latest == []


def test_canonical_athlete_without_a_team_is_not_applied(tmp_path):
    row, latest = _first_row_and_latest(
        tmp_path,
        athletes=(_athlete_outcome("dabble", "a-1", 2544, None, "LeBron James"),),
    )

    assert (row.targetable, row.canonical_player_id, row.unresolved_identities) == (
        False,
        None,
        "athlete",
    )
    assert latest == []


def test_mappings_are_keyed_by_provider_as_well_as_provider_id():
    athletes = athlete_mappings(
        (
            _athlete_outcome("dabble", "a-1", 2544, 1610612747, "LeBron James"),
            _athlete_outcome("novig", "a-1", 201939, 1610612744, "Stephen Curry"),
        )
    )
    events = event_mappings(
        (
            _event_outcome("dabble", "event-9", GAME_ID, (1610612747, 1610612738)),
            _event_outcome("novig", "event-9", "0022500777", (1610612744, 1610612738)),
        )
    )

    market = apply_mapping_outcomes(_snapshot(NOW), athletes=athletes, events=events).markets[0]

    assert (
        market.athlete.canonical_id,
        market.athlete.name,
        market.athlete.team.canonical_id,
        market.event.canonical_id,
    ) == (2544, "LeBron James", 1610612747, GAME_ID)


def _recorded_dabble_snapshot():
    import json
    from pathlib import Path

    from app.providers.dabble import DabbleAdapter
    from app.providers.dfs import RetrievalContext

    folder = Path(__file__).parents[1] / "fixtures" / "dabble"
    payloads = [
        json.loads((folder / name).read_text("utf-8"))
        for name in (
            "recorded_2026-10-15.competitions.json",
            "recorded_2026-10-21.fixtures.json",
            "recorded_2026-10-21.fixture_details.json",
        )
    ]

    class Session:
        headers: ClassVar[dict] = {}

        def get(self, url, **kwargs):
            payload = payloads.pop(0)
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)

    snapshot = DabbleAdapter(session=Session()).get_snapshot(
        NBAMarketQuery(),
        RetrievalContext(deadline="2099-01-01T00:00:00Z", request_id="priced"),
    )
    resolver = CATALOG.resolver
    return replace(
        snapshot,
        retrieved_at=NOW,
        markets=tuple(
            replace(market, statistic_match=resolver.resolve_market(market)) for market in snapshot.markets
        ),
    )


def test_resolved_dabble_standard_line_is_targetable_and_alternate_is_not(tmp_path):
    coordinator, engine = _collector(
        tmp_path,
        lambda: NOW,
        with_outcomes=True,
        snapshot=_recorded_dabble_snapshot(),
        athletes=(
            _athlete_outcome(
                "dabble",
                "87f271b1-e22f-4c71-a55e-019492aa8f44",
                1642259,
                1610612764,
                "Alexandre Sarr",
            ),
        ),
        events=(
            _event_outcome(
                "dabble",
                "01a12b1a-2cb1-7702-8624-5f7115ebf0e0",
                GAME_ID,
                (1610612764, 1610612749),
            ),
        ),
    )

    assert coordinator.run().status == "complete"

    rows = _rows(engine)
    # Sarr threes 1.5: the O/U line and the priced "To Have 2+" over-only side.
    standard = rows["01a1475c-8428-7747-8aa1-05cac594a75e"]
    alternate = rows["01a1475c-87a9-7499-a8cb-9307c70e4bee"]
    assert (standard.targetable, standard.canonical_player_id) == (True, 1642259)
    assert alternate.targetable is False
