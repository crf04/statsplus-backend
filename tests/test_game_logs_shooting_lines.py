"""Dabble FTA, FTM, FGM and FG2M lines on ``/api/games/game_logs`` (crf04/statsplus#370).

The route is called as the web app calls it, over the real ``GameService``,
the real ``StoredGameLogsSource``, the real ``PostedLinesReader`` and a real
composed ``player_game_logs`` publication; the event catalog and the player
pool are stubs. Hits are counted as the Game Logs default counts them, from
the posted line and each game's value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.models.athlete_catalog import AthleteCatalog
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.game_log_preview import LineSource, LineType, preview_facts
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.posted_lines import PostedLinesReader
from tests.services import test_backtest_seasons as seasons
from tests.test_game_logs_posted_line import Catalog, Pools, _event

NEW = seasons.NEW
LAL = 1610612747
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=" + NEW + "&line_date=2026-10-27"
TONIGHT = _event("0022600099", "2026-10-27T23:30:00+00:00", "LAL", "GSW")
#: (FGM, FG3M, FTM, FTA) per game, oldest first. FG2M is 7, 2, 8, 3 and 7.
BOX_BY_GAME = [(10, 3, 4, 6), (6, 4, 1, 2), (9, 1, 6, 7), (8, 5, 2, 2), (7, 0, 5, 8)]
LINES = {"dabble": {"FTA": 5.5, "FTM": 3.5, "FGM": 8.5, "FG2M": 6.5}}


@pytest.fixture
def served(dependencies, tmp_path):
    world = seasons.World(create_engine(f"sqlite:///{tmp_path / 'shooting_lines.sqlite3'}"))
    rows = seasons._season_logs(NEW, okc_games=0)
    for row, (fgm, fg3m, ftm, fta) in zip(rows, BOX_BY_GAME, strict=True):
        row.update(
            field_goals_made=fgm,
            field_goals_attempted=20,
            three_pointers_made=fg3m,
            three_pointers_attempted=8,
            free_throws_made=ftm,
            free_throws_attempted=fta,
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
                "team_id": LAL,
                "published_at": seasons.NOW,
            },
        )
    settings = world.settings.model_copy(
        update={"cache": world.settings.cache.model_copy(update={"enabled": False})}
    )
    athletes = AthleteCatalogReader(world.engine)
    dependencies.game_service = GameService(
        world.engine,
        Mock(),
        settings=settings,
        game_logs_source=StoredGameLogsSource(world.logs),
        publication_reader=world.reader,
        game_log_seasons=world.logs,
        athlete_catalog=athletes,
        posted_lines=PostedLinesReader(
            event_catalog=Catalog(TONIGHT),
            player_pool=Pools({TONIGHT["nba_game_id"]: LINES}, player_id=seasons.bt.LEBRON, team_id=LAL),
            settings=settings,
            athlete_catalog=athletes,
            clock=lambda: datetime(2026, 10, 27, 16, tzinfo=UTC),
        ),
    )
    yield
    world.engine.dispose()


def test_the_posted_line_carries_dabbles_shooting_lines(served, client):
    response = client.get(URL)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["posted_line"]["lines"] == {
        "FTA": {"value": 5.5, "provider": "dabble"},
        "FTM": {"value": 3.5, "provider": "dabble"},
        "FGM": {"value": 8.5, "provider": "dabble"},
        "FG2M": {"value": 6.5, "provider": "dabble"},
    }


@pytest.mark.parametrize(
    ("line_type", "values_newest_first", "hits"),
    [
        ("FTA", [8, 2, 7, 2, 6], 3),
        ("FTM", [5, 2, 6, 1, 4], 3),
        ("FGM", [7, 8, 9, 6, 10], 2),
        ("FG2M", [7, 3, 8, 2, 7], 3),
    ],
)
def test_each_line_type_defaults_to_the_posted_line_and_counts_hits_on_its_own_values(
    served, client, line_type, values_newest_first, hits
):
    payload = client.get(URL).get_json()

    facts = preview_facts(payload, LineType(line_type))

    assert [row[line_type] for row in payload["game_logs"]] == values_newest_first
    assert (facts.source, facts.provider, str(facts.line), facts.hits, facts.games) == (
        LineSource.POSTED,
        "dabble",
        str(LINES["dabble"][line_type]),
        hits,
        5,
    )
