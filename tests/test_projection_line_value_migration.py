"""Migration 063: projection rows gain a nullable line value, never backfilled."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.migrations import run_migrations
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
    TeamEvidence,
)
from app.services.projection_archive import ProjectionArchive
from app.services.statistic_catalog import StatisticCatalog

pytestmark = pytest.mark.real_migrations

OBSERVED_AT = datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc)
_TABLES = ("projection_observations", "latest_player_projections")


def _snapshot(catalog) -> ProviderSnapshot:
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="market-1",
        athlete=AthleteEvidence(
            canonical_id=7, name="Player 7", team=TeamEvidence(canonical_id=10)
        ),
        event=EventEvidence(canonical_id="0022500501"),
        team=TeamEvidence(canonical_id=10),
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
                selection_id="higher",
                direction=SelectionDirection.HIGHER,
                american_price=-110,
            ),
        ),
    )
    return ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1, eligible_count=1, normalized_count=1, expected_total=1
        ),
        retrieved_at=OBSERVED_AT,
    )


def _line_values(engine) -> dict[str, list]:
    with engine.connect() as connection:
        return {
            table: [
                row[0]
                for row in connection.execute(text(f"SELECT line_value FROM {table}"))
            ]
            for table in _TABLES
        }


def test_existing_rows_keep_a_null_line_and_rerun_is_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    ProjectionArchive(engine, StatisticCatalog.load_default()).ingest_complete_snapshot(
        _snapshot(StatisticCatalog.load_default()),
        query=NBAMarketQuery(season="2025-26"),
        accepted_at=OBSERVED_AT,
    )
    assert _line_values(engine) == {
        "projection_observations": [27.5],
        "latest_player_projections": [27.5],
    }
    # The pre-063 shape: the same rows without the column.
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"ALTER TABLE {table} DROP COLUMN line_value"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 63"))

    first = run_migrations(engine)

    assert first.applied == ("063_projection_line_values",)
    assert _line_values(engine) == {
        "projection_observations": [None],
        "latest_player_projections": [None],
    }
    for table in _TABLES:
        column = next(
            c for c in inspect(engine).get_columns(table) if c["name"] == "line_value"
        )
        assert column["nullable"] is True

    assert run_migrations(engine).applied == ()


def test_upgrade_function_is_idempotent_when_the_column_already_exists(tmp_path):
    from app.migrations import _add_projection_line_values

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    ProjectionArchive(engine, StatisticCatalog.load_default()).ingest_complete_snapshot(
        _snapshot(StatisticCatalog.load_default()),
        query=NBAMarketQuery(season="2025-26"),
        accepted_at=OBSERVED_AT,
    )
    with engine.begin() as connection:
        _add_projection_line_values(connection)
        _add_projection_line_values(connection)

    assert _line_values(engine) == {
        "projection_observations": [27.5],
        "latest_player_projections": [27.5],
    }
