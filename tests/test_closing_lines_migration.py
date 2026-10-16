"""Migration 067: the closing-line store and its purchase ledger (#433)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import downgrade_closing_lines, run_migrations

pytestmark = pytest.mark.real_migrations

TABLES = {"closing_lines", "closing_line_purchases"}


def _tables(engine):
    return TABLES & set(inspect(engine).get_table_names())


def test_upgrade_creates_both_tables_keyed_by_game_player_market_and_book(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert "067_closing_lines" in first.applied and first.current_version == 67
    assert second.applied == ()
    assert _tables(engine) == TABLES
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("closing_lines")} == {
        "canonical_game_id", "canonical_player_id", "market_category", "book",
        "line", "snapshot_at", "odds_event_id", "bought_at",
    }
    assert inspector.get_pk_constraint("closing_lines")["constrained_columns"] == [
        "canonical_game_id", "canonical_player_id", "market_category", "book",
    ]
    assert inspector.get_pk_constraint("closing_line_purchases")["constrained_columns"] == [
        "canonical_game_id", "market_category",
    ]


def test_the_ledger_refuses_an_outcome_it_does_not_know(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with pytest.raises(Exception, match="ck_closing_line_purchases_outcome|CHECK"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO closing_line_purchases (canonical_game_id, market_category, "
                    "odds_event_id, requested_for, outcome, bought_at, unresolved_count) "
                    "VALUES ('g', 'PTS', 'e', '2025-11-05', 'bought', '2025-11-05', 0)"
                )
            )


def test_upgrade_is_idempotent_when_the_tables_already_exist(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 67"))

    again = run_migrations(engine)

    assert again.applied == ("067_closing_lines",)
    assert _tables(engine) == TABLES


def test_downgrade_drops_only_its_tables_and_upgrade_restores_them(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with engine.begin() as connection:
        downgrade_closing_lines(connection)
    with engine.begin() as connection:
        downgrade_closing_lines(connection)  # reversing twice is harmless

    assert _tables(engine) == set()
    assert "sportsbook_prop_latest" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    assert 67 not in versions and {65, 64} <= versions
    assert run_migrations(engine).applied == ("067_closing_lines",)
    assert _tables(engine) == TABLES
