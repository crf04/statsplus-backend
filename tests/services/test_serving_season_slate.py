"""The Slate across an opening-week Season Cutover (crf04/statsplus-backend#552).

``GET /api/games/slate`` is served by the application's own dependency graph
over the cutover world of ``test_serving_season_matchup``, with one 2026-27
game stored in its Event Catalog, so the Slate's ``trailing`` comes from the
same Publication reader the Matchups use.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pandas as pd
import pytest

from app import create_app
from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.dependencies import build_dependencies
from app.services.event_catalog_repository import EventCatalogRepository
from tests.services.test_serving_season_matchup import (
    ACTOR,
    GAME_ID,
    GAME_TIP,
    NEW,
    NOW,
    SLATE_DATE,
    TRAILING_DIETS,
    TRAILING_TEAM_DEFENSE,
    World,
    _game_event,
)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    event = {**_game_event(), "scheduled_at": datetime.fromisoformat(GAME_TIP)}
    EventCatalogRepository(world.engine).publish(NEW, pd.DataFrame([event]), NOW)
    return world


@pytest.fixture
def slate(world, tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=world.engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    settings = RuntimeSettings(
        environment="testing",
        database={"url": f"sqlite:///{tmp_path / 'cutover.sqlite3'}"},
        # No NBA_CURRENT_SEASON pin: the game-log pointer decides.
        nba=NBASeasonSettings.model_construct(_fields_set=set(), current_season=NEW),
    )
    client = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": build_dependencies(settings),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    ).test_client()

    def read(date=SLATE_DATE):
        response = client.get(f"/api/games/slate?date={date}")
        assert response.status_code == 200
        body = response.get_json()
        assert [game["game_id"] for game in body["games"]] == ([GAME_ID] if date == SLATE_DATE else [])
        return body["trailing"]

    return read


def test_the_slate_lists_the_families_trailing_after_an_opening_week_cutover(world, slate):
    assert slate() == []

    cutover = world.opening_week_cutover()

    assert slate() == [TRAILING_DIETS, TRAILING_TEAM_DEFENSE]
    # A day with no game has no Matchup to read a trailing family.
    assert slate("2026-10-27") == []

    world.operations.revert_season_cutover(
        NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early"
    )

    assert slate() == []
