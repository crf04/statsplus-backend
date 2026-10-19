"""Migration 072: projection rows gain nullable Over/Under prices, never backfilled."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import _add_projection_side_prices, run_migrations
from app.providers.dfs import NBAMarketQuery, Selection
from app.services.projection_archive import ProjectionArchive
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_projection_archive import OBSERVED_AT, SEASON, _line_market, _line_snapshot

pytestmark = pytest.mark.real_migrations

_TABLES = ("projection_observations", "latest_player_projections")
_COLUMNS = ("over_price", "under_price")


def _ingest(engine) -> None:
    catalog = StatisticCatalog.load_default()
    market = _line_market(
        catalog,
        provider="novig",
        market_id="m",
        statistic_id="points",
        threshold="26.5",
        selections=(
            Selection(selection_id="o", direction="over", decimal_price=Decimal("1.941748")),
            Selection(selection_id="u", direction="under", decimal_price=Decimal("1.980198")),
        ),
    )
    ProjectionArchive(engine, catalog).ingest_complete_snapshot(
        _line_snapshot("novig", (market,)),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )


def _prices(engine) -> dict[str, list[tuple]]:
    with engine.connect() as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(text(f"SELECT over_price, under_price FROM {table}"))
            ]
            for table in _TABLES
        }


def test_existing_rows_keep_null_prices_and_rerun_is_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    _ingest(engine)
    assert _prices(engine) == {table: [(-106, -102)] for table in _TABLES}
    # The pre-072 shape: the same rows without the columns.
    with engine.begin() as connection:
        for table in _TABLES:
            for column in _COLUMNS:
                connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 72"))

    assert run_migrations(engine).applied == ("072_projection_side_prices",)

    assert _prices(engine) == {table: [(None, None)] for table in _TABLES}
    for table in _TABLES:
        columns = {c["name"]: c for c in inspect(engine).get_columns(table)}
        for column in _COLUMNS:
            assert columns[column]["nullable"] is True
    assert run_migrations(engine).applied == ()


def test_upgrade_function_is_idempotent_when_the_columns_already_exist(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    _ingest(engine)
    with engine.begin() as connection:
        _add_projection_side_prices(connection)
        _add_projection_side_prices(connection)

    assert _prices(engine) == {table: [(-106, -102)] for table in _TABLES}
