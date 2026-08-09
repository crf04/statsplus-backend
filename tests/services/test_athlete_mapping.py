"""Contract tests for season-scoped provider athlete mappings."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import and_, create_engine, event, inspect, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.athlete_mapping import (
    MAPPING_DECISION_STATES,
    MAPPING_STATES,
    AthleteMappingDecision,
    AthleteMappingLock,
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
from app.services.athlete_mapping_repository import (
    AthleteMappingRepository,
    BoardMappingOutcome,
    MappingPersistenceResult,
    ProviderAthleteMappingRecord,
)
from app.services.athlete_resolver import (
    AthleteResolver,
    MappingResolutionState,
    normalize_athlete_name,
)
from app.services.dfs_board import DFSBoardService


#: Fixed clearing timestamp for direct-insert constraint cases.
_CLEARED_AT = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)

#: Provider observation instants for out-of-order board reads.  The operator
#: fixture clock sits between them, so a read retrieved at ``_OBSERVED_BEFORE``
#: predates every operator decision and one retrieved at ``_OBSERVED_AFTER``
#: postdates them.
_OBSERVED_BEFORE = datetime(2026, 8, 9, 11, tzinfo=timezone.utc)
_OBSERVED_AFTER = datetime(2026, 8, 9, 13, tzinfo=timezone.utc)

#: A read taken between the two.  It is only fenced if a repeated observation
#: advanced the identity's durable high-water mark past it.
_OBSERVED_BETWEEN = datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc)


def _catalog_row(
    player_id: int,
    name: str,
    *,
    active: bool = True,
    team_id: int | None = 1610612747,
    team_name: str | None = "Los Angeles Lakers",
    team_abbreviation: str | None = "LAL",
    season: str = "2024-25",
) -> dict[str, object]:
    return {
        "season": season,
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
        self.requested_seasons: list[str] = []

    def get_catalog(self, season: str, *, active_only: bool = False):
        self.requested_seasons.append(season)
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
        "athlete_mapping_decision_candidates",
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


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_mapping_state_check_covers_the_closed_current_state_set(dialect):
    """Every state a mapping row may currently hold is enumerated in the check.

    ``unmatched`` is one of them: an established claim whose canonical athlete
    left the requested season's catalog is withdrawn onto the row itself, so it
    has to be a legal current state and an inactive one.
    """

    statement = str(
        CreateTable(ProviderAthleteMapping.__table__).compile(dialect=dialect)
    )

    assert "ck_provider_mapping_state" in statement
    assert "unmatched" in MAPPING_STATES
    for state in MAPPING_STATES:
        assert f"'{state}'" in statement


def test_migrated_mapping_state_check_accepts_a_withdrawn_unmatched_row(tmp_path):
    """The migrated schema, not just the model, allows the withdrawn state."""

    engine = create_engine(f"sqlite:///{tmp_path / 'migrated.sqlite3'}")
    run_migrations(engine)
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            insert(ProviderAthleteMapping.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-15",
                mapping_state="unmatched",
                is_active=False,
                canonical_player_id=15,
                first_seen_at=now,
                last_seen_at=now,
            )
        )

    mapping = AthleteMappingRepository(engine).get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "unmatched"
    assert mapping.canonical_player_id == 15


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


@pytest.mark.parametrize(
    ("mapping_state", "is_active"),
    [
        ("auto", False),
        ("manual_approved", False),
        ("inactive_only", True),
        ("ambiguous", True),
        ("unmatched", True),
    ],
)
def test_active_state_check_rejects_an_incoherently_flagged_row(
    mapping_db, mapping_state, is_active
):
    """Only a decided, comparable state may be active.

    A catalog-inactive mapping keeps its canonical claim but is withdrawn from
    board comparisons, so an active row in that state would be a contradiction.
    """

    engine, now = mapping_db

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(ProviderAthleteMapping.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-98",
                mapping_state=mapping_state,
                is_active=is_active,
                first_seen_at=now,
                last_seen_at=now,
            )
        )


@pytest.mark.parametrize(
    ("mapping_state", "is_active"),
    [
        ("auto", True),
        ("manual_approved", True),
        ("rejected", False),
        ("inactive_only", False),
        ("unmatched", False),
    ],
)
@pytest.mark.parametrize(
    "conflict",
    [
        {"conflict_canonical_player_id": 23},
        {"conflict_canonical_name": "LeBron James"},
    ],
)
def test_conflict_columns_check_rejects_a_non_conflict_row(
    mapping_db, mapping_state, is_active, conflict
):
    """Only a current conflict may name a conflicting canonical athlete."""

    engine, now = mapping_db

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(ProviderAthleteMapping.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                mapping_state=mapping_state,
                is_active=is_active,
                first_seen_at=now,
                last_seen_at=now,
                **conflict,
            )
        )


def test_conflict_columns_check_still_allows_a_current_conflict(mapping_db):
    engine, now = mapping_db

    with engine.begin() as connection:
        connection.execute(
            insert(ProviderAthleteMapping.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                mapping_state="mapping_conflict",
                is_active=False,
                conflict_canonical_player_id=23,
                conflict_canonical_name="LeBron James",
                first_seen_at=now,
                last_seen_at=now,
            )
        )

    mapping = AthleteMappingRepository(engine).get_mapping("prizepicks", "pp-99")
    assert mapping is not None
    assert mapping.conflict_canonical_player_id == 23


@pytest.mark.parametrize(
    "clearing",
    [
        # An active rejection carries no clearing evidence at all.
        {"is_active": True, "cleared_at": _CLEARED_AT},
        {"is_active": True, "cleared_by": "ops@example.com"},
        {"is_active": True, "clear_reason": "identity was reinstated"},
        # A cleared rejection carries every part of its clearing evidence.
        {"is_active": False},
        {"is_active": False, "cleared_at": _CLEARED_AT},
        {"is_active": False, "cleared_at": _CLEARED_AT, "cleared_by": "ops@example.com"},
        {"is_active": False, "cleared_by": "ops@example.com", "clear_reason": "reinstated"},
    ],
)
def test_rejection_clear_check_rejects_partial_clearing_evidence(mapping_db, clearing):
    engine, now = mapping_db

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(AthleteMappingRejection.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                reason="duplicate provider identity",
                operator_id="ops@example.com",
                created_at=now,
                **clearing,
            )
        )


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_conflict_columns_check_is_emitted_for_every_dialect(dialect):
    statement = str(
        CreateTable(ProviderAthleteMapping.__table__).compile(dialect=dialect)
    )

    assert "ck_provider_mapping_conflict_fields" in statement
    for column in ("conflict_canonical_player_id", "conflict_canonical_name"):
        assert f"{column} IS NULL" in statement


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_rejection_clear_check_covers_every_clearing_column(dialect):
    statement = str(
        CreateTable(AthleteMappingRejection.__table__).compile(dialect=dialect)
    )

    assert "ck_mapping_rejection_active" in statement
    for column in ("cleared_at", "cleared_by", "clear_reason"):
        assert f"{column} IS NULL" in statement
        assert f"{column} IS NOT NULL" in statement


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


def _auto_resolution(
    *,
    provider_id: str = "pp-15",
    name: str = "Nikola Jokic",
    observed_at: datetime | None = None,
):
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
        observed_at=observed_at,
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


def _approved_evidence() -> AthleteEvidence:
    """Provider evidence an operator reviewed before approving pp-15."""

    return AthleteEvidence(
        provider_id="pp-15",
        name="Nikola Jokic",
        team=TeamEvidence(
            provider_id="pp-den",
            canonical_id=1610612743,
            name="Denver Nuggets",
            abbreviation="DEN",
        ),
    )


def _reused_identity_evidence() -> AthleteEvidence:
    """The same provider ID later reporting an entirely different athlete."""

    return AthleteEvidence(
        provider_id="pp-15",
        name="LeBron James",
        team=TeamEvidence(
            provider_id="pp-lal",
            canonical_id=1610612747,
            name="Los Angeles Lakers",
            abbreviation="LAL",
        ),
    )


def _approve_pp_15(repository: AthleteMappingRepository) -> None:
    repository.approve(
        "prizepicks",
        "pp-15",
        15,
        season="2024-25",
        operator_id="ops@example.com",
        reason="verified source identity",
        provider_evidence=_approved_evidence(),
    )


def test_a_manual_mapping_keeps_precedence_but_fails_closed_on_changed_identity(
    mapping_db,
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)

    agreeing = resolver.resolve("prizepicks", _approved_evidence(), "2024-25")
    reused = resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")

    # Agreeing evidence still takes the operator's identity, unchallenged by
    # anything the automatic resolution would have chosen.
    assert agreeing.state is MappingResolutionState.MANUAL_APPROVED
    assert agreeing.canonical_player_id == 15
    # Clearly conflicting evidence is never silently mapped to the approved
    # athlete; it is a conflict for an operator to review.
    assert reused.state is MappingResolutionState.MAPPING_CONFLICT
    assert reused.canonical_athlete is None
    assert reused.reason == "manual_mapping_conflict"


def test_a_conflicting_manual_identity_is_deactivated_and_retained_for_review(
    mapping_db,
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)

    conflict = repository.record_resolution(
        resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    )
    repeated = repository.record_resolution(
        resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    )

    assert conflict.state == "mapping_conflict"
    assert conflict.persisted is True
    assert repeated.persisted is False
    # A conflicting identity is not usable for comparisons.
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.is_active is False
    # The approved canonical identity is retained beside the new evidence so an
    # operator can see both sides of the conflict.
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "LeBron James"
    audit = repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    assert [item.decision_state for item in audit] == [
        "manual_approved",
        "mapping_conflict",
    ]
    assert audit[0].operator_id == "ops@example.com"
    assert audit[0].provider_name == "Nikola Jokic"
    assert audit[-1].reason == "manual_mapping_conflict"


def test_a_reapproval_restores_the_manual_mapping_after_a_conflict(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)
    repository.record_resolution(
        resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    )

    result = repository.approve(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider reused the identity",
        provider_evidence=_reused_identity_evidence(),
    )

    assert result.mapping.mapping_state == "manual_approved"
    assert result.mapping.is_active is True
    assert result.mapping.canonical_player_id == 23
    assert result.mapping.conflict_canonical_player_id is None
    assert (
        resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25").state
        is MappingResolutionState.MANUAL_APPROVED
    )


def _reapprove_pp_15_as_the_reused_identity(
    repository: AthleteMappingRepository,
) -> None:
    repository.approve(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider reused the identity",
        provider_evidence=_reused_identity_evidence(),
    )


def test_a_stale_conflict_cannot_undo_a_newer_manual_decision(mapping_db):
    """The operator resolved this very conflict, so replaying it changes nothing."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)
    stale = resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    assert stale.state is MappingResolutionState.MAPPING_CONFLICT
    repository.record_resolution(stale)
    _reapprove_pp_15_as_the_reused_identity(repository)

    result = repository.record_resolution(stale)

    assert result.state == "manual_approved"
    assert result.persisted is False
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "manual_approved"
    assert mapping.canonical_player_id == 23
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["manual_approved", "mapping_conflict", "manual_approved"]


def test_a_racing_reapproval_beats_a_stale_conflict_promotion(mapping_db):
    """The reapproval commits inside the window the resolver read across.

    Only a comparison made inside the identity transaction can see that the
    governed decision now covers exactly the evidence the conflict reports.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)
    stale = resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    assert stale.state is MappingResolutionState.MAPPING_CONFLICT

    serialized = AthleteMappingRepository._transaction.__get__(repository)
    raced = threading.Event()

    @contextmanager
    def _transaction_after_a_racing_reapproval(provider, provider_id):
        if not raced.is_set():
            raced.set()
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(
                    _reapprove_pp_15_as_the_reused_identity, repository
                ).result(timeout=10)
        with serialized(provider, provider_id) as connection:
            yield connection

    repository._transaction = _transaction_after_a_racing_reapproval

    result = repository.record_resolution(stale)

    assert raced.is_set()
    assert result.state == "manual_approved"
    assert result.persisted is False
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 23
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["manual_approved", "manual_approved"]


def test_later_evidence_still_promotes_a_contradicted_manual_mapping(mapping_db):
    """A reapproval is not a blanket licence for every later observation."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)
    _reapprove_pp_15_as_the_reused_identity(repository)

    later = resolver.resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Stephen Curry",
            team=TeamEvidence(
                provider_id="pp-gsw",
                canonical_id=1610612744,
                abbreviation="GSW",
            ),
        ),
        "2024-25",
    )
    result = repository.record_resolution(later)

    assert later.reason == "manual_mapping_conflict"
    assert result.state == "mapping_conflict"
    assert result.persisted is True
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping.canonical_player_id == 23
    assert mapping.provider_name == "Stephen Curry"


@pytest.mark.parametrize(
    ("team", "conflicts"),
    [
        # The provider now reports its own identity for another team while
        # every other fact it reports is unchanged.
        (
            TeamEvidence(
                provider_id="pp-lal",
                canonical_id=1610612743,
                name="Denver Nuggets",
                abbreviation="DEN",
            ),
            True,
        ),
        (
            TeamEvidence(
                provider_id="pp-den",
                canonical_id=1610612743,
                name="Denver Nuggets",
                abbreviation="DEN",
            ),
            False,
        ),
        # No comparable provider team ID, so the canonical team ID decides.
        (TeamEvidence(canonical_id=1610612743, abbreviation="LAL"), False),
        (TeamEvidence(canonical_id=1610612747, abbreviation="DEN"), True),
        # No comparable team ID at all, so the abbreviation decides.
        (TeamEvidence(abbreviation="DEN", name="Los Angeles Lakers"), False),
        (TeamEvidence(abbreviation="LAL"), True),
        # Only a team name is comparable.
        (TeamEvidence(name="Denver Nuggets"), False),
        (TeamEvidence(name="Los Angeles Lakers"), True),
    ],
)
def test_manual_fail_closed_compares_the_provider_team_identity(
    mapping_db, team, conflicts
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)

    result = resolver.resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic", team=team),
        "2024-25",
    )

    if conflicts:
        assert result.state is MappingResolutionState.MAPPING_CONFLICT
        assert result.reason == "manual_mapping_conflict"
    else:
        assert result.state is MappingResolutionState.MANUAL_APPROVED
        assert result.canonical_player_id == 15


@pytest.mark.parametrize("action", ["approve", "override"])
def test_a_manual_decision_recovers_observation_evidence_across_a_rejection(
    mapping_db, action
):
    """Rejecting and clearing an identity does not discard what was observed."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    repository.record_resolution(
        resolver.resolve(
            "prizepicks",
            AthleteEvidence(
                provider_id="pp-77",
                name="King James",
                team=TeamEvidence(
                    provider_id="pp-lal",
                    canonical_id=1610612747,
                    name="Los Angeles Lakers",
                    abbreviation="LAL",
                ),
            ),
            "2024-25",
        )
    )
    repository.reject(
        "prizepicks",
        "pp-77",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    assert repository.clear_rejection(
        "prizepicks",
        "pp-77",
        operator_id="ops@example.com",
        reason="the identity was reinstated",
    )

    result = getattr(repository, action)(
        "prizepicks",
        "pp-77",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider uses a nickname",
    )

    assert result.mapping.provider_name == "King James"
    assert result.mapping.provider_team_id == "pp-lal"
    assert result.mapping.provider_team_canonical_id == 1610612747
    assert result.mapping.provider_team_abbreviation == "LAL"
    assert result.decision.provider_name == "King James"
    assert result.decision.provider_team_name == "Los Angeles Lakers"


@pytest.mark.parametrize("action", ["approve", "override"])
def test_a_manual_decision_falls_back_to_the_latest_observation_evidence(
    mapping_db, action
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    observation = resolver.resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-77",
            name="King James",
            team=TeamEvidence(
                provider_id="pp-lal",
                canonical_id=1610612747,
                name="Los Angeles Lakers",
                abbreviation="LAL",
            ),
        ),
        "2024-25",
    )
    assert observation.state is MappingResolutionState.UNMATCHED
    repository.record_resolution(observation)

    result = getattr(repository, action)(
        "prizepicks",
        "pp-77",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider uses a nickname",
    )

    assert result.mapping.provider_name == "King James"
    assert result.mapping.provider_team_id == "pp-lal"
    assert result.mapping.provider_team_canonical_id == 1610612747
    assert result.mapping.provider_team_name == "Los Angeles Lakers"
    assert result.mapping.provider_team_abbreviation == "LAL"
    assert result.decision.provider_name == "King James"
    assert result.decision.provider_team_name == "Los Angeles Lakers"
    assert result.decision.provider_team_abbreviation == "LAL"


@pytest.mark.parametrize("action", ["approve", "override"])
def test_a_manual_decision_prefers_an_observation_newer_than_a_stale_mapping(
    mapping_db, action
):
    """A rejected mapping's frozen evidence is not what the provider reports now.

    The board reused the provider ID for a different athlete after the
    rejection was cleared, so the observation an operator is acting on is newer
    than anything the inactive mapping row still carries.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(
        rows=[
            _catalog_row(
                15,
                "Nikola Jokić",
                team_id=1610612743,
                team_name="Denver Nuggets",
                team_abbreviation="DEN",
            ),
            _catalog_row(23, "LeBron James"),
        ],
        repository=repository,
    )
    nuggets = TeamEvidence(
        provider_id="pp-den",
        canonical_id=1610612743,
        name="Denver Nuggets",
        abbreviation="DEN",
    )
    lakers = TeamEvidence(
        provider_id="pp-lal",
        canonical_id=1610612747,
        name="Los Angeles Lakers",
        abbreviation="LAL",
    )
    mapped = repository.record_resolution(
        resolver.resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-15", name="Nikola Jokic", team=nuggets),
            "2024-25",
        )
    )
    assert mapped.state == "auto"
    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    assert repository.clear_rejection(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="the identity was reinstated",
    )
    reused = AthleteEvidence(provider_id="pp-15", name="King James", team=lakers)
    observed = resolver.resolve("prizepicks", reused, "2024-25")
    assert observed.state is MappingResolutionState.UNMATCHED
    repository.record_resolution(observed)

    result = getattr(repository, action)(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider uses a nickname",
    )

    assert result.mapping.canonical_player_id == 23
    assert result.mapping.provider_name == "King James"
    assert result.mapping.provider_team_id == "pp-lal"
    assert result.mapping.provider_team_canonical_id == 1610612747
    assert result.mapping.provider_team_abbreviation == "LAL"
    assert result.decision.provider_name == "King James"
    assert result.decision.provider_team_name == "Los Angeles Lakers"

    # The operator reviewed exactly this evidence, so reading the same board
    # row again may not immediately contradict the decision.
    replay = repository.record_resolution(
        resolver.resolve("prizepicks", reused, "2024-25")
    )
    assert replay.state == result.state
    retained = repository.get_active_mapping("prizepicks", "pp-15")
    assert retained is not None
    assert retained.canonical_player_id == 23
    assert retained.provider_name == "King James"
    assert all(
        item.decision_state != "mapping_conflict" for item in repository.history()
    )


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


def _reject_and_clear(repository: AthleteMappingRepository, provider_id: str) -> None:
    repository.reject(
        "prizepicks",
        provider_id,
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    assert repository.clear_rejection(
        "prizepicks",
        provider_id,
        operator_id="ops@example.com",
        reason="the identity was reinstated",
    )


def test_cleared_rejection_lets_a_reused_identity_map_without_a_conflict(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)
    assert repository.record_resolution(_auto_resolution()).state == "auto"
    _reject_and_clear(repository, "pp-15")

    # The provider now reports a different athlete under the same ID.  The
    # rejected row still carries the canonical ID it was mapped to, but nothing
    # active claims the identity any more, so the fresh evidence maps instead
    # of queueing a conflict against a decision an operator already undid.
    reused = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="LeBron James",
            team=TeamEvidence(
                provider_id="pp-lal",
                canonical_id=1610612747,
                name="Los Angeles Lakers",
                abbreviation="LAL",
            ),
        ),
        "2024-25",
    )
    assert reused.state is MappingResolutionState.AUTO

    mapped = repository.record_resolution(reused)
    assert mapped.state == "auto"
    assert mapped.persisted is True
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 23
    assert active.provider_name == "LeBron James"
    assert repository.list_conflicts() == []
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "rejected",
        "rejection_cleared",
        "auto",
    ]


def test_replaying_the_same_auto_after_a_cleared_rejection_is_a_new_transition(
    mapping_db,
):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)
    resolution = _auto_resolution()
    repository.persist_auto_decision(resolution)
    _reject_and_clear(repository, "pp-15")

    # Reinstating the identity is a state transition even though the board read
    # is unchanged, so the audit log records it rather than deduplicating it
    # against the observation that preceded the rejection.
    restored = repository.persist_auto_decision(resolution)
    assert restored.state == "auto"
    assert restored.persisted is True
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 15
    assert active.mapping_state == "auto"

    # Reading the same board row again transitions nothing.
    assert repository.persist_auto_decision(resolution).persisted is False
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "rejected",
        "rejection_cleared",
        "auto",
    ]


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


def test_repository_write_failures_are_translated_at_the_boundary():
    class BrokenEngine:
        url = "sqlite:///unreachable.sqlite3"

        def begin(self):
            raise SQLAlchemyError("connection pool exhausted")

    repository = AthleteMappingRepository(BrokenEngine())

    with pytest.raises(AthleteMappingPersistenceError):
        repository.approve(
            "prizepicks", "pp-15", 15, season="2024-25", operator_id="ops", reason="why"
        )
    with pytest.raises(AthleteMappingPersistenceError):
        repository.override(
            "prizepicks", "pp-15", 15, season="2024-25", operator_id="ops", reason="why"
        )
    with pytest.raises(AthleteMappingPersistenceError):
        repository.reject("prizepicks", "pp-15", operator_id="ops", reason="why")
    with pytest.raises(AthleteMappingPersistenceError):
        repository.clear_rejection(
            "prizepicks", "pp-15", operator_id="ops", reason="why"
        )


def test_duplicate_lock_row_leaves_no_savepoint_and_keeps_the_transaction_usable(
    mapping_db,
):
    """The duplicate insert must be caught after ``begin_nested`` exits.

    PostgreSQL aborts the surrounding transaction if the failed savepoint is
    still open, so every later statement in the same transaction would fail.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    with repository._transaction("prizepicks", "pp-15"):
        pass

    savepoints: list[str] = []
    event.listen(engine, "savepoint", lambda *args: savepoints.append("begin"))
    event.listen(engine, "release_savepoint", lambda *args: savepoints.append("release"))
    event.listen(
        engine, "rollback_savepoint", lambda *args: savepoints.append("rollback")
    )

    # The lock row now exists, so this transaction takes the duplicate path.
    with repository._transaction("prizepicks", "pp-15") as connection:
        # A released savepoint would leave PostgreSQL's transaction aborted, so
        # the failed insert must be rolled back to the savepoint instead.
        assert savepoints == ["begin", "rollback"]
        assert connection.in_nested_transaction() is False
        assert connection.in_transaction() is True
        connection.execute(
            insert(AthleteMappingDecision.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-15",
                decision_state="unmatched",
                created_at=now,
            )
        )

    assert len(repository.history(provider="prizepicks")) == 1


def test_later_resolution_removes_an_identity_from_unresolved(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    ambiguous = _resolver(
        rows=[_catalog_row(15, "LeBron James"), _catalog_row(23, "LeBron James")]
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="LeBron James"),
        "2024-25",
    )
    repository.record_resolution(ambiguous)
    assert [item.provider_athlete_id for item in repository.list_unresolved()] == ["pp-77"]

    repository.record_resolution(
        _resolver(rows=[_catalog_row(23, "LeBron James")]).resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-77", name="LeBron James"),
            "2024-25",
        )
    )

    assert repository.list_unresolved() == []


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_later_manual_decision_removes_an_identity_from_unresolved(mapping_db, action):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(
        _resolver(rows=[_catalog_row(15, "LeBron James", active=False)]).resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-77", name="LeBron James"),
            "2024-25",
        )
    )
    assert repository.list_unresolved()

    if action == "approve":
        repository.approve(
            "prizepicks",
            "pp-77",
            15,
            season="2024-25",
            operator_id="ops@example.com",
            reason="reviewed source identity",
        )
    else:
        repository.reject(
            "prizepicks",
            "pp-77",
            operator_id="ops@example.com",
            reason="provider identity is not trusted",
        )

    assert repository.list_unresolved() == []


def test_a_conflict_is_queued_for_review_without_joining_the_active_mappings(
    mapping_db,
):
    """A conflict is inactive, so only its own queue can surface it."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(
        resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    )

    # The conflict is neither an active mapping nor an unresolved observation,
    # so an operator would otherwise see nothing left to act on.
    assert repository.list_mappings(active_only=True) == []
    assert repository.list_unresolved() == []
    conflicts = repository.list_conflicts()

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.mapping.provider == "prizepicks"
    assert conflict.mapping.provider_athlete_id == "pp-15"
    assert conflict.mapping.season == "2024-25"
    # Both sides of the disagreement, plus the provider evidence that caused it.
    assert conflict.mapping.canonical_player_id == 15
    assert conflict.mapping.conflict_canonical_player_id == 23
    assert conflict.mapping.conflict_canonical_name == "LeBron James"
    assert conflict.mapping.provider_name == "LeBron James"
    assert conflict.latest_decision is not None
    assert conflict.latest_decision.decision_state == "mapping_conflict"
    assert conflict.latest_decision.canonical_player_id == 23
    assert conflict.latest_decision.created_at == conflict.mapping.last_seen_at

    _reapprove_pp_15_as_the_reused_identity(repository)

    # Resolving the conflict is what empties the queue.
    assert repository.list_conflicts() == []
    assert [item.provider_athlete_id for item in repository.list_mappings(active_only=True)] == [
        "pp-15"
    ]


def test_a_conflict_is_named_by_its_queue_and_not_by_the_full_mapping_listing(
    mapping_db,
):
    """The full listing would otherwise repeat the identity without its evidence."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(
        resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    )

    assert repository.list_mappings() == []
    assert [
        item.mapping.provider_athlete_id for item in repository.list_conflicts()
    ] == ["pp-15"]

    _reapprove_pp_15_as_the_reused_identity(repository)

    # Resolving the conflict returns the identity to the mapping listing.
    assert repository.list_conflicts() == []
    assert [item.provider_athlete_id for item in repository.list_mappings()] == ["pp-15"]


def test_the_full_mapping_listing_still_shows_other_inactive_rows(mapping_db):
    """Only conflicts are elaborated elsewhere; a rejection is not."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _approve_pp_15(repository)
    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )

    assert repository.list_mappings(active_only=True) == []
    listed = repository.list_mappings()

    assert [item.provider_athlete_id for item in listed] == ["pp-15"]
    assert listed[0].is_active is False


def test_a_stale_auto_conflict_cannot_undo_a_deliberate_manual_canonical_choice(
    mapping_db,
):
    """The operator accepted this evidence and still chose their own canonical."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    repository.record_resolution(_auto_resolution())

    stale = resolver.resolve("prizepicks", _reused_identity_evidence(), "2024-25")
    assert stale.state is MappingResolutionState.MAPPING_CONFLICT
    # The automatic side of the disagreement names a different canonical athlete
    # from the one the operator is about to keep.
    assert stale.canonical_player_id == 23
    repository.override(
        "prizepicks",
        "pp-15",
        15,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider relabeled the athlete we already approved",
        provider_evidence=_reused_identity_evidence(),
    )

    result = repository.record_resolution(stale)

    assert result.state == "manual_override"
    assert result.persisted is False
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "manual_override"
    assert mapping.canonical_player_id == 15
    assert repository.list_conflicts() == []

    # Evidence the operator never reviewed still promotes a conflict.
    changed = resolver.resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Stephen Curry",
            team=TeamEvidence(
                provider_id="pp-gsw",
                canonical_id=1610612744,
                abbreviation="GSW",
            ),
        ),
        "2024-25",
    )
    promoted = repository.record_resolution(changed)

    assert promoted.state == "mapping_conflict"
    assert promoted.persisted is True
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    assert [
        item.mapping.provider_athlete_id for item in repository.list_conflicts()
    ] == ["pp-15"]


def test_id_only_team_conflict_retains_both_team_sides(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolution = _resolver(
        rows=[_catalog_row(23, "LeBron James", team_id=1610612747)]
    ).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-23",
            name="LeBron James",
            team=TeamEvidence(canonical_id=1610612764),
        ),
        "2024-25",
    )
    assert resolution.state is MappingResolutionState.TEAM_CONFLICT

    repository.record_resolution(resolution)

    observation = repository.list_unresolved(provider="prizepicks")[0]
    assert observation.decision_state == "team_conflict"
    assert observation.provider_team_canonical_id == 1610612764
    assert observation.canonical_team_id == 1610612747
    assert observation.to_dict()["provider_team_canonical_id"] == 1610612764


def test_manual_decisions_retain_canonical_team_evidence(mapping_db):
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
            team=TeamEvidence(provider_id="pp-den", canonical_id=1610612743),
        ),
    )

    assert result.mapping.provider_team_canonical_id == 1610612743
    assert result.decision.provider_team_canonical_id == 1610612743


@pytest.mark.parametrize(
    ("rows", "state", "expected"),
    [
        (
            [_catalog_row(15, "LeBron James"), _catalog_row(23, "LeBron James")],
            "ambiguous",
            [(15, True), (23, True)],
        ),
        (
            [_catalog_row(15, "LeBron James", active=False)],
            "inactive_only",
            [(15, False)],
        ),
    ],
)
def test_unresolved_candidates_are_durable_and_typed(mapping_db, rows, state, expected):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolution = _resolver(rows=rows).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="LeBron James"),
        "2024-25",
    )

    repository.record_resolution(resolution)

    observation = repository.list_unresolved(provider="prizepicks")[0]
    assert observation.decision_state == state
    assert [
        (candidate.canonical_player_id, candidate.is_active_for_season)
        for candidate in observation.candidates
    ] == expected
    assert observation.candidates[0].canonical_name == "LeBron James"
    assert observation.candidates[0].canonical_team_abbreviation == "LAL"
    assert observation.to_dict()["candidates"][0]["canonical_player_id"] == expected[0][0]
    assert repository.history(provider="prizepicks")[-1].candidates == observation.candidates


def test_a_changed_candidate_set_is_recorded_as_a_new_observation(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    evidence = AthleteEvidence(provider_id="pp-77", name="LeBron James")
    first_rows = [_catalog_row(15, "LeBron James"), _catalog_row(23, "LeBron James")]
    second_rows = first_rows + [_catalog_row(31, "LeBron James")]

    repository.record_resolution(
        _resolver(rows=first_rows).resolve("prizepicks", evidence, "2024-25")
    )
    repeated = repository.record_resolution(
        _resolver(rows=first_rows).resolve("prizepicks", evidence, "2024-25")
    )
    changed = repository.record_resolution(
        _resolver(rows=second_rows).resolve("prizepicks", evidence, "2024-25")
    )

    assert repeated.persisted is False
    assert changed.persisted is True
    observation = repository.list_unresolved(provider="prizepicks")[0]
    assert [candidate.canonical_player_id for candidate in observation.candidates] == [
        15,
        23,
        31,
    ]


def test_a_changed_candidate_evidence_set_is_recorded_as_a_new_observation(mapping_db):
    """Candidate identity is more than the player ID an operator has to pick."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    evidence = AthleteEvidence(provider_id="pp-77", name="LeBron James")
    first_rows = [_catalog_row(15, "LeBron James"), _catalog_row(23, "LeBron James")]
    traded_rows = [
        _catalog_row(
            15,
            "LeBron James",
            team_id=1610612743,
            team_name="Denver Nuggets",
            team_abbreviation="DEN",
        ),
        _catalog_row(23, "LeBron James"),
    ]

    repository.record_resolution(
        _resolver(rows=first_rows).resolve("prizepicks", evidence, "2024-25")
    )
    repeated = repository.record_resolution(
        _resolver(rows=first_rows).resolve("prizepicks", evidence, "2024-25")
    )
    changed = repository.record_resolution(
        _resolver(rows=traded_rows).resolve("prizepicks", evidence, "2024-25")
    )

    assert repeated.persisted is False
    assert changed.persisted is True
    observation = repository.list_unresolved(provider="prizepicks")[0]
    assert observation.candidates[0].canonical_team_abbreviation == "DEN"
    assert observation.candidates[0].canonical_team_name == "Denver Nuggets"
    assert observation.candidates[0].canonical_team_id == 1610612743


def test_a_repeated_state_after_a_different_observation_is_recorded_again(mapping_db):
    """Suppression is per transition, not for the lifetime of an identity."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    ambiguous_rows = [_catalog_row(15, "Nikola Jokić"), _catalog_row(23, "Nikola Jokic")]

    repository.record_resolution(_auto_resolution())
    ambiguous = repository.record_resolution(
        _resolver(rows=ambiguous_rows, repository=repository).resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
            "2024-25",
        )
    )
    repeated_auto = repository.record_resolution(_auto_resolution())

    assert ambiguous.state == "ambiguous"
    assert ambiguous.persisted is True
    assert repeated_auto.persisted is True
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto", "ambiguous", "auto"]
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    # The identity is decided again, so the unresolved queue is empty.
    assert repository.list_unresolved(provider="prizepicks") == []


def test_an_unmatched_identity_reappears_after_a_rejection_is_cleared(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    def _unmatched():
        return _resolver(
            rows=[_catalog_row(23, "LeBron James")], repository=repository
        ).resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-77", name="King James"),
            "2024-25",
        )

    assert repository.record_resolution(_unmatched()).persisted is True
    repository.reject(
        "prizepicks",
        "pp-77",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    repository.clear_rejection(
        "prizepicks", "pp-77", operator_id="ops@example.com", reason="new evidence"
    )

    reappeared = repository.record_resolution(_unmatched())

    assert reappeared.persisted is True
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-77")
    ] == ["unmatched", "rejected", "rejection_cleared", "unmatched"]
    assert [
        item.provider_athlete_id
        for item in repository.list_unresolved(provider="prizepicks")
    ] == ["pp-77"]


def test_an_official_catalog_rename_keeps_the_existing_canonical_identity(mapping_db):
    """A renamed catalog row is the same NBA player, not a new identity."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    renamed = _resolver(
        rows=[_catalog_row(15, "Nikola Jokić Sr."), _catalog_row(23, "LeBron James")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(provider_id="pp-lal", canonical_id=1610612747),
        ),
        "2024-25",
    )

    assert renamed.state is MappingResolutionState.AUTO
    assert renamed.canonical_athlete is not None
    assert renamed.canonical_athlete.player_id == 15
    repository.record_resolution(renamed)
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    assert mapping.canonical_name == "Nikola Jokić Sr."


def test_a_provider_label_change_to_the_same_player_is_not_a_conflict(mapping_db):
    """The provider adopting the new official label keeps the same identity."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    relabeled = _resolver(
        rows=[_catalog_row(15, "Nikola Jokić Sr."), _catalog_row(23, "LeBron James")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic Sr.",
            team=TeamEvidence(provider_id="pp-lal", canonical_id=1610612747),
        ),
        "2024-25",
    )

    assert relabeled.state is MappingResolutionState.AUTO
    assert relabeled.canonical_athlete.player_id == 15
    repository.record_resolution(relabeled)
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "auto"
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Nikola Jokic Sr."


def test_repeated_conflict_reads_are_idempotent_and_retain_the_conflict(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    conflicting_evidence = AthleteEvidence(
        provider_id="pp-15",
        name="LeBron James",
        team=TeamEvidence(provider_id="pp-lal", canonical_id=1610612747),
    )
    repository.record_resolution(
        _resolver(repository=repository).resolve(
            "prizepicks", conflicting_evidence, "2024-25"
        )
    )

    def _reread():
        return _resolver(repository=repository).resolve(
            "prizepicks", conflicting_evidence, "2024-25"
        )

    assert _reread().state is MappingResolutionState.MAPPING_CONFLICT
    first_repeat = repository.record_resolution(_reread())
    second_repeat = repository.record_resolution(_reread())

    assert first_repeat.persisted is False
    assert second_repeat.persisted is False
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.is_active is False
    assert mapping.conflict_canonical_player_id == 23
    assert mapping.conflict_canonical_name == "LeBron James"
    assert mapping.provider_name == "LeBron James"
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto", "mapping_conflict"]


def test_a_retained_identity_still_validates_current_team_evidence(mapping_db):
    """A stable canonical ID preserves identity; it never skips team checks."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    conflicting = _resolver(
        rows=[_catalog_row(15, "Nikola Jokić Sr."), _catalog_row(23, "LeBron James")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(
                provider_id="pp-den",
                canonical_id=1610612743,
                name="Denver Nuggets",
                abbreviation="DEN",
            ),
        ),
        "2024-25",
    )

    assert conflicting.state is MappingResolutionState.MAPPING_CONFLICT
    assert repository.record_resolution(conflicting).state == "mapping_conflict"
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.is_active is False
    assert mapping.conflict_canonical_player_id == 15
    assert mapping.provider_team_canonical_id == 1610612743


def test_a_stale_unresolved_observation_cannot_requeue_a_decided_identity(mapping_db):
    """The board resolves outside the identity transaction, so reads go stale."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    stale = _resolver(
        rows=[_catalog_row(23, "LeBron James")], repository=repository
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="King James"),
        "2024-25",
    )
    assert stale.state is MappingResolutionState.UNMATCHED

    repository.approve(
        "prizepicks",
        "pp-77",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="verified source identity",
    )
    result = repository.record_resolution(stale)

    assert result.state == "manual_approved"
    assert result.persisted is False
    assert repository.list_unresolved(provider="prizepicks") == []
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-77")
    ] == ["manual_approved"]
    mapping = repository.get_active_mapping("prizepicks", "pp-77")
    assert mapping is not None
    assert mapping.mapping_state == "manual_approved"


def test_a_racing_stale_observation_never_lands_after_a_manual_decision(mapping_db):
    """Whichever thread wins the identity, governance is the last word."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    stale = _resolver(
        rows=[_catalog_row(23, "LeBron James")], repository=repository
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="King James"),
        "2024-25",
    )
    barrier = threading.Barrier(2)

    def _observe():
        barrier.wait(timeout=5)
        return repository.record_resolution(stale)

    def _approve():
        barrier.wait(timeout=5)
        return repository.approve(
            "prizepicks",
            "pp-77",
            23,
            season="2024-25",
            operator_id="ops@example.com",
            reason="verified source identity",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_observe), executor.submit(_approve)]
        for future in futures:
            future.result(timeout=10)

    states = [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-77")
    ]
    assert states[-1] == "manual_approved"
    assert states.count("manual_approved") == 1
    assert repository.list_unresolved(provider="prizepicks") == []
    mapping = repository.get_active_mapping("prizepicks", "pp-77")
    assert mapping is not None
    assert mapping.mapping_state == "manual_approved"


@pytest.mark.parametrize(
    ("racing", "expected_state", "expected_mapping_state", "expected_active"),
    [
        ("auto", "mapping_conflict", "mapping_conflict", False),
        ("approve", "manual_approved", "manual_approved", True),
        ("reject", "rejected", None, None),
    ],
)
def test_team_conflict_promotion_reads_the_mapping_inside_the_identity_lock(
    mapping_db, racing, expected_state, expected_mapping_state, expected_active
):
    """A mapping committed after the resolver read must still be promoted.

    The racing write lands in the window between the resolver's read and the
    serialized append, so only an inspection inside the identity transaction
    can see it.  An automatic mapping is promoted to a conflict and
    deactivated; a manual decision or an active rejection still wins.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    conflicting = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(provider_id="pp-den", canonical_id=1610612743),
        ),
        "2024-25",
    )
    assert conflicting.state is MappingResolutionState.TEAM_CONFLICT

    def _race():
        if racing == "auto":
            return repository.record_resolution(_auto_resolution())
        if racing == "approve":
            return repository.approve(
                "prizepicks",
                "pp-15",
                15,
                season="2024-25",
                operator_id="ops@example.com",
                reason="verified source identity",
            )
        return repository.reject(
            "prizepicks",
            "pp-15",
            operator_id="ops@example.com",
            reason="provider identity is not trusted",
        )

    serialized = AthleteMappingRepository._transaction.__get__(repository)
    raced = threading.Event()

    @contextmanager
    def _transaction_after_a_racing_commit(provider, provider_id):
        if not raced.is_set():
            raced.set()
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(_race).result(timeout=10)
        with serialized(provider, provider_id) as connection:
            yield connection

    repository._transaction = _transaction_after_a_racing_commit

    result = repository.record_resolution(conflicting)

    assert raced.is_set()
    assert result.state == expected_state
    mapping = repository.get_mapping("prizepicks", "pp-15")
    if expected_mapping_state is None:
        assert mapping is None
    else:
        assert mapping is not None
        assert mapping.mapping_state == expected_mapping_state
        assert mapping.is_active is expected_active
    assert repository.list_unresolved(provider="prizepicks") == []

    if racing == "auto":
        assert result.persisted is True
        assert repository.get_active_mapping("prizepicks", "pp-15") is None
        # The promoted row keeps both sides of the disagreement for review.
        assert mapping.canonical_player_id == 15
        assert mapping.conflict_canonical_player_id == 15
        assert mapping.provider_team_canonical_id == 1610612743
        assert [
            item.decision_state
            for item in repository.history(
                provider="prizepicks", provider_athlete_id="pp-15"
            )
        ] == ["auto", "mapping_conflict"]
    else:
        assert result.persisted is False


def test_a_stale_conflict_cannot_replace_an_active_rejection(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    stale = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="LeBron James",
            team=TeamEvidence(provider_id="pp-lal", canonical_id=1610612747),
        ),
        "2024-25",
    )
    assert stale.state is MappingResolutionState.MAPPING_CONFLICT

    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    blocked = repository.record_resolution(stale)

    assert blocked.state == "rejected"
    assert blocked.persisted is False
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "rejected"
    assert mapping.is_active is False
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto", "rejected"]

    # Transition-aware idempotency still holds once governance is cleared.
    repository.clear_rejection(
        "prizepicks", "pp-15", operator_id="ops@example.com", reason="new evidence"
    )
    replayed = repository.record_resolution(stale)
    repeated = repository.record_resolution(stale)

    assert replayed.persisted is True
    assert repeated.persisted is False
    assert repository.get_mapping("prizepicks", "pp-15").mapping_state == "mapping_conflict"


def test_a_cross_season_team_move_keeps_the_mapping_without_a_conflict(mapping_db):
    """Team evidence belongs to the requested season, not the previous one."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    catalog = FakeCatalog(
        [
            _catalog_row(15, "Nikola Jokić"),
            _catalog_row(
                15,
                "Nikola Jokić",
                season="2025-26",
                team_id=1610612743,
                team_name="Denver Nuggets",
                team_abbreviation="DEN",
            ),
        ]
    )
    resolver = AthleteResolver(catalog, mapping_repository=repository)
    repository.record_resolution(_auto_resolution())

    moved = resolver.resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(
                provider_id="pp-den",
                canonical_id=1610612743,
                name="Denver Nuggets",
                abbreviation="DEN",
            ),
        ),
        "2025-26",
    )

    assert catalog.requested_seasons[-1] == "2025-26"
    assert moved.state is MappingResolutionState.AUTO
    assert repository.record_resolution(moved).state == "auto"
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.season == "2025-26"
    assert mapping.canonical_player_id == 15
    assert mapping.canonical_team_id == 1610612743

    # Evidence that disagrees with the requested season's canonical row is
    # still a conflict rather than a legitimate move.
    inconsistent = resolver.resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-15",
            name="Nikola Jokic",
            team=TeamEvidence(provider_id="pp-lal", canonical_id=1610612747),
        ),
        "2025-26",
    )

    assert inconsistent.state is MappingResolutionState.TEAM_CONFLICT
    assert repository.record_resolution(inconsistent).state == "mapping_conflict"
    assert repository.get_mapping("prizepicks", "pp-15").is_active is False


def test_manual_decisions_may_select_an_inactive_only_catalog_athlete(mapping_db):
    """Automatic mapping needs an active season row; governance may override."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    with engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [_catalog_row(31, "Retired Guard", active=False)],
        )
    inactive_only = _resolver(
        rows=[_catalog_row(31, "Retired Guard", active=False)], repository=repository
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-31", name="Retired Guard"),
        "2024-25",
    )
    assert inactive_only.state is MappingResolutionState.INACTIVE_ONLY
    repository.record_resolution(inactive_only)

    approved = repository.approve(
        "prizepicks",
        "pp-31",
        31,
        season="2024-25",
        operator_id="ops@example.com",
        reason="two-way contract signed after the catalog snapshot",
    )

    assert approved.mapping is not None
    assert approved.mapping.mapping_state == "manual_approved"
    assert approved.mapping.canonical_player_id == 31
    assert approved.mapping.is_active is True
    # The manual selection of an inactive catalog row is explicit in the audit.
    assert [
        (candidate.canonical_player_id, candidate.is_active_for_season)
        for candidate in approved.decision.candidates
    ] == [(31, False)]
    assert approved.decision.operator_id == "ops@example.com"
    # A later automatic read still refuses to map an inactive-only identity.
    later = _resolver(
        rows=[_catalog_row(31, "Retired Guard", active=False)], repository=repository
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-32", name="Retired Guard"),
        "2024-25",
    )
    assert later.state is MappingResolutionState.INACTIVE_ONLY
    assert repository.list_unresolved(provider="prizepicks") == []


def test_manual_decisions_still_reject_an_unknown_canonical_athlete(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)

    with pytest.raises(ValueError, match="requested season"):
        repository.approve(
            "prizepicks",
            "pp-15",
            999,
            season="2024-25",
            operator_id="ops@example.com",
            reason="typo in the canonical ID",
        )


# -- DFS board acceptance ------------------------------------------------


def _market(
    provider_athlete_id: str | None = "pp-15",
    name: str = "Nikola Jokic",
    *,
    team: TeamEvidence | None = None,
    market_id: str | None = None,
) -> PlayerProjectionMarket:
    return PlayerProjectionMarket(
        provider="prizepicks",
        market_id=market_id or f"m-{provider_athlete_id}",
        athlete=AthleteEvidence(provider_id=provider_athlete_id, name=name, team=team),
        statistic=StatisticEvidence(provider_id="pts"),
        threshold=MarketThreshold(value="20.5", unit="points"),
        status=MarketStatus.AVAILABLE,
    )


def _snapshot(
    *markets: PlayerProjectionMarket,
    retrieved_at: datetime = datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
) -> ProviderSnapshot:
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
        retrieved_at=retrieved_at,
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


def test_board_reports_a_manual_mapping_conflict_and_stops_using_it(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _approve_pp_15(repository)
    service = _board_service(
        _snapshot(
            _market(
                "pp-15",
                "LeBron James",
                team=TeamEvidence(
                    provider_id="pp-lal",
                    canonical_id=1610612747,
                    name="Los Angeles Lakers",
                    abbreviation="LAL",
                ),
            )
        ),
        resolver=_resolver(repository=repository),
        repository=repository,
    )

    board = service.get_board(NBAMarketQuery(season="2024-25"))

    assert board.usable
    assert [outcome.state.value for outcome in board.mapping_outcomes] == [
        "mapping_conflict"
    ]
    assert repository.get_active_mapping("prizepicks", "pp-15") is None


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
        def resolve_market(self, market, season, *, observed_at=None):
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
        def resolve_market(self, market, season, *, observed_at=None):
            raise AthleteMappingPersistenceError("mapping state is unreadable")

    class Repository:
        def record_resolution(self, resolution):  # pragma: no cover - never reached
            raise AssertionError("a failed read must not reach persistence")

    board = _board_service(
        _snapshot(market), resolver=BrokenReadResolver(), repository=Repository()
    ).get_board(NBAMarketQuery(season="2024-25"))

    assert _unresolved_markets(board) == (market,)
    assert board.mapping_outcomes == ()


def test_a_fail_closed_governed_result_never_hands_over_its_stored_mapping():
    resolution = _auto_resolution()
    suppressed = ProviderAthleteMappingRecord(
        provider="prizepicks",
        provider_athlete_id="pp-15",
        mapping_state="auto",
        is_active=True,
        season="2024-25",
        canonical_player_id=15,
        canonical_name="Nikola Jokić",
        canonical_team_id=1610612747,
        canonical_team_name="Los Angeles Lakers",
        canonical_team_abbreviation="LAL",
        provider_name="Nikola Jokic",
        provider_team_id=None,
        provider_team_canonical_id=None,
        provider_team_name=None,
        provider_team_abbreviation=None,
        conflict_canonical_player_id=None,
        conflict_canonical_name=None,
        first_seen_at="2026-08-09T12:00:00+00:00",
        last_seen_at="2026-08-09T12:00:00+00:00",
    )

    outcome = MappingPersistenceResult("rejected", False, mapping=suppressed).board_outcome(
        resolution
    )

    assert outcome.mapping is None
    assert outcome.canonical_player_id is None
    # The invariant is enforced by the type, not only by the conversion.
    with pytest.raises(ValueError):
        BoardMappingOutcome(
            resolution=resolution,
            state=MappingResolutionState.REJECTED,
            persisted=False,
            mapping=suppressed,
        )


class _RacingResolver:
    """Resolve one market, then let a decision land before the board persists.

    The board resolves outside the mapping transaction, so this gap is exactly
    where a real race lands.  Running the racing action here reproduces it
    deterministically, with no threads and no sleeping.
    """

    def __init__(self, resolver: AthleteResolver, race) -> None:
        self.resolver = resolver
        self.race = race
        self.resolved = []

    def resolve_market(self, market, season, *, observed_at=None):
        resolution = self.resolver.resolve_market(market, season, observed_at=observed_at)
        self.resolved.append(resolution)
        self.race()
        return resolution


def _racing_board(repository, snapshot, race, *, rows=None):
    resolver = _RacingResolver(_resolver(rows=rows, repository=repository), race)
    board = _board_service(
        snapshot, resolver=resolver, repository=repository
    ).get_board(NBAMarketQuery(season="2024-25"))
    return board, resolver


def test_board_reports_the_rejection_that_raced_its_auto_resolution(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    board, resolver = _racing_board(
        repository,
        _snapshot(_market()),
        lambda: repository.reject(
            "prizepicks",
            "pp-15",
            operator_id="ops@example.com",
            reason="provider identity is not trusted",
        ),
    )

    assert board.snapshots[0].markets[0].market_id == "m-pp-15"
    (outcome,) = board.mapping_outcomes
    # The board resolved an automatic mapping, but the rejection governs it.
    assert resolver.resolved[0].state is MappingResolutionState.AUTO
    assert outcome.observed_state is MappingResolutionState.AUTO
    assert outcome.state is MappingResolutionState.REJECTED
    assert outcome.persisted is False
    assert outcome.mapping is None
    assert outcome.canonical_player_id is None
    assert repository.get_active_mapping("prizepicks", "pp-15") is None


def test_board_withholds_a_suppressed_mapping_it_had_already_established(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _board_service(
        _snapshot(_market()),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(NBAMarketQuery(season="2024-25"))

    board, _ = _racing_board(
        repository,
        _snapshot(_market()),
        lambda: repository.reject(
            "prizepicks",
            "pp-15",
            operator_id="ops@example.com",
            reason="provider identity is not trusted",
        ),
    )

    (outcome,) = board.mapping_outcomes
    assert outcome.state is MappingResolutionState.REJECTED
    # The row the earlier board read established is still durable, but a
    # suppressed identity may not reach a comparison through it.
    assert repository.get_mapping("prizepicks", "pp-15").canonical_player_id == 15
    assert outcome.mapping is None
    assert outcome.canonical_player_id is None


def test_board_reports_the_conflict_an_auto_promotion_raced_into(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    disagreeing_team = _market(
        "pp-15",
        "Nikola Jokic",
        team=TeamEvidence(
            provider_id="pp-bos",
            canonical_id=1610612738,
            name="Boston Celtics",
            abbreviation="BOS",
        ),
    )

    board, resolver = _racing_board(
        repository,
        _snapshot(disagreeing_team),
        lambda: repository.record_resolution(_auto_resolution(observed_at=now)),
    )

    (outcome,) = board.mapping_outcomes
    # The observation could only say the provider's team disagrees; the mapping
    # a concurrent read established made that disagreement a conflict.
    assert resolver.resolved[0].state is MappingResolutionState.TEAM_CONFLICT
    assert outcome.observed_state is MappingResolutionState.TEAM_CONFLICT
    assert outcome.state is MappingResolutionState.MAPPING_CONFLICT
    assert outcome.mapping is None
    assert outcome.canonical_player_id is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.is_active is False


def test_board_reports_the_manual_mapping_that_raced_its_auto_resolution(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    board, resolver = _racing_board(
        repository,
        _snapshot(_market()),
        lambda: repository.approve(
            "prizepicks",
            "pp-15",
            23,
            season="2024-25",
            operator_id="ops@example.com",
            reason="reviewed source identity",
        ),
    )

    (outcome,) = board.mapping_outcomes
    assert resolver.resolved[0].canonical_player_id == 15
    assert outcome.state is MappingResolutionState.MANUAL_APPROVED
    assert outcome.persisted is False
    # The operator's athlete is what the identity means now, not the one this
    # board read resolved before the decision landed.
    assert outcome.mapping.canonical_player_id == 23
    assert outcome.canonical_player_id == 23
    assert repository.get_active_mapping("prizepicks", "pp-15").canonical_player_id == 23


def test_board_reports_current_state_for_a_read_fenced_as_stale(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution(observed_at=_OBSERVED_AFTER))

    board = _board_service(
        _snapshot(_market("pp-15", "LeBron James"), retrieved_at=_OBSERVED_BEFORE),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(NBAMarketQuery(season="2024-25"))

    (outcome,) = board.mapping_outcomes
    # The read was taken before the mapping it disagrees with was observed, so
    # it is fenced -- and the board reports the identity as it currently is.
    assert outcome.resolution.canonical_player_id == 23
    assert outcome.state is MappingResolutionState.AUTO
    assert outcome.persisted is False
    assert outcome.canonical_player_id == 15
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Nikola Jokic"


def test_board_keeps_reporting_the_mapping_a_repeated_read_left_alone(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    service = _board_service(
        _snapshot(_market()),
        resolver=_resolver(repository=repository),
        repository=repository,
    )
    query = NBAMarketQuery(season="2024-25")

    first = service.get_board(query)
    second = service.get_board(query)

    (established,) = first.mapping_outcomes
    (repeated,) = second.mapping_outcomes
    assert established.state is MappingResolutionState.AUTO
    assert established.persisted is True
    # The repeat changed nothing, which is not the same as observing nothing.
    assert repeated.state is MappingResolutionState.AUTO
    assert repeated.persisted is False
    assert repeated.canonical_player_id == 15
    assert len(repository.history(provider="prizepicks", provider_athlete_id="pp-15")) == 1


def test_a_fenced_read_that_left_no_mapping_claims_no_canonical_athlete(mapping_db):
    """A governed state without an active mapping row asserts no claim."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    unmatched = resolver.resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="King James"),
        "2024-25",
        observed_at=_OBSERVED_BEFORE,
    )
    assert repository.record_resolution(unmatched).persisted is True
    _reject_and_clear(repository, "pp-77")
    delayed = resolver.resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="LeBron James"),
        "2024-25",
        observed_at=_OBSERVED_BEFORE,
    )

    result = repository.record_resolution(delayed)
    outcome = result.board_outcome(delayed)

    # The read predates the clearing, so it is fenced and nothing is stored.
    assert result.state == "auto"
    assert result.persisted is False
    assert result.mapping is None
    assert repository.get_mapping("prizepicks", "pp-77") is None
    assert outcome.state is MappingResolutionState.AUTO
    assert outcome.resolution.canonical_player_id == 23
    # Only an active governed mapping supplies a canonical athlete.
    assert outcome.canonical_player_id is None


def test_board_reports_one_outcome_when_a_snapshot_repeats_an_identity(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    board = _board_service(
        _snapshot(_market(), _market(market_id="m-pp-15-again")),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(NBAMarketQuery(season="2024-25"))

    assert len(board.snapshots[0].markets) == 2
    (outcome,) = board.mapping_outcomes
    # The last write is the durable state, and the repeat changed nothing.
    assert outcome.state is MappingResolutionState.AUTO
    assert outcome.persisted is False
    assert outcome.canonical_player_id == 15
    assert len(repository.history(provider="prizepicks", provider_athlete_id="pp-15")) == 1


def test_board_reports_the_conflict_a_contradictory_snapshot_ended_in(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    board = _board_service(
        _snapshot(_market(), _market(name="LeBron James", market_id="m-pp-15-alt")),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(NBAMarketQuery(season="2024-25"))

    assert len(board.snapshots[0].markets) == 2
    # Two markets shared one provider identity and disagreed about it.  The
    # board reports the state that governs it now, not the claim it opened with.
    (outcome,) = board.mapping_outcomes
    assert outcome.state is MappingResolutionState.MAPPING_CONFLICT
    assert outcome.mapping is None
    assert outcome.canonical_player_id is None
    assert repository.get_active_mapping("prizepicks", "pp-15") is None


def test_board_keeps_every_observation_that_names_no_provider_identity(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    board = _board_service(
        _snapshot(
            _market(provider_athlete_id=None, market_id="m-1"),
            _market(provider_athlete_id=None, market_id="m-2"),
        ),
        resolver=_resolver(repository=repository),
        repository=repository,
    ).get_board(NBAMarketQuery(season="2024-25"))

    # Neither observation names an identity, so neither stands for the other.
    states = [outcome.state for outcome in board.mapping_outcomes]
    assert states == [MappingResolutionState.MISSING_IDENTITY] * 2


def test_board_reports_the_rejection_that_governs_a_repeated_identity(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    rejected = []

    def race():
        if rejected:
            return
        rejected.append(
            repository.reject(
                "prizepicks",
                "pp-15",
                operator_id="ops@example.com",
                reason="provider identity is not trusted",
            )
        )

    board, _ = _racing_board(
        repository, _snapshot(_market(), _market(market_id="m-pp-15-again")), race
    )

    (outcome,) = board.mapping_outcomes
    assert outcome.state is MappingResolutionState.REJECTED
    assert outcome.canonical_player_id is None


def test_board_reports_the_manual_mapping_a_contradictory_snapshot_ended_in(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolved = []

    def race():
        resolved.append(None)
        if len(resolved) != 3:
            return
        # An operator approves the identity the snapshot just conflicted over.
        repository.approve(
            "prizepicks",
            "pp-15",
            15,
            season="2024-25",
            operator_id="ops@example.com",
            reason="verified source identity",
            provider_evidence=AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        )

    board, _ = _racing_board(
        repository,
        _snapshot(
            _market(),
            _market(name="LeBron James", market_id="m-pp-15-alt"),
            _market(market_id="m-pp-15-again"),
        ),
        race,
    )

    # The identity conflicted mid-snapshot and was governed again before it
    # ended, so the board reports the mapping that is actually durable now.
    (outcome,) = board.mapping_outcomes
    assert outcome.state is MappingResolutionState.MANUAL_APPROVED
    assert outcome.canonical_player_id == 15
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "manual_approved"


def _reused_identity_auto(
    repository: AthleteMappingRepository, *, observed_at: datetime | None = None
):
    """The provider now reports LeBron James under the reused pp-15 identity."""

    return _resolver(repository=repository).resolve(
        "prizepicks",
        _reused_identity_evidence(),
        "2024-25",
        observed_at=observed_at,
    )


def test_resolution_carries_the_observation_instant_in_utc():
    resolver = _resolver()
    evidence = AthleteEvidence(provider_id="pp-15", name="Nikola Jokic")

    resolved = resolver.resolve(
        "prizepicks", evidence, "2024-25", observed_at="2026-08-09T13:00:00+00:00"
    )

    assert resolved.observed_at == _OBSERVED_AFTER
    with pytest.raises(ValueError, match="timezone-aware"):
        resolver.resolve(
            "prizepicks", evidence, "2024-25", observed_at=datetime(2026, 8, 9, 13)
        )


def test_the_decision_audit_records_the_observation_instant(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    repository.record_resolution(_auto_resolution(observed_at=_OBSERVED_AFTER))
    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )

    history = repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    assert [item.decision_state for item in history] == ["auto", "rejected"]
    # A board observation records when the provider was read; an operator
    # decision has no observation of its own to record.
    assert history[0].observed_at == _OBSERVED_AFTER.isoformat()
    assert history[0].to_dict()["observed_at"] == _OBSERVED_AFTER.isoformat()
    assert history[1].observed_at is None


def test_a_delayed_pre_clear_observation_cannot_deactivate_a_newer_mapping(mapping_db):
    """The rejection and its clearing govern every earlier board read.

    The identity was mapped, rejected, cleared, and mapped again to a different
    canonical athlete.  A read the provider produced before the clearing may
    still be in flight; landing it now would deactivate the newer mapping and
    queue a conflict from evidence that predates the operator's decision.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    stale = _auto_resolution(observed_at=_OBSERVED_BEFORE)
    assert repository.record_resolution(stale).state == "auto"
    _reject_and_clear(repository, "pp-15")
    fresh = _reused_identity_auto(repository, observed_at=_OBSERVED_AFTER)
    assert repository.record_resolution(fresh).state == "auto"

    replayed = repository.record_resolution(stale)

    assert replayed.state == "auto"
    assert replayed.persisted is False
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 23
    assert active.provider_name == "LeBron James"
    assert active.conflict_canonical_player_id is None
    assert repository.list_conflicts() == []
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "rejected",
        "rejection_cleared",
        "auto",
    ]


def test_a_delayed_pre_clear_conflict_cannot_requeue_a_newer_mapping(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    assert repository.record_resolution(
        _auto_resolution(observed_at=_OBSERVED_BEFORE)
    ).state == "auto"
    # Resolved while the identity was still mapped to player 15, so it reports
    # a conflict against that mapping rather than against the current one.
    stale_conflict = _resolver(
        rows=[_catalog_row(15, "Other Name"), _catalog_row(23, "Nikola Jokic")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
        observed_at=_OBSERVED_BEFORE,
    )
    assert stale_conflict.state is MappingResolutionState.MAPPING_CONFLICT
    _reject_and_clear(repository, "pp-15")
    repository.record_resolution(_reused_identity_auto(repository, observed_at=_OBSERVED_AFTER))

    replayed = repository.record_resolution(stale_conflict)

    assert replayed.state == "auto"
    assert replayed.persisted is False
    assert repository.list_conflicts() == []
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.is_active is True
    assert mapping.canonical_player_id == 23
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "rejected",
        "rejection_cleared",
        "auto",
    ]


def test_a_delayed_pre_clear_unmatched_observation_cannot_requeue_an_identity(
    mapping_db,
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    stale = _resolver(
        rows=[_catalog_row(23, "LeBron James")], repository=repository
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="King James"),
        "2024-25",
        observed_at=_OBSERVED_BEFORE,
    )
    assert repository.record_resolution(stale).persisted is True
    _reject_and_clear(repository, "pp-77")
    named = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="LeBron James"),
        "2024-25",
        observed_at=_OBSERVED_AFTER,
    )
    assert repository.record_resolution(named).state == "auto"

    replayed = repository.record_resolution(stale)

    assert replayed.persisted is False
    assert repository.list_unresolved(provider="prizepicks") == []
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-77")
    ] == ["unmatched", "rejected", "rejection_cleared", "auto"]


def test_a_racing_delayed_observation_never_beats_the_newer_one(mapping_db):
    """Whichever read commits first, the older observation changes nothing."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    stale = _auto_resolution(observed_at=_OBSERVED_BEFORE)
    repository.record_resolution(stale)
    _reject_and_clear(repository, "pp-15")
    fresh = _reused_identity_auto(repository, observed_at=_OBSERVED_AFTER)
    barrier = threading.Barrier(2)

    def _record(resolution):
        barrier.wait(timeout=5)
        return repository.record_resolution(resolution)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_record, stale),
            executor.submit(_record, fresh),
        ]
        for future in futures:
            future.result(timeout=10)

    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 23
    assert repository.list_conflicts() == []
    states = [item.decision_state for item in repository.history()]
    assert states == ["auto", "rejected", "rejection_cleared", "auto"]


def test_a_reactivated_identity_retains_no_conflict_fields(mapping_db):
    """A conflict is evidence for one state, not a scar on every later one."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution(observed_at=_OBSERVED_BEFORE))
    conflict = _resolver(
        rows=[_catalog_row(15, "Other Name"), _catalog_row(23, "Nikola Jokic")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
        observed_at=_OBSERVED_BEFORE,
    )
    assert repository.record_resolution(conflict).state == "mapping_conflict"
    queued = repository.get_mapping("prizepicks", "pp-15")
    assert queued is not None
    assert queued.conflict_canonical_player_id == 23

    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    rejected = repository.get_mapping("prizepicks", "pp-15")
    assert rejected is not None
    assert rejected.mapping_state == "rejected"
    assert rejected.conflict_canonical_player_id is None
    assert rejected.conflict_canonical_name is None

    assert repository.clear_rejection(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="the identity was reinstated",
    )
    assert repository.record_resolution(
        _reused_identity_auto(repository, observed_at=_OBSERVED_AFTER)
    ).state == "auto"

    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "auto"
    assert active.canonical_player_id == 23
    assert active.conflict_canonical_player_id is None
    assert active.conflict_canonical_name is None
    assert repository.list_conflicts() == []


def test_a_manual_reactivation_retains_no_conflict_fields(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    conflict = _resolver(
        rows=[_catalog_row(15, "Other Name"), _catalog_row(23, "Nikola Jokic")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
    )
    assert repository.record_resolution(conflict).state == "mapping_conflict"

    repository.approve(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the provider reused the identity",
    )

    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.conflict_canonical_player_id is None
    assert active.conflict_canonical_name is None
    assert repository.list_conflicts() == []


def test_board_fences_a_snapshot_retrieved_before_a_cleared_rejection(mapping_db):
    """The board plumbs the snapshot's retrieval instant into the audit."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    provider = _StaticProvider(_snapshot(_market(), retrieved_at=_OBSERVED_BEFORE))
    service = DFSBoardService(
        provider_registry={"prizepicks": provider},
        athlete_resolver=_resolver(repository=repository),
        athlete_mapping_repository=repository,
    )
    query = NBAMarketQuery(season="2024-25")
    service.get_board(query)
    _reject_and_clear(repository, "pp-15")

    provider.snapshot = _snapshot(
        _market(name="LeBron James"), retrieved_at=_OBSERVED_AFTER
    )
    service.get_board(query)
    # A slow provider read retrieved before the clearing finally arrives.
    provider.snapshot = _snapshot(_market(), retrieved_at=_OBSERVED_BEFORE)
    delayed = service.get_board(query)

    assert delayed.usable
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 23
    assert repository.list_conflicts() == []
    history = repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    assert [item.decision_state for item in history] == [
        "auto",
        "rejected",
        "rejection_cleared",
        "auto",
    ]
    assert history[0].observed_at == _OBSERVED_BEFORE.isoformat()
    assert history[-1].observed_at == _OBSERVED_AFTER.isoformat()


def test_a_delayed_pre_clear_observation_of_an_unmapped_identity_writes_nothing(
    mapping_db,
):
    """An identity may be governed without ever holding a mapping row."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    stale = _resolver(
        rows=[_catalog_row(23, "LeBron James")], repository=repository
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="King James"),
        "2024-25",
        observed_at=_OBSERVED_BEFORE,
    )
    assert repository.record_resolution(stale).persisted is True
    _reject_and_clear(repository, "pp-77")

    replayed = repository.record_resolution(stale)

    assert replayed.state == "unmatched"
    assert replayed.persisted is False
    assert replayed.mapping is None
    assert repository.get_mapping("prizepicks", "pp-77") is None
    assert repository.list_unresolved(provider="prizepicks") == []
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-77")
    ] == ["unmatched", "rejected", "rejection_cleared"]


def _observation_clock(engine, provider_id: str) -> datetime | None:
    """Read the identity's durable high-water mark straight from the database."""

    with engine.connect() as connection:
        stored = connection.execute(
            select(AthleteMappingLock.last_observed_at).where(
                and_(
                    AthleteMappingLock.provider == "prizepicks",
                    AthleteMappingLock.provider_athlete_id == provider_id,
                )
            )
        ).scalar()
    if stored is None:
        return None
    return stored if stored.tzinfo else stored.replace(tzinfo=timezone.utc)


def test_a_repeated_observation_advances_the_durable_observation_clock(mapping_db):
    """Audit dedupe suppresses the row, never the instant it was observed."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    assert repository.record_resolution(
        _auto_resolution(observed_at=_OBSERVED_BEFORE)
    ).persisted is True
    assert _observation_clock(engine, "pp-15") == _OBSERVED_BEFORE
    repeated = repository.record_resolution(_auto_resolution(observed_at=_OBSERVED_AFTER))

    assert repeated.persisted is False
    assert len(repository.history()) == 1
    assert _observation_clock(engine, "pp-15") == _OBSERVED_AFTER


def test_a_suppressed_duplicate_observation_still_fences_a_delayed_read(mapping_db):
    """The identity was observed twice; only one decision records it.

    A conflicting read taken between the two arrives late.  Its evidence
    predates what the provider has since reported, so landing it would
    deactivate the mapping and queue a conflict between two canonical athletes
    that were never claimed at the same time.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution(observed_at=_OBSERVED_BEFORE))
    assert repository.record_resolution(
        _auto_resolution(observed_at=_OBSERVED_AFTER)
    ).persisted is False

    delayed = repository.record_resolution(
        _reused_identity_auto(repository, observed_at=_OBSERVED_BETWEEN)
    )

    assert delayed.state == "auto"
    assert delayed.persisted is False
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 15
    assert active.provider_name == "Nikola Jokic"
    assert repository.list_conflicts() == []
    assert [item.decision_state for item in repository.history()] == ["auto"]


def test_a_repeated_unresolved_observation_fences_a_delayed_read_without_a_mapping(
    mapping_db,
):
    """An identity with no mapping row still has an observation history."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    def _unmatched(observed_at):
        return _resolver(
            rows=[_catalog_row(23, "LeBron James")], repository=repository
        ).resolve(
            "prizepicks",
            AthleteEvidence(provider_id="pp-77", name="King James"),
            "2024-25",
            observed_at=observed_at,
        )

    assert repository.record_resolution(_unmatched(_OBSERVED_BEFORE)).persisted is True
    assert repository.record_resolution(_unmatched(_OBSERVED_AFTER)).persisted is False
    delayed = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="LeBron James"),
        "2024-25",
        observed_at=_OBSERVED_BETWEEN,
    )

    replayed = repository.record_resolution(delayed)

    assert replayed.persisted is False
    assert repository.get_mapping("prizepicks", "pp-77") is None
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-77")
    ] == ["unmatched"]


def test_concurrent_repeats_of_one_observation_advance_the_clock_once(mapping_db):
    """The high-water mark is raised inside the identity's own transaction."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution(observed_at=_OBSERVED_BEFORE))
    repeated = _auto_resolution(observed_at=_OBSERVED_AFTER)
    barrier = threading.Barrier(2)

    def _record():
        barrier.wait(timeout=5)
        return repository.record_resolution(repeated)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_record), executor.submit(_record)]
        for future in futures:
            assert future.result(timeout=10).persisted is False

    assert _observation_clock(engine, "pp-15") == _OBSERVED_AFTER
    delayed = repository.record_resolution(
        _reused_identity_auto(repository, observed_at=_OBSERVED_BETWEEN)
    )
    assert delayed.persisted is False
    assert repository.list_conflicts() == []
    assert [item.decision_state for item in repository.history()] == ["auto"]


def _inactive_catalog_resolution(
    repository: AthleteMappingRepository, *, observed_at: datetime | None = None
):
    """The requested season now lists Nikola Jokić as inactive."""

    return _resolver(
        rows=[
            _catalog_row(15, "Nikola Jokić", active=False),
            _catalog_row(23, "LeBron James"),
        ],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
        observed_at=observed_at,
    )


def test_an_inactive_only_observation_withdraws_the_automatic_mapping(mapping_db):
    """An athlete the catalog no longer lists as active cannot be compared.

    The mapping stays as the durable record of the claim, but it is inactive,
    so no board comparison can reach it, and the candidate evidence stays in
    the operator's unresolved queue.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    assert repository.record_resolution(_auto_resolution()).state == "auto"

    result = repository.record_resolution(_inactive_catalog_resolution(repository))

    assert result.state == "inactive_only"
    assert result.persisted is True
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "inactive_only"
    assert mapping.is_active is False
    assert mapping.canonical_player_id == 15
    assert mapping.conflict_canonical_player_id is None
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == ["inactive_only"]
    assert [candidate.canonical_player_id for candidate in queued[0].candidates] == [15]
    assert queued[0].provider_name == "Nikola Jokic"
    assert repository.list_conflicts() == []


def test_a_repeated_inactive_only_observation_changes_nothing(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_inactive_catalog_resolution(repository))

    repeated = repository.record_resolution(_inactive_catalog_resolution(repository))

    assert repeated.state == "inactive_only"
    assert repeated.persisted is False
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "inactive_only",
    ]


def test_a_reactivated_catalog_row_maps_the_identity_again(mapping_db):
    """Catalog inactivity suspends the claim; it does not retract it."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_inactive_catalog_resolution(repository))

    again = repository.record_resolution(_auto_resolution())

    assert again.state == "auto"
    assert again.persisted is True
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "auto"
    assert active.canonical_player_id == 15
    assert active.conflict_canonical_player_id is None
    assert active.conflict_canonical_name is None
    assert repository.list_unresolved(provider="prizepicks") == []
    assert repository.list_conflicts() == []
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "inactive_only",
        "auto",
    ]


def test_a_withdrawn_mapping_still_conflicts_with_a_reused_identity(mapping_db):
    """A suspended claim is still a claim, so a reused ID cannot remap silently."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_inactive_catalog_resolution(repository))

    reused = _reused_identity_auto(repository)
    assert reused.state is MappingResolutionState.MAPPING_CONFLICT
    result = repository.record_resolution(reused)

    assert result.state == "mapping_conflict"
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.conflict_canonical_player_id == 23
    assert [item.mapping.provider_athlete_id for item in repository.list_conflicts()] == [
        "pp-15"
    ]


def _renamed_inactive_resolution(
    repository: AthleteMappingRepository, *, name: str = "Nikola Jokic"
):
    """The claimed row was relabeled *and* is inactive for the season."""

    return _resolver(
        rows=[
            _catalog_row(15, "Nikola Jokić Sr.", active=False),
            _catalog_row(23, "LeBron James"),
        ],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name=name),
        "2024-25",
    )


def test_a_renamed_inactive_row_withdraws_the_claim_by_canonical_id(mapping_db):
    """Identity is the canonical player ID, so a rename cannot hide inactivity.

    The provider still reports the name we observed, so the claim still stands;
    the catalog just relabeled the row and stopped listing it as active.  Read
    by canonical ID that is an inactive-only observation, not an unmatched one,
    so the mapping is withdrawn and the evidence reaches the operator's queue.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    withdrawn = _renamed_inactive_resolution(repository)

    assert withdrawn.state is MappingResolutionState.INACTIVE_ONLY
    assert [candidate.player_id for candidate in withdrawn.candidates] == [15]
    result = repository.record_resolution(withdrawn)

    assert result.state == "inactive_only"
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "inactive_only"
    assert mapping.canonical_player_id == 15
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == ["inactive_only"]
    assert [candidate.canonical_player_id for candidate in queued[0].candidates] == [15]


def test_a_renamed_active_row_still_retains_the_withdrawn_claim(mapping_db):
    """The safe path: same provider name, active row, relabeled catalog entry."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_renamed_inactive_resolution(repository))

    retained = _resolver(
        rows=[_catalog_row(15, "Nikola Jokić Sr."), _catalog_row(23, "LeBron James")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
    )

    assert retained.state is MappingResolutionState.AUTO
    assert retained.reason == "retained_canonical_identity"
    assert retained.canonical_athlete.player_id == 15
    assert repository.record_resolution(retained).state == "auto"
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.canonical_player_id == 15
    assert active.canonical_name == "Nikola Jokić Sr."


def test_a_withdrawn_claim_is_not_reactivated_by_an_unmatched_new_name(mapping_db):
    """Retention needs the provider to still be reporting the same athlete.

    A wholly unmatched label under a claimed identity is a reused provider ID,
    not a relabeling, so the identity fails closed instead of quietly reviving
    the old canonical claim.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_renamed_inactive_resolution(repository))

    mystery = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Mystery Prospect"),
        "2024-25",
    )

    assert mystery.state is MappingResolutionState.MAPPING_CONFLICT
    assert repository.record_resolution(mystery).state == "mapping_conflict"
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "mapping_conflict"
    # The claim and the evidence that unseated it both survive for review.
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Mystery Prospect"


def test_an_exact_inactive_match_on_another_player_conflicts_with_the_claim(
    mapping_db,
):
    """An exact label naming a different athlete cannot keep the old claim."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    conflicting = _resolver(
        rows=[
            _catalog_row(15, "Nikola Jokić"),
            _catalog_row(23, "LeBron James", active=False),
        ],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="LeBron James"),
        "2024-25",
    )

    assert conflicting.state is MappingResolutionState.MAPPING_CONFLICT
    assert [candidate.player_id for candidate in conflicting.candidates] == [23]
    assert repository.record_resolution(conflicting).state == "mapping_conflict"
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "mapping_conflict"
    assert mapping.conflict_canonical_player_id == 23
    assert mapping.provider_name == "LeBron James"


def test_catalog_inactivity_never_withdraws_a_manual_mapping(mapping_db):
    """Only a governed conflict may unseat an operator's decision."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    _approve_pp_15(repository)
    # Resolved without seeing the manual mapping, as a read in flight would be.
    inactive = _resolver(
        rows=[_catalog_row(15, "Nikola Jokić", active=False)]
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
    )
    assert inactive.state is MappingResolutionState.INACTIVE_ONLY

    result = repository.record_resolution(inactive)

    assert result.state == "manual_approved"
    assert result.persisted is False
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "manual_approved"
    assert repository.list_unresolved(provider="prizepicks") == []


def _manual_map_pp_15(
    repository: AthleteMappingRepository, action: str, canonical_player_id: int = 15
) -> None:
    """Approve or override pp-15 on the evidence the operator reviewed."""

    getattr(repository, action)(
        "prizepicks",
        "pp-15",
        canonical_player_id,
        season="2024-25",
        operator_id="ops@example.com",
        reason="verified source identity",
        provider_evidence=_approved_evidence(),
    )


def _agreeing_manual_observation(
    repository: AthleteMappingRepository, *, observed_at: datetime | None = None
):
    """A board read of the identity the operator already decided."""

    return _resolver(repository=repository).resolve(
        "prizepicks",
        _approved_evidence(),
        "2024-25",
        observed_at=observed_at,
    )


@pytest.mark.parametrize("action", ["approve", "override"])
def test_an_agreeing_manual_observation_advances_the_observation_clock(
    mapping_db, action
):
    """A governed read is still a read: it changes nothing but the clock.

    The operator's mapping stays exactly as it was and the audit gains no
    duplicate, but the identity has demonstrably been observed at that instant,
    so its high-water mark has to move.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _manual_map_pp_15(repository, action)
    state = f"manual_{'approved' if action == 'approve' else 'override'}"

    result = repository.record_resolution(
        _agreeing_manual_observation(repository, observed_at=_OBSERVED_AFTER)
    )

    assert result.state == state
    assert result.persisted is False
    assert _observation_clock(engine, "pp-15") == _OBSERVED_AFTER
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == state
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Nikola Jokic"
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == [state]


def test_a_newer_agreeing_manual_read_fences_an_older_conflict(mapping_db):
    """The provider has since reported the approved identity again.

    A conflicting read taken before that later agreeing one describes evidence
    the provider no longer reports, so landing it would unseat the operator's
    mapping on stale evidence.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    resolver = _resolver(repository=repository)
    _approve_pp_15(repository)
    delayed = resolver.resolve(
        "prizepicks",
        _reused_identity_evidence(),
        "2024-25",
        observed_at=_OBSERVED_BETWEEN,
    )
    assert delayed.state is MappingResolutionState.MAPPING_CONFLICT
    repository.record_resolution(
        _agreeing_manual_observation(repository, observed_at=_OBSERVED_AFTER)
    )

    result = repository.record_resolution(delayed)

    assert result.state == "manual_approved"
    assert result.persisted is False
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Nikola Jokic"
    assert repository.list_conflicts() == []
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["manual_approved"]


def test_concurrent_agreeing_manual_reads_advance_the_clock_once(mapping_db):
    """The governed no-op still runs inside the identity's own transaction."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _approve_pp_15(repository)
    observation = _agreeing_manual_observation(repository, observed_at=_OBSERVED_AFTER)
    barrier = threading.Barrier(2)

    def _record():
        barrier.wait(timeout=5)
        return repository.record_resolution(observation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_record), executor.submit(_record)]
        for future in futures:
            assert future.result(timeout=10).persisted is False

    assert _observation_clock(engine, "pp-15") == _OBSERVED_AFTER
    assert [item.decision_state for item in repository.history()] == ["manual_approved"]


def test_an_agreeing_manual_read_without_an_instant_leaves_the_clock_alone(mapping_db):
    """A caller that reports no retrieval time cannot be ordered."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _approve_pp_15(repository)

    result = repository.record_resolution(_agreeing_manual_observation(repository))

    assert result.state == "manual_approved"
    assert result.persisted is False
    assert _observation_clock(engine, "pp-15") is None
    assert [item.decision_state for item in repository.history()] == ["manual_approved"]
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15


def _duplicate_name_resolution(
    repository: AthleteMappingRepository, *, observed_at: datetime | None = None
):
    """The requested season now lists two active athletes with one name."""

    return _resolver(
        rows=[
            _catalog_row(15, "Nikola Jokić"),
            _catalog_row(99, "Nikola Jokic", team_id=1610612743),
        ],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
        observed_at=observed_at,
    )


def test_an_ambiguous_observation_withdraws_the_automatic_mapping(mapping_db):
    """Evidence that names two athletes cannot keep one of them mapped.

    The claim is suspended rather than retracted: the row stays as the durable
    record, inactive so no comparison can reach it, while the candidates the
    board could not choose between wait in the operator's queue.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    assert repository.record_resolution(_auto_resolution()).state == "auto"

    result = repository.record_resolution(_duplicate_name_resolution(repository))

    assert result.state == "ambiguous"
    assert result.persisted is True
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "ambiguous"
    assert mapping.is_active is False
    assert mapping.canonical_player_id == 15
    assert mapping.conflict_canonical_player_id is None
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == ["ambiguous"]
    assert [candidate.canonical_player_id for candidate in queued[0].candidates] == [
        15,
        99,
    ]
    assert queued[0].provider_name == "Nikola Jokic"
    assert repository.list_conflicts() == []
    assert [mapping.provider_athlete_id for mapping in repository.list_mappings()] == [
        "pp-15"
    ]


def test_a_repeated_ambiguous_observation_changes_nothing(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_duplicate_name_resolution(repository))

    repeated = repository.record_resolution(_duplicate_name_resolution(repository))

    assert repeated.state == "ambiguous"
    assert repeated.persisted is False
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        "ambiguous",
    ]


def test_a_resolved_duplicate_name_maps_the_identity_again(mapping_db):
    """Ambiguity suspends the claim; the same athlete reclaims it."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_duplicate_name_resolution(repository))

    again = repository.record_resolution(_auto_resolution())

    assert again.state == "auto"
    assert again.persisted is True
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "auto"
    assert active.canonical_player_id == 15
    assert repository.list_unresolved(provider="prizepicks") == []


def test_an_ambiguous_withdrawal_still_fences_a_different_canonical_athlete(mapping_db):
    """A suspended claim is not an invitation to remap the identity."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_duplicate_name_resolution(repository))

    resolved_elsewhere = _resolver(
        rows=[_catalog_row(99, "Nikola Jokic", team_id=1610612747)],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
    )

    assert resolved_elsewhere.state is MappingResolutionState.MAPPING_CONFLICT
    result = repository.record_resolution(resolved_elsewhere)
    assert result.state == "mapping_conflict"
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    assert mapping.conflict_canonical_player_id == 99


def test_an_operator_resolves_an_ambiguous_withdrawal_by_approving_one_athlete(
    mapping_db,
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_duplicate_name_resolution(repository))

    result = repository.approve(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the duplicate names a different athlete",
    )

    assert result.mapping.mapping_state == "manual_approved"
    assert result.mapping.is_active is True
    assert result.mapping.canonical_player_id == 23
    assert repository.list_unresolved(provider="prizepicks") == []


def test_ambiguity_never_withdraws_a_manual_mapping(mapping_db):
    """Only a governed conflict may unseat an operator's decision."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _approve_pp_15(repository)
    # Resolved without seeing the manual mapping, as a read in flight would be.
    ambiguous = _duplicate_name_resolution(None)

    result = repository.record_resolution(ambiguous)

    assert result.state == "manual_approved"
    assert result.persisted is False
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "manual_approved"
    assert repository.list_unresolved(provider="prizepicks") == []


def test_a_reused_provider_identity_conflicts_instead_of_reading_unmatched(mapping_db):
    """Later evidence matching nothing is ordered by the established claim.

    The provider stopped reporting the name that was mapped, so the identity
    may have been reused.  It fails closed as a conflict rather than landing as
    an unmatched observation beside a mapping that stays comparable.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    reused = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Unlisted Prospect"),
        "2024-25",
    )

    assert reused.state is MappingResolutionState.MAPPING_CONFLICT
    assert repository.record_resolution(reused).state == "mapping_conflict"
    assert repository.get_active_mapping("prizepicks", "pp-15") is None


def _absent_catalog_resolution(
    repository: AthleteMappingRepository, *, observed_at: datetime | None = None
):
    """The requested season no longer lists the claimed athlete at all.

    The provider still reports the name the claim was established on, so the
    identity has not been reused; the catalog row it was mapped to is simply
    gone from the requested season.
    """

    return _resolver(
        rows=[_catalog_row(23, "LeBron James")],
        repository=repository,
    ).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        "2024-25",
        observed_at=observed_at,
    )


def test_a_vanished_catalog_row_withdraws_the_claim_as_unmatched(mapping_db):
    """A claim whose catalog row disappeared may not stay comparable.

    The board can no longer say the identity is that canonical athlete, so the
    mapping is withdrawn to ``unmatched`` while keeping the claim, and the
    observation queues the athlete that vanished as its evidence.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    assert repository.record_resolution(_auto_resolution()).state == "auto"

    resolution = _absent_catalog_resolution(repository)
    assert resolution.state is MappingResolutionState.UNMATCHED
    assert resolution.reason == "claimed_athlete_absent"
    result = repository.record_resolution(resolution)

    assert result.state == "unmatched"
    assert result.persisted is True
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "unmatched"
    assert mapping.is_active is False
    assert mapping.canonical_player_id == 15
    assert mapping.canonical_name == "Nikola Jokić"
    assert mapping.conflict_canonical_player_id is None
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == ["unmatched"]
    assert queued[0].reason == "claimed_athlete_absent"
    assert queued[0].provider_name == "Nikola Jokic"
    assert [
        (candidate.canonical_player_id, candidate.is_active_for_season)
        for candidate in queued[0].candidates
    ] == [(15, False)]
    assert repository.list_conflicts() == []
    assert [item.provider_athlete_id for item in repository.list_mappings()] == ["pp-15"]


def test_an_unclaimed_unmatched_observation_names_no_withdrawn_claim(mapping_db):
    """An identity with no claim is an ordinary unmatched observation.

    Nothing was withdrawn, so the queue names no canonical candidate and no
    current mapping state is created for the identity.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)

    resolution = _resolver(repository=repository).resolve(
        "prizepicks",
        AthleteEvidence(provider_id="pp-77", name="Unlisted Prospect"),
        "2024-25",
    )
    assert resolution.state is MappingResolutionState.UNMATCHED
    assert resolution.reason == "unmatched"
    result = repository.record_resolution(resolution)

    assert result.state == "unmatched"
    assert result.persisted is True
    assert repository.get_mapping("prizepicks", "pp-77") is None
    assert repository.list_mappings() == []
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == ["unmatched"]
    assert queued[0].reason == "unmatched"
    assert queued[0].candidates == ()


def _withdrawal(name: str):
    """Return the withdrawing observation factory one transition step names."""

    return {
        "inactive_only": _inactive_catalog_resolution,
        "ambiguous": _duplicate_name_resolution,
        "unmatched": _absent_catalog_resolution,
    }[name]


@pytest.mark.parametrize(
    "sequence",
    [
        ("inactive_only", "ambiguous"),
        ("ambiguous", "inactive_only"),
        ("inactive_only", "unmatched"),
        ("unmatched", "inactive_only"),
        ("ambiguous", "unmatched"),
        ("unmatched", "ambiguous"),
        ("inactive_only", "ambiguous", "unmatched", "inactive_only"),
    ],
)
def test_successive_withdrawals_move_the_current_mapping_state(mapping_db, sequence):
    """Each withdrawal is news about the identity, inactive row or not.

    An operator reads the current row to decide, so it has to say why the
    identity is withdrawn *now* rather than why it was withdrawn first.  The
    canonical claim and the provider evidence that established it are kept
    through every step.
    """

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())

    for step in sequence:
        result = repository.record_resolution(_withdrawal(step)(repository))
        assert result.state == step
        assert result.persisted is True

    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == sequence[-1]
    assert mapping.is_active is False
    assert mapping.canonical_player_id == 15
    assert mapping.provider_name == "Nikola Jokic"
    assert mapping.conflict_canonical_player_id is None
    assert mapping.conflict_canonical_name is None
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    assert [item.decision_state for item in repository.history()] == [
        "auto",
        *sequence,
    ]
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == [sequence[-1]]


@pytest.mark.parametrize("state", ["inactive_only", "ambiguous", "unmatched"])
def test_a_repeated_withdrawal_only_advances_the_observation_clock(mapping_db, state):
    """The same withdrawal twice is not a new transition."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    withdrawal = _withdrawal(state)
    repository.record_resolution(withdrawal(repository, observed_at=_OBSERVED_BEFORE))
    first = repository.get_mapping("prizepicks", "pp-15")

    repeated = repository.record_resolution(
        withdrawal(repository, observed_at=_OBSERVED_AFTER)
    )

    assert repeated.state == state
    assert repeated.persisted is False
    assert repository.get_mapping("prizepicks", "pp-15") == first
    assert [item.decision_state for item in repository.history()] == ["auto", state]
    assert _observation_clock(engine, "pp-15") == _OBSERVED_AFTER


@pytest.mark.parametrize("state", ["inactive_only", "ambiguous", "unmatched"])
def test_a_withdrawn_identity_is_reclaimed_by_the_same_canonical_athlete(
    mapping_db, state
):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_withdrawal(state)(repository))

    again = repository.record_resolution(_auto_resolution())

    assert again.state == "auto"
    assert again.persisted is True
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "auto"
    assert active.canonical_player_id == 15
    assert active.conflict_canonical_player_id is None
    assert repository.list_unresolved(provider="prizepicks") == []
    assert repository.list_conflicts() == []


@pytest.mark.parametrize("state", ["inactive_only", "ambiguous", "unmatched"])
def test_a_withdrawn_identity_still_fails_closed_on_a_different_athlete(
    mapping_db, state
):
    """A suspended claim is still a claim, whichever way it was suspended."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_withdrawal(state)(repository))

    reused = _reused_identity_auto(repository)
    assert reused.state is MappingResolutionState.MAPPING_CONFLICT
    result = repository.record_resolution(reused)

    assert result.state == "mapping_conflict"
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    assert mapping.conflict_canonical_player_id == 23
    assert repository.get_active_mapping("prizepicks", "pp-15") is None


def test_catalog_absence_never_withdraws_a_manual_mapping(mapping_db):
    """Only a governed conflict may unseat an operator's decision."""

    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    _approve_pp_15(repository)
    # Resolved without seeing the manual mapping, as a read in flight would be.
    absent = _absent_catalog_resolution(None)

    result = repository.record_resolution(absent)

    assert result.state == "manual_approved"
    assert result.persisted is False
    active = repository.get_active_mapping("prizepicks", "pp-15")
    assert active is not None
    assert active.mapping_state == "manual_approved"
    assert repository.list_unresolved(provider="prizepicks") == []


def test_an_operator_resolves_a_vanished_claim_by_approving_another_athlete(mapping_db):
    engine, now = mapping_db
    repository = AthleteMappingRepository(engine, clock=lambda: now)
    repository.record_resolution(_auto_resolution())
    repository.record_resolution(_absent_catalog_resolution(repository))

    result = repository.approve(
        "prizepicks",
        "pp-15",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="the catalog dropped the mapped athlete",
    )

    assert result.mapping.mapping_state == "manual_approved"
    assert result.mapping.is_active is True
    assert result.mapping.canonical_player_id == 23
    assert repository.list_unresolved(provider="prizepicks") == []
