"""Backtest season reads on PostgreSQL (crf04/statsplus#104).

The retained-Publication selection is one statement joining the pointer
history, the stream registry, and the version, ordered per stream; these run
the SQLite scenarios' core paths against the production dialect.  Skipped
unless ``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.errors import SeasonUnavailableError
from app.models import Base
from tests.services import test_backtest_seasons as seasons

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


def test_last_season_reads_its_retained_generation_on_postgres(world):
    world.compose(
        "player_game_logs",
        seasons.LAST,
        {"rows": seasons._season_logs(seasons.LAST, okc_games=1)},
    )
    target = world.saved_target()
    seasons._activate_new_season(world)
    service = world.backtests(redis_client=seasons.bt.FakeRedis())

    first, first_state = service.backtest(seasons.OWNER, target["id"], season=seasons.LAST)
    world.revoke("player_game_logs", seasons.LAST)
    revoked, revoked_state = service.backtest(
        seasons.OWNER, target["id"], season=seasons.LAST
    )
    published, _ = service.backtest(seasons.OWNER, target["id"])

    assert (first["season"], first["season_reason"]) == (seasons.LAST, "requested")
    assert (first_state, revoked_state) == ("miss", "miss")
    assert first["games_considered"] == {"played": 1, "kept": 1}
    assert revoked["games_considered"] == {"played": 2, "kept": 2}
    assert (published["season"], published["season_reason"]) == (seasons.NEW, "published")


def test_a_pin_ahead_falls_back_and_an_unretained_stream_is_unavailable_on_postgres(world):
    target = world.saved_target()
    seasons._pin(world, seasons.NEW)
    service = world.backtests()

    body, _ = service.backtest(seasons.OWNER, target["id"])
    world.revoke("player_assist_locations", seasons.LAST)
    seasons._pin(world, seasons.LAST)
    seasons._activate_new_season(world)

    assert (body["season"], body["season_reason"]) == (seasons.LAST, "fallback_no_games")
    with pytest.raises(SeasonUnavailableError, match="player_assist_locations"):
        world.backtests().backtest(seasons.OWNER, target["id"])
