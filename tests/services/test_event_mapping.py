"""Contract tests for season-scoped provider event mappings."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, insert, inspect, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.config.settings import CatalogSettings, RuntimeSettings
from app.migrations import run_migrations
from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh
from app.models.event_mapping import (
    EVENT_MAPPING_DECISION_STATES,
    EVENT_MAPPING_STATES,
    EventMappingDecision,
    EventMappingDecisionCandidate,
    EventMappingLock,
    EventMappingRejection,
    ProviderEventMapping,
)
from app.providers.dfs import (
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.dfs_board import DFSBoardService
from app.services.event_mapping_errors import EventMappingPersistenceError
from app.services.event_mapping_repository import EventMappingRepository
from app.services.event_resolver import (
    EventResolutionState,
    EventResolver,
    normalize_team_label,
)

SEASON = "2025-26"
LAL = 1610612747
SAS = 1610612759
BOS = 1610612738

#: The scheduled tip-off every case is measured against.
TIP_OFF = datetime(2025, 10, 23, 0, 0, tzinfo=timezone.utc)

#: Provider observation instants for out-of-order board reads.  The operator
#: fixture clock sits between them.
_OBSERVED_BEFORE = datetime(2025, 10, 22, 11, tzinfo=timezone.utc)
_NOW = datetime(2025, 10, 22, 12, tzinfo=timezone.utc)
_OBSERVED_AFTER = datetime(2025, 10, 22, 13, tzinfo=timezone.utc)


def _event_row(
    game_id: str = "0022500001",
    *,
    offset_hours: float = 0.0,
    home_team_id: int = LAL,
    home_team_name: str = "Los Angeles Lakers",
    home_team_tricode: str = "LAL",
    away_team_id: int = SAS,
    away_team_name: str = "San Antonio Spurs",
    away_team_tricode: str = "SAS",
    season: str = SEASON,
    postponed_status: str | None = None,
) -> dict[str, object]:
    return {
        "nba_game_id": game_id,
        "season": season,
        "scheduled_at": TIP_OFF + timedelta(hours=offset_hours),
        "home_team_id": home_team_id,
        "home_team_name": home_team_name,
        "home_team_tricode": home_team_tricode,
        "away_team_id": away_team_id,
        "away_team_name": away_team_name,
        "away_team_tricode": away_team_tricode,
        "status_text": "Postponed" if postponed_status else "Scheduled",
        "status_code": 1,
        "postponed_status": postponed_status,
        "postponement_evidence": None,
        "classification": "Regular Season",
        "first_seen_at": _NOW,
        "last_seen_at": _NOW,
    }


class FakeEventCatalog:
    """The two read seams ``EventResolver`` requires, without a database."""

    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        fresh: bool = True,
        refreshed: bool = True,
    ) -> None:
        self.rows = rows
        self.fresh = fresh
        self.refreshed = refreshed
        self.requested_seasons: list[str] = []

    def get_events(self, season: str):
        self.requested_seasons.append(season)
        return [
            {**row, "is_postponed": bool(row["postponed_status"])}
            for row in self.rows
            if row["season"] == season
        ]

    def get_freshness(self, season: str):
        return {
            "season": season,
            "fresh": self.fresh,
            "last_success_at": _NOW.isoformat() if self.refreshed else None,
        }


def _evidence(
    provider_id: str | None = "ud-1",
    *,
    starts_at: datetime | None = TIP_OFF,
    label: str | None = "Lakers vs Spurs",
    home: TeamEvidence | None = None,
    away: TeamEvidence | None = None,
    status_label: str | None = "scheduled",
    canonical_id: str | None = None,
) -> EventEvidence:
    return EventEvidence(
        provider_id=provider_id,
        canonical_id=canonical_id,
        label=label,
        starts_at=starts_at,
        status_label=status_label,
        home_team=home if home is not None else TeamEvidence(abbreviation="LAL"),
        away_team=away if away is not None else TeamEvidence(abbreviation="SAS"),
    )


def _resolver(
    rows: list[dict[str, object]] | None = None,
    *,
    repository=None,
    fresh: bool = True,
    refreshed: bool = True,
    match_window=None,
    settings=None,
) -> EventResolver:
    catalog = FakeEventCatalog(
        rows if rows is not None else [_event_row()], fresh=fresh, refreshed=refreshed
    )
    return EventResolver(
        catalog,
        mapping_repository=repository,
        match_window=match_window,
        settings=settings,
    )


# -- resolution --------------------------------------------------------------


def test_team_label_normalization_ignores_case_and_punctuation():
    assert normalize_team_label("L.A. Lakers") == normalize_team_label("la lakers")
    assert normalize_team_label(None) == ""


def test_unique_nearby_matchup_maps_and_retains_typed_evidence():
    resolver = _resolver()

    result = resolver.resolve(
        "underdog",
        _evidence(
            home=TeamEvidence(provider_id="ud-lal", name="Los Angeles Lakers"),
            away=TeamEvidence(canonical_id=SAS, abbreviation="SAS"),
        ),
        SEASON,
    )

    assert result.state is EventResolutionState.AUTO
    assert result.reason == "canonical_matchup_within_window"
    assert result.canonical_event_id == "0022500001"
    assert result.canonical_event.home_team_tricode == "LAL"
    assert result.canonical_event.start_offset_seconds == 0
    assert result.is_durable
    # The provider's own evidence survives beside the canonical result.
    assert result.provider_evidence.label == "Lakers vs Spurs"
    assert result.provider_evidence.starts_at == TIP_OFF
    assert result.provider_evidence.home_team.provider_id == "ud-lal"
    assert result.provider_evidence.away_team.canonical_id == SAS
    assert result.provider_evidence.status_label == "scheduled"


@pytest.mark.parametrize("offset_hours", [-6, -5.5, 0, 5.5, 6])
def test_a_start_time_inside_the_window_including_its_boundary_matches(offset_hours):
    resolver = _resolver()

    result = resolver.resolve(
        "underdog",
        _evidence(starts_at=TIP_OFF + timedelta(hours=offset_hours)),
        SEASON,
    )

    assert result.state is EventResolutionState.AUTO
    assert result.canonical_event.start_offset_seconds == int(
        -offset_hours * 3600
    )


@pytest.mark.parametrize("offset_seconds", [-6 * 3600 - 1, 6 * 3600 + 1])
def test_a_start_time_past_the_boundary_matches_nothing(offset_seconds):
    resolver = _resolver()

    result = resolver.resolve(
        "underdog",
        _evidence(starts_at=TIP_OFF + timedelta(seconds=offset_seconds)),
        SEASON,
    )

    assert result.state is EventResolutionState.UNMATCHED
    assert result.reason == "no_nearby_event"
    assert result.candidates == ()


def test_the_match_window_is_configurable():
    far = _evidence(starts_at=TIP_OFF + timedelta(hours=8))
    settings = RuntimeSettings(
        environment="testing",
        catalog=CatalogSettings(event_match_window_hours=9),
    )

    assert _resolver().resolve("underdog", far, SEASON).state is (
        EventResolutionState.UNMATCHED
    )
    assert _resolver(settings=settings).resolve("underdog", far, SEASON).state is (
        EventResolutionState.AUTO
    )
    assert _resolver(match_window=timedelta(hours=9)).resolve(
        "underdog", far, SEASON
    ).state is EventResolutionState.AUTO
    with pytest.raises(ValueError, match="match window"):
        _resolver(match_window=0)


def test_two_equally_near_games_stay_ambiguous():
    resolver = _resolver([_event_row(), _event_row("0022500002", offset_hours=3)])

    result = resolver.resolve("underdog", _evidence(), SEASON)

    assert result.state is EventResolutionState.AMBIGUOUS
    assert result.reason == "ambiguous_nearby_events"
    assert [candidate.nba_game_id for candidate in result.candidates] == [
        "0022500001",
        "0022500002",
    ]


def test_reversed_home_and_away_evidence_is_not_the_same_game():
    resolver = _resolver()

    result = resolver.resolve(
        "underdog",
        _evidence(
            home=TeamEvidence(abbreviation="SAS"), away=TeamEvidence(abbreviation="LAL")
        ),
        SEASON,
    )

    assert result.state is EventResolutionState.UNMATCHED
    assert result.reason == "home_away_mismatch"
    assert [candidate.nba_game_id for candidate in result.candidates] == [
        "0022500001"
    ]


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (
            EventEvidence(
                provider_id="ud-1",
                starts_at=TIP_OFF,
                away_team=TeamEvidence(abbreviation="SAS"),
            ),
            "missing_team_evidence",
        ),
        (
            EventEvidence(
                provider_id="ud-1",
                starts_at=TIP_OFF,
                home_team=TeamEvidence(abbreviation="LAL"),
            ),
            "missing_team_evidence",
        ),
        (_evidence(home=TeamEvidence(abbreviation="XXX")), "missing_team_evidence"),
        (_evidence(starts_at=None), "missing_start_time"),
        (
            _evidence(
                home=TeamEvidence(abbreviation="LAL"),
                away=TeamEvidence(abbreviation="LAL"),
            ),
            "single_team_matchup",
        ),
    ],
)
def test_incomplete_or_impossible_evidence_never_matches(evidence, reason):
    result = _resolver().resolve("underdog", evidence, SEASON)

    assert result.state is EventResolutionState.UNMATCHED
    assert result.reason == reason


def test_a_matchup_label_is_never_parsed_into_teams():
    """A label is retained evidence; it may not stand in for team identity."""

    resolver = _resolver()

    result = resolver.resolve(
        "underdog",
        EventEvidence(provider_id="pp-1", label="LAL @ SAS", starts_at=TIP_OFF),
        SEASON,
    )

    assert result.state is EventResolutionState.UNMATCHED
    assert result.reason == "missing_team_evidence"
    assert result.provider_evidence.label == "LAL @ SAS"


def test_a_team_label_two_canonical_teams_answer_to_is_not_evidence():
    rows = [
        _event_row(),
        _event_row(
            "0022500009",
            offset_hours=48,
            home_team_id=BOS,
            home_team_name="Boston Celtics",
            home_team_tricode="LAL",
        ),
    ]

    result = _resolver(rows).resolve("underdog", _evidence(), SEASON)

    assert result.state is EventResolutionState.UNMATCHED
    assert result.reason == "missing_team_evidence"


@pytest.mark.parametrize(
    ("fresh", "refreshed", "reason"),
    [(False, True, "event_catalog_stale"), (True, False, "event_catalog_missing")],
)
def test_missing_or_over_age_catalog_data_has_no_comparison_identity(
    fresh, refreshed, reason
):
    resolver = _resolver(fresh=fresh, refreshed=refreshed)

    result = resolver.resolve("underdog", _evidence(), SEASON)

    assert result.state is EventResolutionState.EVENT_CATALOG_UNAVAILABLE
    assert result.reason == reason
    assert result.canonical_event is None


def test_evidence_without_a_provider_event_id_matches_but_is_never_durable():
    resolver = _resolver()

    result = resolver.resolve("underdog", _evidence(provider_id=None), SEASON)

    assert result.state is EventResolutionState.AUTO
    assert result.canonical_event_id == "0022500001"
    assert result.is_durable is False
    assert result.provider_event_id is None


def test_resolution_requires_typed_values():
    resolver = _resolver()
    with pytest.raises(TypeError, match="EventEvidence"):
        resolver.resolve("underdog", object(), SEASON)
    with pytest.raises(TypeError, match="PlayerProjectionMarket"):
        resolver.resolve_market(object(), SEASON)
    with pytest.raises(ValueError, match="no event evidence"):
        resolver.resolve_market(
            PlayerProjectionMarket(
                provider="underdog",
                market_id="m-eventless",
                statistic=StatisticEvidence(provider_id="points"),
                threshold=MarketThreshold(value="20.5", unit="points"),
                status=MarketStatus.AVAILABLE,
            ),
            SEASON,
        )
    with pytest.raises(TypeError, match="get_events"):
        EventResolver(object())
    with pytest.raises(TypeError, match="get_freshness"):
        EventResolver(type("Partial", (), {"get_events": lambda self, season: []})())


def test_a_catalog_read_failure_is_one_documented_failure_type():
    class FailingCatalog:
        def get_events(self, season):
            raise SQLAlchemyError("boom")

        def get_freshness(self, season):
            raise SQLAlchemyError("boom")

    with pytest.raises(EventMappingPersistenceError):
        EventResolver(FailingCatalog()).resolve("underdog", _evidence(), SEASON)


# -- persistence -------------------------------------------------------------


@pytest.fixture
def event_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'events.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [_event_row(), _event_row("0022500002", offset_hours=48)],
        )
        connection.execute(
            insert(EventCatalogRefresh.__table__).values(
                season=SEASON,
                last_attempt_at=_NOW,
                last_success_at=_NOW,
                event_count=2,
            )
        )
    return engine, _NOW


def test_migration_008_creates_event_mapping_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping.sqlite3'}")

    run_migrations(engine)

    tables = set(inspect(engine).get_table_names())
    assert {
        "provider_event_mappings",
        "event_mapping_decisions",
        "event_mapping_decision_candidates",
        "event_mapping_rejections",
        "event_mapping_locks",
    } <= tables
    assert set(EVENT_MAPPING_STATES) < set(EVENT_MAPPING_DECISION_STATES)


@pytest.mark.parametrize(
    "table",
    [
        ProviderEventMapping.__table__,
        EventMappingDecision.__table__,
        EventMappingDecisionCandidate.__table__,
        EventMappingRejection.__table__,
        EventMappingLock.__table__,
    ],
)
@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_the_event_mapping_ddl_is_valid_on_both_supported_dialects(table, dialect):
    """Boolean comparisons must be ``true``/``false`` rather than ``1``/``0``.

    PostgreSQL cannot compare a boolean column with an integer literal, so a
    check written that way would create a schema only SQLite accepts.
    """

    statement = str(CreateTable(table).compile(dialect=dialect))

    assert "is_active = 1" not in statement
    assert "is_active = 0" not in statement
    assert "IN (0, 1)" not in statement
    if table is ProviderEventMapping.__table__:
        assert "true" in statement
        assert "false" in statement


def test_the_closed_event_state_sets_match_the_resolution_vocabulary():
    resolution_states = {state.value for state in EventResolutionState}
    assert EVENT_MAPPING_DECISION_STATES <= resolution_states
    assert set(EVENT_MAPPING_STATES) < EVENT_MAPPING_DECISION_STATES
    # Neither non-durable outcome may ever be written as a state.
    assert not {"event_catalog_unavailable"} & EVENT_MAPPING_DECISION_STATES
    statement = str(
        CreateTable(EventMappingDecision.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "ck_event_mapping_decision_state" in statement
    for state in EVENT_MAPPING_DECISION_STATES:
        assert f"'{state}'" in statement


@pytest.mark.parametrize(
    ("state", "is_active"),
    [("auto", False), ("ambiguous", True), ("mapping_conflict", True)],
)
def test_the_schema_rejects_an_incoherent_active_state(event_db, state, is_active):
    engine, now = event_db

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(ProviderEventMapping.__table__).values(
                    provider="underdog",
                    provider_event_id="ud-1",
                    mapping_state=state,
                    is_active=is_active,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )


def test_the_schema_keeps_a_conflicting_game_to_the_conflict_state(event_db):
    engine, now = event_db

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(ProviderEventMapping.__table__).values(
                    provider="underdog",
                    provider_event_id="ud-1",
                    mapping_state="auto",
                    is_active=True,
                    conflict_canonical_event_id="0022500002",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )


def _repository(engine, now) -> EventMappingRepository:
    return EventMappingRepository(engine, clock=lambda: now)


def _catalog_resolver(engine, repository, *, clock=None, **kwargs) -> EventResolver:
    """A resolver reading the real catalog tables through the service seam."""

    from app.services.event_catalog_service import EventCatalogService

    class _RefusingProvider:
        def fetch_whole_season_schedule(self, *, season):  # pragma: no cover - guard
            raise AssertionError("resolution never contacts a provider")

    service = EventCatalogService(
        engine,
        _RefusingProvider(),
        clock=(clock or (lambda: _NOW)),
        **kwargs,
    )
    return EventResolver(service, mapping_repository=repository)


def test_an_automatic_observation_is_durable_and_idempotent(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    resolution = resolver.resolve("underdog", _evidence(), SEASON)

    first = repository.record_resolution(resolution)
    second = repository.record_resolution(
        resolver.resolve("underdog", _evidence(), SEASON)
    )

    assert first.state == "auto"
    assert first.persisted is True
    assert second.persisted is False
    mapping = repository.get_active_mapping("underdog", "ud-1")
    assert mapping.canonical_event_id == "0022500001"
    assert mapping.canonical_home_team_tricode == "LAL"
    assert mapping.canonical_scheduled_at == TIP_OFF.isoformat()
    assert mapping.provider_event_label == "Lakers vs Spurs"
    assert mapping.provider_starts_at == TIP_OFF.isoformat()
    assert mapping.provider_home_team_abbreviation == "LAL"
    assert mapping.provider_away_team_abbreviation == "SAS"
    assert len(repository.history(provider="underdog", provider_event_id="ud-1")) == 1


def test_an_unresolved_observation_is_queued_with_its_candidates(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    with engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__).values(
                _event_row("0022500003", offset_hours=2)
            )
        )
    resolver = _catalog_resolver(engine, repository)

    result = repository.record_resolution(
        resolver.resolve("underdog", _evidence(), SEASON)
    )

    assert result.state == "ambiguous"
    assert repository.get_mapping("underdog", "ud-1") is None
    (queued,) = repository.list_unresolved()
    assert queued.decision_state == "ambiguous"
    assert [candidate.canonical_event_id for candidate in queued.candidates] == [
        "0022500001",
        "0022500003",
    ]
    assert queued.candidates[1].start_offset_seconds == 2 * 3600
    assert queued.candidates[0].is_scheduled is True


def test_a_no_longer_placeable_identity_is_withdrawn_from_comparisons(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.record_resolution(resolver.resolve("underdog", _evidence(), SEASON))

    # A start time 24 hours out is near neither scheduled game.
    withdrawn = repository.record_resolution(
        resolver.resolve(
            "underdog", _evidence(starts_at=TIP_OFF + timedelta(hours=24)), SEASON
        )
    )

    assert withdrawn.state == "unmatched"
    mapping = repository.get_mapping("underdog", "ud-1")
    assert mapping.mapping_state == "unmatched"
    assert mapping.is_active is False
    # The claim is suspended, not retracted, so the game it named is retained.
    assert mapping.canonical_event_id == "0022500001"
    assert repository.get_active_mapping("underdog", "ud-1") is None
    (queued,) = repository.list_unresolved()
    assert [candidate.canonical_event_id for candidate in queued.candidates] == [
        "0022500001"
    ]

    remapped = repository.record_resolution(
        resolver.resolve("underdog", _evidence(), SEASON)
    )
    assert remapped.state == "auto"
    assert repository.get_active_mapping("underdog", "ud-1") is not None
    assert repository.list_unresolved() == []


def test_a_replaced_game_id_never_inherits_the_mapping(event_db):
    """A postponed game's replacement is an operator decision, not a transfer."""

    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.record_resolution(resolver.resolve("underdog", _evidence(), SEASON))

    # The schedule replaces the game ID and moves the tip-off by an hour.
    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.delete().where(
                EventCatalogEntry.nba_game_id == "0022500001"
            )
        )
        connection.execute(
            insert(EventCatalogEntry.__table__).values(
                _event_row("0022500011", offset_hours=1)
            )
        )
    pending = repository.record_resolution(
        resolver.resolve(
            "underdog", _evidence(starts_at=TIP_OFF + timedelta(hours=1)), SEASON
        )
    )

    assert pending.state == "replacement_pending"
    mapping = repository.get_mapping("underdog", "ud-1")
    assert mapping.is_active is False
    assert mapping.canonical_event_id == "0022500001"
    (queued,) = repository.list_unresolved()
    assert queued.reason == "replacement_event_identity"
    assert [
        (candidate.canonical_event_id, candidate.is_scheduled)
        for candidate in queued.candidates
    ] == [("0022500001", False), ("0022500011", True)]

    approved = repository.approve(
        "underdog",
        "ud-1",
        "0022500011",
        season=SEASON,
        operator_id="ops",
        reason="reviewed the rescheduled game",
    )
    assert approved.state == "manual_approved"
    assert repository.get_active_mapping("underdog", "ud-1").canonical_event_id == (
        "0022500011"
    )
    assert repository.list_unresolved() == []


def test_ambiguous_replacement_evidence_stays_unresolved(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.record_resolution(resolver.resolve("underdog", _evidence(), SEASON))

    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.delete().where(
                EventCatalogEntry.nba_game_id == "0022500001"
            )
        )
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [
                _event_row("0022500011", offset_hours=1),
                _event_row("0022500012", offset_hours=2),
            ],
        )

    pending = repository.record_resolution(
        resolver.resolve("underdog", _evidence(), SEASON)
    )

    assert pending.state == "replacement_pending"
    (queued,) = repository.list_unresolved()
    assert queued.reason == "ambiguous_replacement_event"
    assert len(queued.candidates) == 3
    assert repository.get_active_mapping("underdog", "ud-1") is None


def test_later_conflicting_evidence_stops_the_mapping(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.record_resolution(resolver.resolve("underdog", _evidence(), SEASON))

    # The same fixture ID now reports a game two days later, which the schedule
    # lists as a different NBA game.
    conflicting = resolver.resolve(
        "underdog",
        _evidence(starts_at=TIP_OFF + timedelta(hours=48)),
        SEASON,
    )
    result = repository.record_resolution(conflicting)

    assert conflicting.state is EventResolutionState.MAPPING_CONFLICT
    assert result.state == "mapping_conflict"
    mapping = repository.get_mapping("underdog", "ud-1")
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.is_active is False
    assert mapping.canonical_event_id == "0022500001"
    assert mapping.conflict_canonical_event_id == "0022500002"
    assert repository.list_mappings() == []
    (conflict,) = repository.list_conflicts()
    assert conflict.mapping.provider_event_id == "ud-1"
    assert conflict.latest_decision.decision_state == "mapping_conflict"
    assert [
        candidate.canonical_event_id for candidate in conflict.latest_decision.candidates
    ] == ["0022500002"]

    # Reading the same conflicting evidence again changes nothing further.
    repeated = repository.record_resolution(
        resolver.resolve(
            "underdog", _evidence(starts_at=TIP_OFF + timedelta(hours=48)), SEASON
        )
    )
    assert repeated.state == "mapping_conflict"
    assert repeated.persisted is False
    assert (
        repository.get_mapping("underdog", "ud-1").conflict_canonical_event_id
        == "0022500002"
    )


def test_a_manual_decision_wins_over_a_later_automatic_read(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)

    approved = repository.approve(
        "underdog",
        "ud-1",
        "0022500002",
        season=SEASON,
        operator_id="ops",
        reason="the provider labels this fixture wrongly",
        provider_evidence=_evidence(),
    )
    observed = resolver.resolve("underdog", _evidence(), SEASON)
    result = repository.record_resolution(observed)

    assert approved.state == "manual_approved"
    assert observed.state is EventResolutionState.MANUAL_APPROVED
    assert result.state == "manual_approved"
    assert result.persisted is False
    mapping = repository.get_active_mapping("underdog", "ud-1")
    assert mapping.canonical_event_id == "0022500002"
    assert mapping.provider_event_label == "Lakers vs Spurs"
    states = [
        decision.decision_state
        for decision in repository.history(provider="underdog", provider_event_id="ud-1")
    ]
    assert states == ["manual_approved"]


def test_a_governed_identity_reporting_another_fixture_fails_closed(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.approve(
        "underdog",
        "ud-1",
        "0022500001",
        season=SEASON,
        operator_id="ops",
        reason="reviewed",
        provider_evidence=_evidence(),
    )

    reused = resolver.resolve(
        "underdog",
        _evidence(
            home=TeamEvidence(abbreviation="BOS"), away=TeamEvidence(abbreviation="SAS")
        ),
        SEASON,
    )
    result = repository.record_resolution(reused)

    assert reused.state is EventResolutionState.MAPPING_CONFLICT
    assert reused.reason == "manual_mapping_conflict"
    assert result.state == "mapping_conflict"
    mapping = repository.get_mapping("underdog", "ud-1")
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.canonical_event_id == "0022500001"

    restored = repository.override(
        "underdog",
        "ud-1",
        "0022500002",
        season=SEASON,
        operator_id="ops",
        reason="the fixture was reassigned",
    )
    assert restored.state == "manual_override"
    assert repository.get_active_mapping("underdog", "ud-1").canonical_event_id == (
        "0022500002"
    )
    assert repository.list_conflicts() == []


def test_a_rescheduled_start_time_inside_the_window_is_not_a_conflict(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.approve(
        "underdog",
        "ud-1",
        "0022500001",
        season=SEASON,
        operator_id="ops",
        reason="reviewed",
        provider_evidence=_evidence(),
    )

    result = resolver.resolve(
        "underdog", _evidence(starts_at=TIP_OFF + timedelta(hours=1)), SEASON
    )

    assert result.state is EventResolutionState.MANUAL_APPROVED


def test_a_rejection_suppresses_the_identity_until_cleared(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.record_resolution(resolver.resolve("underdog", _evidence(), SEASON))

    repository.reject(
        "underdog", "ud-1", operator_id="ops", reason="not an NBA fixture", season=SEASON
    )
    rejected = resolver.resolve("underdog", _evidence(), SEASON)
    suppressed = repository.record_resolution(rejected)

    assert rejected.state is EventResolutionState.REJECTED
    assert suppressed.state == "rejected"
    assert repository.get_active_mapping("underdog", "ud-1") is None
    assert repository.is_rejected("underdog", "ud-1")
    assert [rejection.provider_event_id for rejection in repository.list_rejections()] == [
        "ud-1"
    ]
    with pytest.raises(ValueError, match="clear the active rejection"):
        repository.approve(
            "underdog",
            "ud-1",
            "0022500001",
            season=SEASON,
            operator_id="ops",
            reason="too soon",
        )

    assert repository.clear_rejection(
        "underdog", "ud-1", operator_id="ops", reason="reviewed again"
    )
    assert repository.clear_rejection(
        "underdog", "ud-1", operator_id="ops", reason="already cleared"
    ) is False
    remapped = repository.record_resolution(
        resolver.resolve("underdog", _evidence(), SEASON)
    )
    assert remapped.state == "auto"
    states = [
        decision.decision_state
        for decision in repository.history(provider="underdog", provider_event_id="ud-1")
    ]
    assert states == ["auto", "rejected", "rejection_cleared", "auto"]


def test_the_audit_history_records_operator_identity_and_reason(event_db):
    engine, now = event_db
    repository = _repository(engine, now)

    repository.approve(
        "underdog",
        "ud-1",
        "0022500001",
        season=SEASON,
        operator_id="ops",
        reason="reviewed the matchup",
        provider_evidence=_evidence(),
    )

    (decision,) = repository.history(limit=1)
    assert decision.operator_id == "ops"
    assert decision.reason == "reviewed the matchup"
    assert decision.requested_season == SEASON
    assert decision.canonical_event_id == "0022500001"
    assert decision.provider_event_label == "Lakers vs Spurs"
    assert decision.created_at == now.isoformat()
    with pytest.raises(ValueError, match="history limit"):
        repository.history(limit=0)


def test_operator_actions_validate_their_inputs(event_db):
    engine, now = event_db
    repository = _repository(engine, now)

    with pytest.raises(ValueError, match="operator identity"):
        repository.reject("underdog", "ud-1", operator_id=" ", reason="x")
    with pytest.raises(ValueError, match="operator reason"):
        repository.reject("underdog", "ud-1", operator_id="ops", reason=" ")
    with pytest.raises(ValueError, match="canonical NBA game ID"):
        repository.approve(
            "underdog", "ud-1", "", season=SEASON, operator_id="ops", reason="x"
        )
    with pytest.raises(ValueError, match="not in the requested season"):
        repository.approve(
            "underdog",
            "ud-1",
            "0099999999",
            season=SEASON,
            operator_id="ops",
            reason="x",
        )
    with pytest.raises(ValueError, match="season is required"):
        repository.approve(
            "underdog", "ud-1", "0022500001", operator_id="ops", reason="x"
        )
    with pytest.raises(ValueError, match="provider event ID"):
        repository.get_mapping("underdog", " ")


def test_a_read_taken_before_the_governing_decision_cannot_unseat_it(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.approve(
        "underdog",
        "ud-1",
        "0022500001",
        season=SEASON,
        operator_id="ops",
        reason="reviewed",
        provider_evidence=_evidence(),
    )
    stale = replace(
        resolver.resolve(
            "underdog",
            _evidence(home=TeamEvidence(abbreviation="BOS")),
            SEASON,
        ),
        observed_at=_OBSERVED_BEFORE,
    )

    result = repository.record_resolution(stale)

    assert stale.state is EventResolutionState.MAPPING_CONFLICT
    assert result.state == "manual_approved"
    assert repository.get_active_mapping("underdog", "ud-1") is not None


def test_a_read_taken_after_the_governing_decision_still_fails_closed(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    repository.approve(
        "underdog",
        "ud-1",
        "0022500001",
        season=SEASON,
        operator_id="ops",
        reason="reviewed",
        provider_evidence=_evidence(),
    )
    fresh = replace(
        resolver.resolve(
            "underdog",
            _evidence(home=TeamEvidence(abbreviation="BOS")),
            SEASON,
        ),
        observed_at=_OBSERVED_AFTER,
    )

    result = repository.record_resolution(fresh)

    assert result.state == "mapping_conflict"
    assert repository.get_active_mapping("underdog", "ud-1") is None


def test_an_observation_clock_makes_a_repeated_read_idempotent(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    observed = replace(
        resolver.resolve("underdog", _evidence(), SEASON), observed_at=_OBSERVED_AFTER
    )

    repository.record_resolution(observed)
    repository.record_resolution(observed)

    with engine.connect() as connection:
        assert connection.execute(
            select(EventMappingLock.last_observed_at)
        ).scalar_one() == _OBSERVED_AFTER.replace(tzinfo=None)
        assert connection.execute(
            select(EventMappingDecision.id)
        ).scalars().all() == [1]


def test_the_demo_database_can_never_store_event_mappings():
    from app.errors import InvalidConfigurationError

    with pytest.raises(InvalidConfigurationError):
        EventMappingRepository(create_engine("sqlite:///nba_play_types.db"))


def test_a_write_failure_is_one_documented_failure_type(event_db, monkeypatch):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    resolution = resolver.resolve("underdog", _evidence(), SEASON)
    monkeypatch.setattr(
        EventMappingRepository,
        "persist_auto_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(SQLAlchemyError("boom")),
    )

    with pytest.raises(EventMappingPersistenceError):
        repository.record_resolution(resolution)


def test_a_resolution_the_repository_cannot_key_is_never_stored(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)

    idless = repository.record_resolution(
        resolver.resolve("underdog", _evidence(provider_id=None), SEASON)
    )
    unavailable = repository.record_resolution(
        replace(
            resolver.resolve("underdog", _evidence(), SEASON),
            state=EventResolutionState.EVENT_CATALOG_UNAVAILABLE,
            canonical_event=None,
        )
    )

    assert idless.state == "auto"
    assert idless.persisted is False
    assert unavailable.persisted is False
    assert repository.list_mappings() == []
    assert repository.history() == []
    with pytest.raises(TypeError, match="EventResolution"):
        repository.record_resolution(object())


# -- board integration -------------------------------------------------------


def _market(
    market_id: str = "m-1",
    *,
    event: EventEvidence | None = None,
    provider: str = "underdog",
) -> PlayerProjectionMarket:
    return PlayerProjectionMarket(
        provider=provider,
        market_id=market_id,
        event=_evidence() if event is None else event,
        statistic=StatisticEvidence(provider_id="points"),
        threshold=MarketThreshold(value="20.5", unit="points"),
        status=MarketStatus.AVAILABLE,
    )


def _snapshot(
    *markets: PlayerProjectionMarket,
    retrieved_at: datetime = _OBSERVED_AFTER,
) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider="underdog",
        status=SnapshotStatus.COMPLETE,
        markets=markets,
        coverage=CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=retrieved_at,
    )


class _StaticProvider:
    def __init__(self, snapshot: ProviderSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self, query, context):
        return self.snapshot


def _board_service(snapshot, *, resolver, repository) -> DFSBoardService:
    return DFSBoardService(
        provider_registry={"underdog": _StaticProvider(snapshot)},
        event_resolver=resolver,
        event_mapping_repository=repository,
    )


def test_one_board_read_records_one_observation_per_fixture(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    service = _board_service(
        _snapshot(_market("m-1"), _market("m-2")),
        resolver=resolver,
        repository=repository,
    )
    query = NBAMarketQuery(season=SEASON)

    first = service.get_board(query)
    second = service.get_board(query)

    assert len(first.resolved_markets) == 2
    (outcome,) = first.event_mapping_outcomes
    assert outcome.state is EventResolutionState.AUTO
    assert outcome.canonical_event_id == "0022500001"
    assert second.usable
    assert len(repository.history(provider="underdog", provider_event_id="ud-1")) == 1


def test_a_fixture_its_own_read_contradicts_fails_closed(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    disagreeing = (
        _market("m-1"),
        _market(
            "m-2",
            event=_evidence(home=TeamEvidence(abbreviation="BOS")),
        ),
    )
    query = NBAMarketQuery(season=SEASON)

    forward = _board_service(
        _snapshot(*disagreeing), resolver=resolver, repository=repository
    ).get_board(query)

    (outcome,) = forward.event_mapping_outcomes
    assert outcome.state is EventResolutionState.MAPPING_CONFLICT
    assert outcome.canonical_event_id is None
    assert outcome.resolution.reason == "contradictory_provider_evidence"
    assert len(outcome.resolution.contradictory_evidence) == 2
    # Both markets stay on the board; only the comparison identity is withheld.
    assert len(forward.resolved_markets) == 2

    reversed_read = _board_service(
        _snapshot(*reversed(disagreeing)), resolver=resolver, repository=repository
    ).get_board(query)
    (repeat,) = reversed_read.event_mapping_outcomes
    assert repeat.state is EventResolutionState.MAPPING_CONFLICT
    assert len(repository.history(provider="underdog", provider_event_id="ud-1")) == 1


def test_compatible_markets_are_one_observation_carrying_every_fact(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    sparse = _market(
        "m-1",
        event=EventEvidence(
            provider_id="ud-1",
            starts_at=TIP_OFF,
            home_team=TeamEvidence(canonical_id=LAL),
            away_team=TeamEvidence(canonical_id=SAS),
        ),
    )
    labelled = _market("m-2", event=_evidence())

    board = _board_service(
        _snapshot(sparse, labelled), resolver=resolver, repository=repository
    ).get_board(NBAMarketQuery(season=SEASON))

    (outcome,) = board.event_mapping_outcomes
    assert outcome.state is EventResolutionState.AUTO
    mapping = repository.get_active_mapping("underdog", "ud-1")
    assert mapping.provider_event_label == "Lakers vs Spurs"
    assert mapping.provider_home_team_canonical_id == LAL
    assert mapping.provider_home_team_abbreviation == "LAL"


def test_a_board_read_keeps_markets_when_the_catalog_is_unavailable(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    # The catalog's last successful refresh is older than the allowed age.
    resolver = _catalog_resolver(
        engine,
        repository,
        clock=lambda: _NOW + timedelta(hours=4),
        max_age_hours=1,
    )
    service = _board_service(
        _snapshot(_market("m-1")), resolver=resolver, repository=repository
    )

    board = service.get_board(NBAMarketQuery(season=SEASON))

    (outcome,) = board.event_mapping_outcomes
    assert outcome.state is EventResolutionState.EVENT_CATALOG_UNAVAILABLE
    assert outcome.canonical_event_id is None
    assert len(board.resolved_markets) == 1
    assert board.resolved_markets[0].statistic_match is not None
    assert repository.list_mappings() == []
    assert repository.history() == []


def test_an_id_less_market_matches_the_current_board_without_a_durable_row(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)
    service = _board_service(
        _snapshot(_market("m-1", event=_evidence(provider_id=None))),
        resolver=resolver,
        repository=repository,
    )

    board = service.get_board(NBAMarketQuery(season=SEASON))

    (outcome,) = board.event_mapping_outcomes
    assert outcome.state is EventResolutionState.AUTO
    assert outcome.persisted is False
    assert outcome.canonical_event_id == "0022500001"
    assert repository.list_mappings() == []
    assert repository.history() == []
    with engine.connect() as connection:
        assert connection.execute(select(EventMappingLock.provider)).all() == []


def test_a_mapping_failure_never_removes_a_market(event_db):
    engine, now = event_db
    repository = _repository(engine, now)
    resolver = _catalog_resolver(engine, repository)

    class _FailingRepository:
        def get_mapping(self, provider, provider_event_id):
            return None

        def get_rejection(self, provider, provider_event_id):
            return None

        def record_resolution(self, resolution, **kwargs):
            raise EventMappingPersistenceError("boom")

    service = _board_service(
        _snapshot(_market("m-1")), resolver=resolver, repository=_FailingRepository()
    )

    board = service.get_board(NBAMarketQuery(season=SEASON))

    assert len(board.resolved_markets) == 1
    assert board.event_mapping_outcomes == ()


def test_a_board_without_event_collaborators_reports_no_event_outcomes(event_db):
    service = DFSBoardService(
        provider_registry={"underdog": _StaticProvider(_snapshot(_market("m-1")))}
    )

    board = service.get_board(NBAMarketQuery(season=SEASON))

    assert board.event_mapping_outcomes == ()
    assert len(board.resolved_markets) == 1
