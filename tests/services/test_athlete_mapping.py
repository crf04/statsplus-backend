"""Contract tests for season-scoped provider athlete mappings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, insert

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
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
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.athlete_mapping_repository import AthleteMappingPersistenceError
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


def test_migration_006_creates_mapping_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping.sqlite3'}")

    result = run_migrations(engine)

    assert "006_create_athlete_mappings" in result.applied
    assert {
        "provider_athlete_mappings",
        "athlete_mapping_decisions",
        "athlete_mapping_rejections",
    }.issubset(inspect(engine).get_table_names())


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


def _auto_resolution(engine, *, provider_id: str = "pp-15", name: str = "Nikola Jokic"):
    resolver = AthleteResolver(
        type(
            "Catalog",
            (),
            {"get_catalog": lambda _self, season, active_only=False: [
                _catalog_row(15, "Nikola Jokić"),
                _catalog_row(23, "LeBron James"),
            ]},
        )()
    )
    return resolver.resolve(
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
    resolution = _auto_resolution(engine)

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
    resolution = _auto_resolution(engine)

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
    resolution = _auto_resolution(engine)
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


def test_rejection_suppresses_until_clear_and_clear_is_audited(mapping_db):
    engine, _ = mapping_db
    repository = AthleteMappingRepository(engine)
    resolution = _auto_resolution(engine, provider_id="pp-23", name="LeBron James")

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
    first = _auto_resolution(engine)
    repository.persist_auto_decision(first)
    second = AthleteResolver(
        type(
            "Catalog",
            (),
            {"get_catalog": lambda _self, season, active_only=False: [
                _catalog_row(15, "Other Name"),
                _catalog_row(23, "Nikola Jokic"),
            ]},
        )(),
        mapping_repository=repository,
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


def test_board_returns_market_when_mapping_persistence_fails():
    market = PlayerProjectionMarket(
        provider="prizepicks",
        market_id="m-1",
        athlete=AthleteEvidence(provider_id="pp-15", name="Nikola Jokic"),
        statistic=StatisticEvidence(provider_id="pts"),
        threshold=MarketThreshold(value="20.5", unit="points"),
        status=MarketStatus.AVAILABLE,
    )
    snapshot = ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
    )

    class Provider:
        def get_snapshot(self, query, context):
            return snapshot

    class Resolver:
        def resolve(self, market, season):
            return type("Resolution", (), {"is_auto_qualifying": True})()

    class BrokenRepository:
        def record_resolution(self, resolution):
            raise AthleteMappingPersistenceError("database unavailable")

    board = DFSBoardService(
        provider_registry={"prizepicks": Provider()},
        athlete_resolver=Resolver(),
        athlete_mapping_repository=BrokenRepository(),
    ).get_board(
        NBAMarketQuery(season="2024-25")
    )

    # The market survives the persistence failure. Statistic resolution still
    # attaches its canonical match, so compare the market identity rather than
    # the pre-resolution instance.
    retained = board.snapshots[0].markets
    assert len(retained) == 1
    assert replace(retained[0], statistic_match=None) == market
    assert retained[0].statistic_match is not None
