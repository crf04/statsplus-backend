"""Postgres check for ``offensive_possessions`` (crf04/statsplus#433): migration
085 on a deployed-shape database and the backfill's idempotent, checksum-neutral
write."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_offensive_possessions_ledger import (
    FILLED,
    LAL,
    SAS,
    _game,
    _null_possessions,
    _stored,
)

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


def test_migration_085_is_rerun_safe_and_leaves_stored_facts_null(engine):
    CanonicalGameLedgerRepository(engine).replace_game(_game())
    with engine.begin() as connection:
        for table in ("canonical_game_ledger_player_facts", "canonical_game_ledger_team_facts"):
            connection.execute(text(f"ALTER TABLE {table} DROP COLUMN offensive_possessions"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 85"))

    assert run_migrations(engine).applied == ("085_ledger_offensive_possessions",)
    assert run_migrations(engine).applied == ()

    for table in ("canonical_game_ledger_player_facts", "canonical_game_ledger_team_facts"):
        columns = {column["name"]: column for column in inspect(engine).get_columns(table)}
        assert columns["offensive_possessions"]["nullable"] is True
    assert _stored(engine)[0] == {LAL: None, SAS: None}


def test_backfill_fills_possessions_unchanged_checksums_and_a_rerun_writes_nothing(engine):
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    _null_possessions(engine)

    first = repository.backfill_offensive_possessions(season="2024-25")
    second = repository.backfill_offensive_possessions(season="2024-25")

    assert (first.updated, first.rejected, second.updated, second.unchanged) == (1, (), 0, 1)
    assert _stored(engine) == FILLED
    assert repository.get_game(game.game_id).checksum == game.checksum
