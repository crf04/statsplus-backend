"""``closing_line`` on ``/api/games/game_logs`` rows (#433).

The route is called as the web app and the MCP call it, over the real
``GameService`` and the real closing-line table; only the box-score source is
a stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert, text

from app.migrations import run_migrations
from app.models.closing_lines import ClosingLine
from app.services.closing_lines import ClosingLineReader
from app.services.game_service import GameService

WHEN = datetime(2025, 11, 5, 0, 24, 41, tzinfo=UTC)
LEBRON = 2544


def _frame():
    columns = [
        "GAME_DATE",
        "GAME_ID",
        "PLAYER_ID",
        "PLAYER_NAME",
        "TEAM_ID",
        "TEAM_ABBREVIATION",
        "MATCHUP",
        "MIN",
        "PTS",
        "REB",
        "AST",
        "PRA",
        "PA",
        "PR",
        "RA",
        "STKS",
        "FD_PTS",
        "FGM",
        "FGA",
        "FG_PCT",
        "FG2M",
        "FG2A",
        "FG3M",
        "FG3A",
        "FTM",
        "FTA",
        "OREB",
        "DREB",
        "TOV",
        "STL",
        "BLK",
        "PF",
    ]

    def row(day, game_id, points):
        return [
            day,
            game_id,
            LEBRON,
            "LeBron James",
            1610612747,
            "LAL",
            "LAL @ DEN",
            35,
            points,
            8,
            7,
            points + 15,
            points + 7,
            points + 8,
            15,
            2,
            44.2,
            9,
            16,
            56.3,
            6,
            9,
            3,
            7,
            4,
            5,
            1,
            7,
            2,
            1,
            1,
            2,
        ]

    return pd.DataFrame(
        [
            row("2025-11-04", "0022500101", 27),
            row("2025-11-06", "0022500104", 22),
            row("2025-11-08", "0022500109", 31),
        ],
        columns=columns,
    )


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(ClosingLine.__table__),
            [
                dict(
                    canonical_game_id=game,
                    canonical_player_id=player,
                    market_category=market,
                    book=book,
                    line=line,
                    snapshot_at=WHEN,
                    odds_event_id="e",
                    bought_at=WHEN,
                )
                for game, player, market, book, line in (
                    ("0022500101", LEBRON, "PTS", "draftkings", 24.5),
                    ("0022500101", LEBRON, "PTS", "fanduel", 25.5),
                    ("0022500104", LEBRON, "PTS", "betmgm", 23.5),
                    ("0022500109", LEBRON, "REB", "draftkings", 7.5),
                )
            ],
        )
    return engine


def _serve(dependencies, engine, *, reader):
    class Logs:
        def get_player_logs(self, player_id, season, **kwargs):
            assert player_id == LEBRON
            return _frame()

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            return [
                dict(
                    player_id=LEBRON,
                    display_name="LeBron James",
                    is_active_for_season=True,
                    team_id=1610612747,
                )
            ]

    dependencies.game_service = GameService(
        engine,
        redis_client=SimpleNamespace(),
        settings=dependencies.settings,
        athlete_catalog=Catalog(),
        game_logs_source=Logs(),
        closing_lines=reader,
    )


def test_each_row_carries_its_games_closing_points_line_or_null(dependencies, client, engine):
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [(row["GAME_DATE"], row["PTS"], row["closing_line"]) for row in rows] == [
        # The better book wins when two stored a line; this is the contract's
        # `{value, book}` object, the book being the Odds API bookmaker key.
        ("2025-11-04", 27, {"value": 24.5, "book": "draftkings"}),
        ("2025-11-06", 22, {"value": 23.5, "book": "betmgm"}),
        # Only a rebounds line is stored for this game: no points line.
        ("2025-11-08", 31, None),
    ]


def test_closing_line_is_null_on_every_row_when_no_lines_are_stored(dependencies, client, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    run_migrations(engine)
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert [row["closing_line"] for row in response.get_json()["game_logs"]] == [None, None, None]


def test_a_deployment_without_the_reader_still_serves_rows_with_a_null_line(dependencies, client, engine):
    _serve(dependencies, engine, reader=None)

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert response.status_code == 200
    assert [row["closing_line"] for row in response.get_json()["game_logs"]] == [None, None, None]


def test_a_failing_line_store_does_not_fail_the_game_logs(dependencies, client, engine):
    _serve(dependencies, engine, reader=ClosingLineReader(engine))
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE closing_lines"))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [row["PTS"] for row in rows] == [27, 22, 31]
    assert [row["closing_line"] for row in rows] == [None, None, None]


def test_the_line_follows_the_rows_a_filter_keeps(dependencies, client, engine):
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get(
        "/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26" "&self_filters[PTS]=25,40"
    )

    rows = response.get_json()["game_logs"]
    assert [(row["PTS"], row["closing_line"]) for row in rows] == [
        (27, {"value": 24.5, "book": "draftkings"}),
        (31, None),
    ]
