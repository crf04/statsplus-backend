"""``FG2A`` and ``FG2M`` as Self Filters on ``/api/games/game_logs`` (statsplus#284).

The web app's filter panel and the MCP's ``query_game_logs`` send
``self_filters[FG2A]`` / ``self_filters[FG2M]``. The route is called as they
call it, over the real ``GameService``, the real ``StoredGameLogsSource`` and a
real composed ``player_game_logs`` publication; only the athlete catalog is
minimal. The publication stores field goals and threes, so each range must
filter on the derived two-point value (FGA - FG3A, FGM - FG3M), not on FGA/FGM.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.models.athlete_catalog import AthleteCatalog
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from tests.services import test_backtest_seasons as seasons

NEW = seasons.NEW
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=" + NEW

#: (FGM, FGA, FG3M, FG3A) per game, oldest first. FG2M/FG2A are 7/12, 2/5,
#: 8/16, 3/10 and 7/10, so an FGA or FGM range would keep other games.
SHOOTING_BY_GAME = [(10, 20, 3, 8), (6, 15, 4, 10), (9, 18, 1, 2), (8, 22, 5, 12), (7, 11, 0, 1)]


@pytest.fixture
def served(dependencies, tmp_path):
    world = seasons.World(create_engine(f"sqlite:///{tmp_path / 'two_pointers.sqlite3'}"))
    rows = seasons._season_logs(NEW, okc_games=0)
    for row, (fgm, fga, fg3m, fg3a) in zip(rows, SHOOTING_BY_GAME, strict=True):
        row.update(
            field_goals_made=fgm,
            field_goals_attempted=fga,
            three_pointers_made=fg3m,
            three_pointers_attempted=fg3a,
        )
    world.compose("player_game_logs", NEW, {"rows": rows})
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            {
                "season": NEW,
                "player_id": seasons.bt.LEBRON,
                "display_name": "LeBron James",
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "published_at": seasons.NOW,
            },
        )
    settings = world.settings.model_copy(
        update={"cache": world.settings.cache.model_copy(update={"enabled": False})}
    )
    dependencies.game_service = GameService(
        world.engine,
        Mock(),
        settings=settings,
        game_logs_source=StoredGameLogsSource(world.logs),
        publication_reader=world.reader,
        game_log_seasons=world.logs,
        athlete_catalog=AthleteCatalogReader(world.engine),
    )
    yield
    world.engine.dispose()


def _games(response, stat):
    assert response.status_code == 200, response.get_json()
    return [(row["GAME_DATE"], row[stat]) for row in response.get_json()["game_logs"]]


def test_an_fg2a_range_keeps_only_the_games_whose_two_point_attempts_are_inside_it(served, client):
    assert _games(client.get(URL + "&self_filters[FG2A]=10,12"), "FG2A") == [
        ("2026-10-26", 10),
        ("2026-10-25", 10),
        ("2026-10-22", 12),
    ]


def test_an_fg2m_range_keeps_only_the_games_whose_two_point_makes_are_inside_it(served, client):
    assert _games(client.get(URL + "&self_filters[FG2M]=7,8"), "FG2M") == [
        ("2026-10-26", 7),
        ("2026-10-24", 8),
        ("2026-10-22", 7),
    ]
