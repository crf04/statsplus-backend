"""Contract tests for season-scoped provider athlete mappings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, insert
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.athlete_mapping import (
    MAPPING_DECISION_STATES,
    AthleteMappingDecision,
    AthleteMappingRejection,
    ProviderAthleteMapping,
)
from app.providers.dfs import AthleteEvidence, TeamEvidence
from app.providers.dfs import (
    CoverageEvidence,
    MarketThreshold,
    MarketStatus,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
    StatisticEvidence,
)
from app.services.athlete_mapping_errors import AthleteMappingPersistenceError
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.athlete_resolver import (
    AthleteResolver,
    MappingResolutionState,
    normalize_athlete_name,
)
from app.services.dfs_board import DFSBoardService


def _catalog_row(
    player_id: int,
    name: str,
    *,
    active: bool = True,
    team_id: int | None = 1610612747,
    team_name: str | None = "Los Angeles Lakers",
    team_abbreviation: str | None = "LAL",
) -> dict[str, object]:
    return {
        "season": "2024-25",
        "player_id": player_id,
        "display_name": name,
        "roster_status": "active" if active else "inactive",
        "is_active": active,
        "is_active_for_season": active,
        "team_id": team_id,
        "team_name": team_name,
        "team_abbreviation": team_abbreviation,
        "published_at": datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
    }


def test_name_normalization_ignores_unicode_marks_punctuation_and_underscores():
    assert normalize_athlete_name("D'Angelo") == normalize_athlete_name("DAngelo")
    assert normalize_athlete_name("Nikola_Jokic") == normalize_athlete_name("Nikola Jokic")
    assert normalize_athlete_name("José Alvarado") == normalize_athlete_name("Jose Alvarado")


@pytest.mark.parametrize(
    ("value", "ascii_value"),
    [
        ("Kristaps Porziņģis", "Kristaps Porzingis"),
        ("Bøj Ødegaard", "Boj Odegaard"),
        ("Łukasz Słoma", "Lukasz Sloma"),
        ("Œdipe Æther", "Oedipe Aether"),
        ("Đorđe Þorsson", "Dorde Thorsson"),
        ("Weiß", "Weiss"),
    ],
)
def test_normalization_folds_non_decomposing_latin_letters(value, ascii_value):
    assert normalize_athlete_name(value) == normalize_athlete_name(ascii_value)


class FakeCatalog:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get_catalog(self, season: str, *, active_only: bool = False):
        assert season == "2024-25"
        rows = [row for row in self.rows if row["season"] == season]
        if active_only:
            rows = [row for row in rows if row["is_active_for_season"]]
        return rows


def test_exact_normalized_name_matches_accent_and_retains_typed_evidence():
    resolver = AthleteResolver(
        FakeCatalog([_catalog_row(15, "Nikola Jokić")]),
    )

    result = resolver.resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(
                provider_id="pp-lal",
                canonical_id=1610612747,
                name="Los Angeles Lakers",
                abbreviation="lal",
            ),
        ),
        "2024-25",
    )

    assert result.state is MappingResolutionState.AUTO
    assert result.canonical_athlete is not None
    assert result.canonical_athlete.player_id == 15
    assert result.provider_evidence.team is not None
    assert result.provider_evidence.team.provider_id == "pp-lal"
    assert result.provider_evidence.team.abbreviation == "LAL"


@pytest.mark.parametrize(
    ("rows", "evidence", "state"),
    [
        (
            [_catalog_row(15, "LeBron James"), _catalog_row(23, "LeBron James")],
            AthleteEvidence(provider_id="pp-1", name="LeBron James"),
            MappingResolutionState.AMBIGUOUS,
        ),
        (
            [_catalog_row(15, "LeBron James", active=False)],
            AthleteEvidence(provider_id="pp-1", name="LeBron James"),
            MappingResolutionState.INACTIVE_ONLY,
        ),
        (
            [_catalog_row(15, "LeBron James")],
            AthleteEvidence(provider_id="pp-1", name="King James"),
            MappingResolutionState.UNMATCHED,
        ),
        (
            [_catalog_row(15, "LeBron James", team_id=1610612747)],
            AthleteEvidence(
                provider_id="pp-1",
                name="LeBron James",
                team=TeamEvidence(canonical_id=1610612764),
            ),
            MappingResolutionState.TEAM_CONFLICT,
        ),
    ],
)
def test_non_qualifying_evidence_is_not_auto_mapped(rows, evidence, state):
    result = AthleteResolver(FakeCatalog(rows)).resolve(
        "prizepicks", evidence, "2024-25"
    )

    assert result.state is state
    assert not result.is_auto_qualifying


def test_resolver_translates_catalog_read_failure():
    class BrokenCatalog:
        def get_catalog(self, season, active_only=False):
            raise SQLAlchemyError("catalog is unavailable")

    resolver = AthleteResolver(BrokenCatalog())

    with pytest.raises(AthleteMappingPersistenceError):
        resolver.resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-1", name="LeBron James"),
            "2024-25",
        )


def test_migration_006_creates_mapping_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping.sqlite3'}")

    result = run_migrations(engine)

    assert "006_create_athlete_mappings" in result.applied
    assert {
        "provider_athlete_mappings",
        "athlete_mapping_decisions",
        "athlete_mapping_rejections",
    }.issubset(inspect(engine).get_table_names())


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
@pytest.mark.parametrize(
    "table",
    [
        ProviderAthleteMapping.__table__,
        AthleteMappingRejection.__table__,
        AthleteMappingDecision.__table__,
    ],
)
def test_boolean_checks_compile_with_true_false_on_both_dialects(table, dialect):
    statement = str(CreateTable(table).compile(dialect=dialect))

    assert "is_active = 1" not in statement
    assert "is_active = 0" not in statement
    assert "IN (0, 1)" not in statement
    if "is_active" in table.c:
        assert "true" in statement
        assert "false" in statement


def test_decision_state_check_covers_the_closed_resolution_state_set():
    assert MAPPING_DECISION_STATES == frozenset(
        state.value for state in MappingResolutionState
    )
    statement = str(
        CreateTable(AthleteMappingDecision.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "ck_athlete_mapping_decision_state" in statement
    for state in MAPPING_DECISION_STATES:
        assert f"'{state}'" in statement


@pytest.fixture
def mapping_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mapping.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    run_migrations(engine)
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [
                _catalog_row(15, "Nikola Jokić"),
                _catalog_row(23, "LeBron James"),
            ],
        )
    return engine, now


def test_active_state_check_rejects_an_incoherent_row(mapping_db):
    engine, now = mapping_db

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(ProviderAthleteMapping.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                mapping_state="mapping_conflict",
                is_active=True,
                first_seen_at=now,
                last_seen_at=now,
            )
        )


def test_decision_state_check_rejects_an_unknown_state(mapping_db):
    engine, now = mapping_db

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(AthleteMappingDecision.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                decision_state="speculative_state",
                created_at=now,
            )
        )


def _resolver(*, rows=None, repository=None) -> AthleteResolver:
    rows = rows or [_catalog_row(15, "Nikola Jokić"), _catalog_row(23, "LeBron James")]
    return AthleteResolver(FakeCatalog(rows), mapping_repository=repository)


def _auto_resolution(*, provider_id: str = "pp-15", name: str = "Nikola Jokic"):
    return _resolver().resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id=provider_id,
            name=name,
            team=TeamEvidence(
                provider_id="pp-lal",
                canonical_id=1610612747,
                name="Los Angeles Lakers",
                abbreviation="LAL",
            ),
        ),
        "2024-25",
    )


def test_auto_decision_is_durable_and_idempotent(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolution = _auto_resolution()

    first = repository.persist_auto_decision(resolution)
    second = repository.persist_auto_decision(resolution)

    assert first.state == "auto"
    assert first.persisted is True
    assert second.persisted is False
    mapping = repository.get_active_mapping("PRIZEPICKS", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    assert mapping.provider_team_id == "pp-lal"
    assert len(repository.history(provider="prizepicks", provider_athlete_id="pp-15")) == 1


def test_concurrent_first_auto_decision_has_one_mapping_and_audit_row(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolution = _auto_resolution()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(repository.persist_auto_decision, [resolution, resolution])
        )

    assert sum(result.persisted for result in results) == 1
    assert len(repository.list_mappings()) == 1
    assert len(repository.history()) == 1


def test_manual_decisions_require_audited_identity_and_reason_and_protect_mapping(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)
    resolution = _auto_resolution()
    repository.persist_auto_decision(resolution)

    with pytest.raises(ValueError, match="operator identity"):
        repository.override(
            "prizepicks", "pp-15", 23, season="2024-25", operator_id="", reason="fix"
        )
    with pytest.raises(ValueError, match="operator reason"):
        repository.override(
            "prizepicks", "pp-15", 23, season="2024-25", operator_id="ops", reason=""
        )

    override = repository.override(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="reviewed source identity",
    )
    assert override.mapping is not None
    assert override.mapping.mapping_state == "manual_override"
    assert override.mapping.canonical_player_id == 23

    # A new board read cannot replace a manual decision.
    automatic = repository.persist_auto_decision(resolution)
    assert automatic.persisted is False
    assert repository.get_mapping("prizepicks", "pp-15").canonical_player_id == 23
    audit = repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    assert audit[-1].operator_id == "ops@example.com"
    assert audit[-1].reason == "reviewed source identity"


def test_manual_decision_retains_supplied_provider_evidence(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)

    result = repository.approve(
        "prizepicks",
        "pp-15",
        15,
        season="2024-25",
        operator_id="ops@example.com",
        reason="verified source identity",
        provider_evidence=AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(
                provider_id="pp-den",
                canonical_id=1610612743,
                name="Denver Nuggets",
                abbreviation="den",
            ),
        ),
    )

    assert result.mapping is not None
    assert result.mapping.provider_name == "Nikola Jokic"
    assert result.mapping.provider_team_id == "pp-den"
    assert result.mapping.provider_team_abbreviation == "DEN"
    assert result.decision is not None
    assert result.decision.provider_name == "Nikola Jokic"
    assert result.decision.provider_team_name == "Denver Nuggets"


def test_rejection_suppresses_until_clear_and_clear_is_audited(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)
    resolution = _auto_resolution(provider_id="pp-23", name="LeBron James")

    repository.reject(
        "prizepicks",
        "pp-23",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    blocked = repository.persist_auto_decision(resolution)
    assert blocked.state == "rejected"
    assert blocked.persisted is False
    assert repository.is_rejected("prizepicks", "pp-23")

    assert repository.clear_rejection(
        "prizepicks",
        "pp-23",
        operator_id="ops@example.com",
        reason="new evidence reviewed",
    )
    mapped = repository.persist_auto_decision(resolution)
    assert mapped.state == "auto"
    assert repository.is_rejected("prizepicks", "pp-23") is False
    assert any(item.decision_state == "rejection_cleared" for item in repository.history())


def test_later_candidate_conflict_deactivates_mapping_and_retains_new_evidence(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)
    first = _auto_resolution()
    repository.persist_auto_decision(first)
    second = _resolver(
        rows=[_catalog_row(15, "Other Name"), _catalog_row(23, "Nikola Jokic")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
    )

    assert second.state is MappingResolutionState.MAPPING_CONFLICT
    conflict = repository.record_resolution(second)
    assert conflict.state == "mapping_conflict"
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.is_active is False
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.provider_name == "Nikola Jokic"


@pytest.mark.parametrize(
    ("rows", "name", "state"),
    [
        (
            [_catalog_row(15, "LeBron James"), _catalog_row(23, "LeBron James")],
            "LeBron James",
            "ambiguous",
        ),
        ([_catalog_row(15, "LeBron James", active=False)], "LeBron James", "inactive_only"),
        ([_catalog_row(15, "LeBron James")], "King James", "unmatched"),
    ],
)
def test_unresolved_observations_are_durable_typed_and_idempotent(
    mapping_db, rows, name, state
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolution = _resolver(rows=rows).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-77",
            name=name,
            team=TeamEvidence(provider_id="pp-lal", name="Los Angeles Lakers"),
        ),
        "2024-25",
    )

    first = repository.record_resolution(resolution)
    second = repository.record_resolution(resolution)

    assert first.state == state
    assert first.persisted is True
    assert second.persisted is False
    observations = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in observations] == [state]
    assert observations[0].provider_athlete_id == "pp-77"
    assert observations[0].provider_name == name
    assert observations[0].provider_team_name == "Los Angeles Lakers"
    assert observations[0].requested_season == "2024-25"
    # An unresolved observation never creates current mapping state.
    assert repository.get_mapping("prizepicks", "pp-77") is None


def test_repository_read_failures_are_translated_at_the_boundary():
    class BrokenEngine:
        url = "sqlite:///unreachable.sqlite3"

        def connect(self):
            raise SQLAlchemyError("connection pool exhausted")

    repository = AthleteMappingRepository(BrokenEngine())

    with pytest.raises(AthleteMappingPersistenceError):
        repository.get_mapping("prizepicks", "pp-15")
    with pytest.raises(AthleteMappingPersistenceError):
        repository.get_rejection("prizepicks", "pp-15")
    with pytest.raises(AthleteMappingPersistenceError):
        repository.list_mappings()
    with pytest.raises(AthleteMappingPersistenceError):
        repository.list_rejections()
    with pytest.raises(AthleteMappingPersistenceError):
        repository.history()
    with pytest.raises(AthleteMappingPersistenceError):
        repository.list_unresolved()


# -- DFS board acceptance ------------------------------------------------


def _market(
    provider_athlete_id: str = "pp-15",
    name: str = "Nikola Jokic",
    *,
    team: TeamEvidence | None = None,
) -> PlayerProjectionMarket:
    return PlayerProjectionMarket(
        provider="prizepicks",
        market_id=f"m-{provider_athlete_id}",
        athlete=AthleteEvidence(provider_id=provider_athlete_id, name=name, team=team),
        statistic=StatisticEvidence(provider_id="pts"),
        threshold=MarketThreshold(value="20.5", unit="points"),
        status=MarketStatus.AVAILABLE,
    )


def _snapshot(*markets: PlayerProjectionMarket) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.COMPLETE,
        markets=markets,
        coverage=CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
    )


def _unresolved_markets(board) -> tuple[PlayerProjectionMarket, ...]:
    """Board markets stripped of the statistic match the board attaches.

    The board resolves every market against the statistic catalog, so a test
    asserting that a market survived a mapping failure compares the market
    identity rather than the pre-resolution instance.
    """

    return tuple(
        replace(market, statistic_match=None)
        for snapshot in board.snapshots
        for market in snapshot.markets
    )


class _StaticProvider:
    def __init__(self, snapshot: ProviderSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self, query, context):
        return self.snapshot


def _board_service(snapshot, *, resolver, repository) -> DFSBoardService:
    return DFSBoardService(
        provider_registry={"prizepicks": _StaticProvider(snapshot)},
        athlete_resolver=resolver,
        athlete_mapping_repository=repository,
    )


def test_board_persists_the_first_qualifying_mapping_and_is_idempotent(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    service = _board_service(
        _snapshot(_market()), resolver=resolver, repository=repository
    )
    query = NBAMarketQuery(season="2024-25")

    first = service.get_board(query)
    second = service.get_board(query)

    assert first.snapshots[0].markets[0].market_id == "m-pp-15"
    assert second.usable
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "auto"
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Nikola Jokic"
    assert len(repository.history(provider="prizepicks", provider_athlete_id="pp-15")) == 1


def test_board_never_replaces_a_manual_decision(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.approve(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="reviewed source identity",
    )
    service = _board_service(
        _snapshot(_market()),
        resolver=_resolver(repository=repository),
        repository=repository,
    )

    board = service.get_board(NBAMarketQuery(season="2024-25"))

    assert board.usable
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping.mapping_state == "manual_approved"
    assert mapping.canonical_player_id == 23


def test_board_respects_an_active_rejection(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    service = _board_service(
        _snapshot(_market()),
        resolver=_resolver(repository=repository),
        repository=repository,
    )

    board = service.get_board(NBAMarketQuery(season="2024-25"))

    assert board.usable
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    assert repository.is_rejected("prizepicks", "pp-15")


def test_board_deactivates_a_mapping_when_later_evidence_conflicts(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    query = NBAMarketQuery(season="2024-25")
    _board_service(
        _snapshot(_market()),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(query)

    conflicting = _board_service(
        _snapshot(_market(name="LeBron James")),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(query)

    assert conflicting.usable
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.is_active is False
    assert mapping.provider_name == "LeBron James"


def test_board_returns_market_when_mapping_persistence_fails():
    market = _market()

    class Resolver:
        def resolve_market(self, market, season):
            return type("Resolution", (), {"is_auto_qualifying": True})()

    class BrokenRepository:
        def record_resolution(self, resolution):
            raise AthleteMappingPersistenceError("database unavailable")

    board = _board_service(
        _snapshot(market), resolver=Resolver(), repository=BrokenRepository()
    ).get_board(NBAMarketQuery(season="2024-25"))

    assert _unresolved_markets(board) == (market,)
    assert board.snapshots[0].markets[0].statistic_match is not None


def test_board_returns_markets_when_a_mapping_read_fails():
    market = _market()

    class BrokenReadResolver:
        def resolve_market(self, market, season):
            raise AthleteMappingPersistenceError("mapping state is unreadable")

    class Repository:
        def record_resolution(self, resolution):  # pragma: no cover - never reached
            raise AssertionError("a failed read must not reach persistence")

    board = _board_service(
        _snapshot(market), resolver=BrokenReadResolver(), repository=Repository()
    ).get_board(NBAMarketQuery(season="2024-25"))

    assert _unresolved_markets(board) == (market,)
    assert board.mapping_outcomes == ()
