"""The ledger's period-clock read on PostgreSQL.

``CanonicalGameLedgerRepository.get_games`` reads each game's period clock
from its source observation with PostgreSQL's JSON operators, which SQLite
does not exercise.  Skipped unless ``TEST_DATABASE_URL`` names a disposable
database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.models import Base
from tests.services.test_canonical_game_ledger import assert_a_loaded_game_carries_its_recorded_period_clock

pytestmark = pytest.mark.integration


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    run_migrations(engine)
    yield engine
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    engine.dispose()


def test_a_loaded_game_carries_its_recorded_period_clock_on_postgres(engine):
    assert_a_loaded_game_carries_its_recorded_period_clock(engine)
