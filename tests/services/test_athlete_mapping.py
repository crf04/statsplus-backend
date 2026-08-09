"""Contract tests for season-scoped provider athlete mappings."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, inspect, insert
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


#: Fixed clearing timestamp for direct-insert constraint cases.
_CLEARED_AT = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


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
