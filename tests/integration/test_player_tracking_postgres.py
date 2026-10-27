"""Postgres checks for the player-tracking store (crf04/statsplus-backend#604):
migration 078, whole-game replace and the batched read."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from app.services.player_tracking_repository import PlayerTrackingRepository

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

SEASON = "2025-26"
FETCHED = datetime(2026, 4, 13, 10, tzinfo=UTC)
LATER = datetime(2026, 4, 14, 10, tzinfo=UTC)


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


def _rows():
    return [
        {"player_id": 1642266, "reb_chances": 2, "oreb_chances": 0, "dreb_chances": 2, "potential_ast": 2},
        {"player_id": 1629651, "reb_chances": 0, "oreb_chances": 0, "dreb_chances": 0, "potential_ast": None},
    ]


def test_migration_078_creates_the_nullable_table_and_reruns_cleanly(engine):
    columns = {column["name"]: column for column in inspect(engine).get_columns("player_game_tracking")}

    assert {name for name in columns if columns[name]["nullable"]} == {
        "reb_chances",
        "oreb_chances",
        "dreb_chances",
        "potential_ast",
    }
    assert run_migrations(engine).applied == ()


def test_replacing_a_game_twice_leaves_identical_rows_and_keeps_the_first_fetch_time(engine):
    repository = PlayerTrackingRepository(engine)
    assert repository.replace_game(SEASON, "0022501192", date(2026, 4, 12), _rows(), FETCHED) is True

    def stored():
        with engine.connect() as connection:
            return connection.execute(text("SELECT * FROM player_game_tracking ORDER BY player_id")).all()

    first = stored()

    assert repository.replace_game(SEASON, "0022501192", date(2026, 4, 12), _rows(), LATER) is False
    assert stored() == first
    assert repository.for_player(SEASON, 1642266) == {
        "0022501192": {"reb_chances": 2, "oreb_chances": 0, "dreb_chances": 2, "potential_ast": 2}
    }
    assert repository.for_player(SEASON, 1629651)["0022501192"]["potential_ast"] is None
    assert repository.stored_game_ids(SEASON) == {"0022501192"}


def test_a_changed_value_replaces_the_game_whole(engine):
    repository = PlayerTrackingRepository(engine)
    repository.replace_game(SEASON, "0022501192", date(2026, 4, 12), _rows(), FETCHED)
    changed = [{**_rows()[0], "potential_ast": 3}]

    assert repository.replace_game(SEASON, "0022501192", date(2026, 4, 12), changed, LATER) is True

    assert repository.for_players(SEASON, [1642266, 1629651]) == {
        1642266: {"0022501192": {"reb_chances": 2, "oreb_chances": 0, "dreb_chances": 2, "potential_ast": 3}}
    }
