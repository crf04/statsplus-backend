"""The saved-Target Backtest precompute on PostgreSQL (#363).

The cross-account Target selection joins ``users.last_login`` with a
timezone-aware bound; this runs the SQLite scenario's core path against the
production dialect.  Skipped unless ``TEST_DATABASE_URL`` names a disposable
database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.models import Base
from tests.services import test_backtest_seasons as seasons
from tests.services import test_target_backtest_precompute as precompute

pytestmark = pytest.mark.integration


@pytest.fixture
def world():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    built = seasons.World(engine)
    built.publish_season(seasons.LAST, okc_games=2, corner_three=0.42)
    yield built
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    engine.dispose()


def test_after_a_generation_bump_recent_targets_are_hits_on_postgres(world):
    first, second = precompute._two_targets(world)
    precompute._stale_target(world)
    service = world.backtests(redis_client=precompute.CountingRedis())
    service.precompute(now=seasons.NOW)
    precompute._bump_generation(world)

    result = service.precompute(now=seasons.NOW)

    assert precompute._without_duration(result) == precompute._counts(computed=2)
    assert precompute._statuses(service) == ["ok", "ok"]
    assert service.backtest(seasons.OWNER, first["id"])[1] == "hit"
    assert service.backtest(seasons.OWNER, second["id"])[1] == "hit"
    assert precompute._statuses(service, precompute.STALE_OWNER) == ["uncached"]


def test_position_and_average_targets_precompute_on_postgres(world):
    precompute.exercise_position_and_average_targets(world)
