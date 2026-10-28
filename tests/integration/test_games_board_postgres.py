"""GET /api/games/board on Postgres (crf04/statsplus#373): the pool, the
injury report and the books of a two-game slate, one repeatable-read scope."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from tests.routes.test_games_board import (
    CURRY_PTS,
    HAUSER,
    LATER_PTS,
    LEBRON_PTS,
    SEASON,
    SOURCES,
    TATUM,
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
            "games": _games([CURRY_PTS, LEBRON_PTS], LATER_PTS),
            "sources": SOURCES,
            "players_total": 8,
        },
    )
    status, index = _get(client, "date=2026-01-15")
    assert status == 200
    assert index["markets"] == [
        {"market": "PTS", "players": 8, "sources": ["dabble", "prizepicks", "draftkings", "fanduel"]},
        {"market": "3PM", "players": 1, "sources": ["draftkings"]},
        {"market": "FG3A", "players": 2, "sources": ["dabble"]},
    ]


def _tatum(body):
    (row,) = [row for game in body["games"] for row in game["players"] if row["name"] == "Jayson Tatum"]
    books = {book["book"]: book["line"] for book in row["book_lines"]["books"]}
    return row["lines"]["dabble"], books, row["injury_status"]


def _hauser(body):
    return [row["name"] for game in body["games"] for row in game["players"] if row["player_id"] == HAUSER[0]]


def _correct_tatum(engine):
    """Dabble and DraftKings move Tatum from 27.5 to 30.5, the report rules him Out and the
    catalog renames Hauser, in one commit."""

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE latest_player_projections SET line_value = 30.5 "
                "WHERE provider = 'dabble' AND canonical_player_id = :player AND market_category = 'PTS'"
            ),
            {"player": TATUM[0]},
        )
        connection.execute(
            text(
                "UPDATE sportsbook_prop_latest SET line = 30.5 "
                "WHERE book = 'draftkings' AND canonical_player_id = :player AND market_category = 'PTS'"
            ),
            {"player": TATUM[0]},
        )
        connection.execute(
            text(
                "UPDATE injury_snapshots "
                "SET normalized_entries = REPLACE(normalized_entries, '\"Questionable\"', '\"Out\"')"
            )
        )
        connection.execute(
            text("UPDATE athlete_catalog SET display_name = 'Samuel Hauser' WHERE player_id = :player"),
            {"player": HAUSER[0]},
        )


@pytest.mark.parametrize(
    ("owner", "method", "commits_first"),
    [
        # After the scope's first read, before the pool is read.
        ("event_catalog", "get_events_between", False),
        # After the pool is read, before the injury report and the books.
        ("injuries", "get_stored_injuries_many", True),
    ],
)
def test_a_correction_committed_mid_read_never_mixes_into_the_board(
    board, client, monkeypatch, owner, method, commits_first
):
    reads = getattr(board.service, owner)
    read = getattr(reads, method)

    def read_beside_a_correction(*arguments, connection=None, **keywords):
        if commits_first:
            _correct_tatum(board.engine)
        result = read(*arguments, connection=connection, **keywords)
        if not commits_first:
            _correct_tatum(board.engine)
        return result

    monkeypatch.setattr(reads, method, read_beside_a_correction)
    status, during = _get(client, "date=2026-01-15&market=PTS")
    monkeypatch.setattr(reads, method, read)
    _status, after = _get(client, "date=2026-01-15&market=PTS")

    assert status == 200
    assert _tatum(during) == (27.5, {"draftkings": 27.5, "fanduel": 28.5}, "Questionable")
    assert _tatum(after) == (30.5, {"draftkings": 30.5, "fanduel": 28.5}, "Out")
    assert (_hauser(during), _hauser(after)) == (["Sam Hauser"], ["Samuel Hauser"])
