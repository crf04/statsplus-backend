"""Postgres checks for on/off lineup segments (crf04/statsplus#410): migration
084, the signed 64-bit mask predicate, and parity through the real SQL."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import downgrade_lineup_segments, run_migrations
from scripts.on_off_parity import parity, within_tolerance

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

TABLES = {"lineup_roster_slots", "lineup_segment_games", "lineup_segment_players", "lineup_segment_seasons"}


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def test_migration_084_is_idempotent_and_reversible_on_postgres(engine):
    assert TABLES <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 84"))
    assert run_migrations(engine).applied == ("084_lineup_segments",)
    assert run_migrations(engine).applied == ()

    with engine.begin() as connection:
        downgrade_lineup_segments(connection)
    assert not TABLES & set(inspect(engine).get_table_names())
    assert run_migrations(engine).applied == ("084_lineup_segments",)


def test_slot_63_sets_the_sign_bit_and_the_mask_predicate_still_selects_on_postgres(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lineup_segment_players VALUES "
                "('0022500001', 1, 0, 7, '2025-26', :mask, 6000, 0,0,0,0,0,0,0,0, 0,0,0,0,0)"
            ),
            {"mask": -(1 << 63) | 1},
        )
        selected = connection.execute(
            text(
                "SELECT count(*) FROM lineup_segment_players "
                "WHERE (lineup_mask & CAST(:on AS bigint)) = CAST(:on AS bigint) AND (lineup_mask & 2) = 0"
            ),
            {"on": -(1 << 63)},
        ).scalar()
    assert selected == 1


def test_the_den_2026_27_splits_match_pbp_stats_through_postgres(engine):
    results = parity("den_2026_27", engine)

    assert sorted(results) == ["jokic_off", "jokic_on"]
    assert all(within_tolerance(result) for result in results.values())
