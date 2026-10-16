"""Postgres check of the PrizePicks feed (crf04/statsplus#215): one beat writes
Partial snapshots into the projection archive, the reader serves the lines for
the registry's 90 minutes, and the started game is closed -- over the same
migrated schema production runs."""

from __future__ import annotations

from datetime import timedelta
import os

import pandas as pd
import pytest
from sqlalchemy import create_engine, select, text

from app.models.projection_archive import ClosingProjectionSet, ProviderPoll
from app.models.sportsbook_odds import OddsApiPoll
from tests.support.odds_world import (
    GAME_ID,
    LAL,
    LEBRON,
    NOW,
    SEASON,
    TIP,
    FakeClient,
    build_world,
    event_row,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def world(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    with create_engine(url).begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    world = build_world(tmp_path, database_url=url)
    yield world
    world.engine.dispose()


def _rows(world, model):
    with world.engine.connect() as connection:
        return connection.execute(select(model.__table__)).mappings().all()


def test_a_beat_serves_prizepicks_lines_for_ninety_minutes_then_closes_the_game(world):
    coordinator = world.dfs_coordinator(FakeClient(regions="us_dfs"))

    result = coordinator.run()

    assert (result.status, result.calls, result.credits) == ("complete", 1, 5)
    (poll,) = _rows(world, ProviderPoll)
    assert (poll["provider"], poll["outcome"], poll["promoted"], poll["observation_count"]) == (
        "prizepicks", "partial", True, 10,
    )
    (beat,) = _rows(world, OddsApiPoll)
    assert (beat["outcome"], beat["quote_count"], beat["cost"]) == ("complete", 10, 5)

    reader = world.pool_reader()
    world.clock[0] = NOW + timedelta(minutes=90)
    pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
    lebron = {p.canonical_player_id: p for p in pool.players}[LEBRON]
    assert dict(lebron.lines["prizepicks"]) == {
        "PTS": 27.5, "REB": 7.5, "AST": 7.5, "3PM": 2.5, "PRA": 42.5,
    }
    world.clock[0] = NOW + timedelta(minutes=91)
    assert reader.get_pool_for_game(season=SEASON, game_id=GAME_ID).players == ()

    started = event_row(GAME_ID, 1610612743, "Nuggets", "DEN", LAL, "Lakers", "LAL", TIP)
    started.update(status_text="Q1 11:42", status_code=2)
    world.catalog_service.repository.publish(
        SEASON, pd.DataFrame([started]), TIP + timedelta(minutes=2)
    )
    world.clock[0] = TIP + timedelta(minutes=3)
    coordinator.run()
    (closing,) = _rows(world, ClosingProjectionSet)
    assert (closing["provider"], closing["canonical_game_id"]) == ("prizepicks", GAME_ID)
    pool, is_closing = reader.get_pool_for_game_with_closing_state(season=SEASON, game_id=GAME_ID)
    assert is_closing is True
    assert dict({p.canonical_player_id: p for p in pool.players}[LEBRON].lines["prizepicks"])["PTS"] == 27.5
