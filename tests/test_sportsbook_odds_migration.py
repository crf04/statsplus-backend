"""Migration 063: the sportsbook odds store is idempotent and reversible (#377)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import downgrade_sportsbook_odds, run_migrations

pytestmark = pytest.mark.real_migrations

TABLES = {
    "odds_api_polls",
    "odds_api_quota_state",
    "sportsbook_prop_latest",
    "sportsbook_prop_quotes",
}


def _tables(engine):
    return TABLES & set(inspect(engine).get_table_names())


def test_upgrade_creates_the_four_tables_and_a_rerun_applies_nothing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.current_version == 77 and "063_sportsbook_odds" in first.applied
    assert second.applied == ()
    assert _tables(engine) == TABLES
    columns = {c["name"] for c in inspect(engine).get_columns("sportsbook_prop_latest")}
    assert columns == {
        "canonical_game_id",
        "canonical_player_id",
        "market_category",
        "book",
        "poll_id",
        "line",
        "over_price",
        "under_price",
        "market_last_update",
        "observed_at",
        "is_closing",
    }
    primary_key = inspect(engine).get_pk_constraint("sportsbook_prop_latest")["constrained_columns"]
    assert primary_key == ["canonical_game_id", "canonical_player_id", "market_category", "book"]


def test_upgrade_is_idempotent_when_the_tables_already_exist(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 63"))

    again = run_migrations(engine)

    assert again.applied == ("063_sportsbook_odds",)
    assert _tables(engine) == TABLES


def test_downgrade_drops_only_its_tables_and_upgrade_restores_them(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with engine.begin() as connection:
        downgrade_sportsbook_odds(connection)
    with engine.begin() as connection:
        downgrade_sportsbook_odds(connection)  # reversing twice is harmless

    assert _tables(engine) == set()
    assert "projection_collection_leases" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    assert 63 not in versions and {62, 64} <= versions
    restored = run_migrations(engine)
    assert restored.applied == ("063_sportsbook_odds",)
    assert _tables(engine) == TABLES
