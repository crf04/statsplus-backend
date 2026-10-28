"""GET /api/games/board on Postgres (crf04/statsplus#373): the pool, the
injury report and the books of a two-game slate, one repeatable-read scope."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from tests.routes.test_games_board import (
    BRUNSON_PTS,
    CURRY_PTS,
    LEBRON_PTS,
    SEASON,
    SOURCES,
    TATUM_PTS,
    _games,
    _get,
    seed_board,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def board(dependencies):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield seed_board(engine, dependencies)
    engine.dispose()


def test_the_pts_board_and_its_index_on_postgres(board, client):
    assert _get(client, "date=2026-01-15&market=PTS") == (
        200,
        {
            "date": "2026-01-15",
            "season": SEASON,
            "games": _games([CURRY_PTS, LEBRON_PTS], [TATUM_PTS, BRUNSON_PTS]),
            "sources": SOURCES,
            "players_total": 4,
        },
    )
    status, index = _get(client, "date=2026-01-15")
    assert status == 200
    assert index["markets"] == [
        {"market": "PTS", "players": 4, "sources": ["dabble", "prizepicks", "draftkings", "fanduel"]},
        {"market": "3PM", "players": 1, "sources": ["draftkings"]},
        {"market": "FG3A", "players": 2, "sources": ["dabble"]},
    ]
