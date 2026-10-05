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
    default, _ = service.backtest(seasons.OWNER, target["id"])
    ahead, _ = service.backtest(seasons.OWNER, target["id"], season=seasons.NEW)

    assert (first["season"], first["season_reason"]) == (seasons.LAST, "requested")
    assert (first_state, revoked_state) == ("miss", "miss")
    assert first["games_considered"] == {"played": 1, "kept": 1}
    assert revoked["games_considered"] == {"played": 2, "kept": 2}
    assert (default["season"], default["season_reason"]) == (seasons.LAST, "default")
    assert default["games_considered"] == {"played": 2, "kept": 2}
    assert (ahead["season"], ahead["season_reason"]) == (seasons.NEW, "requested")


def test_a_pin_ahead_keeps_the_default_and_an_unretained_stream_is_unavailable_on_postgres(
    world,
):
    target = world.saved_target()
    seasons._pin(world, seasons.NEW)
    service = world.backtests()

    body, _ = service.backtest(seasons.OWNER, target["id"])
    world.revoke("player_assist_locations", seasons.LAST)
    seasons._pin(world, seasons.LAST)
    seasons._activate_new_season(world)

    assert (body["season"], body["season_reason"]) == (seasons.LAST, "default")
    with pytest.raises(SeasonUnavailableError, match="player_assist_locations"):
        world.backtests().backtest(seasons.OWNER, target["id"])


@pytest.mark.parametrize("route", ["single", "batch"])
def test_a_revocation_after_the_check_never_serves_an_unchecked_entry_on_postgres(
    client, authenticate, world, dependencies, monkeypatch, route
):
    """2025-26 holds A (two games), then B (one game); A loses its projection.

    B's revocation commits on another connection after the request checked B
    and before it looks the cache up: READ COMMITTED would show a second
    history lookup A, whose warm two-game entry this request never checked.
    """

    target = world.saved_target()
    seasons._pin(world, seasons.NEW)
    service = world.backtests(redis_client=seasons.bt.FakeRedis())
    service.backtest(seasons.OWNER, target["id"], season=seasons.LAST)
    world.compose(
        "player_game_logs",
        seasons.LAST,
        {"rows": seasons._season_logs(seasons.LAST, okc_games=1)},
    )
    seasons._activate_new_season(world)
    service.backtest(seasons.OWNER, target["id"], season=seasons.LAST)
    with world.engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM publication_player_game_logs WHERE publication_id = "
            "(SELECT publication_id FROM publication_pointer_history "
            "WHERE stream_key = 'player_game_logs' AND season = :season "
            "ORDER BY fence ASC LIMIT 1)"
        ), {"season": seasons.LAST})
    check = service.seasons.require_streams

    def revoke_after_the_check(*args, **kwargs):
        checked = check(*args, **kwargs)
        monkeypatch.setattr(service.seasons, "require_streams", check)
        world.revoke("player_game_logs", seasons.LAST)
        return checked

    monkeypatch.setattr(service.seasons, "require_streams", revoke_after_the_check)
    dependencies.user_service = world.users
    dependencies.target_backtest_service = service
    url = (
        f"/api/user/targets/{target['id']}/backtest"
        if route == "single"
        else "/api/user/targets/backtests"
    )

    raced = client.get(url, query_string={"season": seasons.LAST}, headers=authenticate())
    after = client.get(url, query_string={"season": seasons.LAST}, headers=authenticate())

    body = raced.get_json()
    backtest = body if route == "single" else body["backtests"][0]["backtest"]
    assert raced.status_code == 200
    assert backtest["games_considered"] == {"played": 1, "kept": 1}
    assert after.status_code == 503
    assert after.get_json()["error"]["details"] == {
        "season": seasons.LAST,
        "published_season": seasons.NEW,
        "stream": "player_game_logs",
    }
