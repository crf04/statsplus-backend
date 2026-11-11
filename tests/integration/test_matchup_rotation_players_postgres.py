"""Rotation Players on PostgreSQL (crf04/statsplus#459).

The team's last-five-games read is a grouped, limited subquery inside ``IN``;
this runs the SQLite scenario against the production dialect.  Skipped unless
``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.models import Base
from tests.services.test_matchup_rotation_players import assert_rotation_scenario, world

pytestmark = pytest.mark.integration


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    built = create_engine(url)
    Base.metadata.drop_all(built)
    with built.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    yield built
    Base.metadata.drop_all(built)
    with built.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    built.dispose()


def test_a_scheduled_matchup_lists_each_rotation_player_after_the_pool_on_postgres(engine):
    assert_rotation_scenario(world(engine))
