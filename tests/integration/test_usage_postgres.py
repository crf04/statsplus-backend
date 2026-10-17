"""Postgres check for ``usage_pct`` (crf04/statsplus-backend#450): migration 071
on a deployed-shape database and the backfill's idempotent, checksum-neutral write."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_usage_ledger import _game, _null_usage, _stored

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


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


def test_migration_071_is_rerun_safe_and_leaves_stored_facts_null(engine):
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_game(_game())
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE canonical_game_ledger_player_facts DROP COLUMN usage_pct"
        ))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 71"))

    assert run_migrations(engine).applied == ("071_ledger_player_usage_pct",)
    assert run_migrations(engine).applied == ()

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("canonical_game_ledger_player_facts")
    }
    assert columns["usage_pct"]["nullable"] is True
    assert set(_stored(engine).values()) == {None}


def test_backfill_fills_usage_unchanged_checksums_and_a_rerun_writes_nothing(engine):
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    _null_usage(engine)

    first = repository.backfill_usage(season="2024-25")
    second = repository.backfill_usage(season="2024-25")

    assert (first.updated, first.rejected, second.updated, second.unchanged) == (1, (), 0, 1)
    assert _stored(engine) == {101: 28.5714, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}
    assert repository.get_game(game.game_id).checksum == game.checksum
