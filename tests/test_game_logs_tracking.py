"""Rebound chances and potential assists on ``/api/games/game_logs`` rows (crf04/statsplus#349).

Called as the web app and the MCP call it, over the real ``GameService``, the
real ``StoredGameLogsSource`` and a real composed ``player_game_logs``
publication; the tracking rows go in through the real repository.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.models.athlete_catalog import AthleteCatalog
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.player_tracking_repository import PlayerTrackingRepository
from tests.services import test_backtest_seasons as seasons

NEW = seasons.NEW
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=" + NEW
FETCHED = datetime(2026, 10, 27, 10, tzinfo=UTC)


def _stored(counts):
    return {
        "reb_chances": counts[0],
        "oreb_chances": counts[1],
        "dreb_chances": counts[2],
        "potential_ast": counts[3],
    }


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'tracking.sqlite3'}"))
    built.compose("player_game_logs", NEW, {"rows": seasons._season_logs(NEW, okc_games=0)})
    with built.engine.begin() as connection:
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
    tracking = PlayerTrackingRepository(built.engine)
    # Newest game (10-26) has no tracking yet; 10-25 is collected but the day
    # feed lacked him (potential assists null); 10-24 holds a real zero.
    for game_id, day, counts in (
        ("0022600000", date(2026, 10, 22), (9, 4, 5, 12)),
        ("0022600001", date(2026, 10, 23), (3, 1, 2, 8)),
        ("0022600002", date(2026, 10, 24), (0, 0, 0, 0)),
        ("0022600003", date(2026, 10, 25), (6, 2, 4, None)),
    ):
        tracking.replace_game(
            NEW,
            game_id,
            day,
            [
                {"player_id": seasons.bt.LEBRON, **_stored(counts)},
                {"player_id": 999, **_stored((1, 1, 0, 1))},
            ],
            FETCHED,
        )
    yield built, tracking
    built.engine.dispose()


@pytest.fixture
def served(dependencies, world):
    built, tracking = world
    settings = built.settings.model_copy(
        update={"cache": built.settings.cache.model_copy(update={"enabled": False})}
    )
    dependencies.game_service = GameService(
        built.engine,
        Mock(),
        settings=settings,
        game_logs_source=StoredGameLogsSource(built.logs, tracking=tracking),
        publication_reader=built.reader,
        game_log_seasons=built.logs,
        athlete_catalog=AthleteCatalogReader(built.engine),
    )


def _rows(response, *keys):
    assert response.status_code == 200
    return [(row["GAME_DATE"], *(row[key] for key in keys)) for row in response.get_json()["game_logs"]]


ALL = ("REB_CHANCES", "OREB_CHANCES", "DREB_CHANCES", "POTENTIAL_AST")


def test_every_row_carries_the_four_integer_or_null_keys(served, client):
    # Newest first; 10-26 was never collected.
    assert _rows(client.get(URL), *ALL) == [
        ("2026-10-26", None, None, None, None),
        ("2026-10-25", 6, 2, 4, None),
        ("2026-10-24", 0, 0, 0, 0),
        ("2026-10-23", 3, 1, 2, 8),
        ("2026-10-22", 9, 4, 5, 12),
    ]


def test_values_are_json_integers_not_floats(served, client):
    row = client.get(URL).get_json()["game_logs"][-1]

    assert row["REB_CHANCES"] == 9 and isinstance(row["REB_CHANCES"], int)
    assert row["POTENTIAL_AST"] == 12 and isinstance(row["POTENTIAL_AST"], int)


def test_a_potential_assists_range_keeps_only_games_inside_it(served, client):
    assert _rows(client.get(URL + "&self_filters[POTENTIAL_AST]=8,20"), "POTENTIAL_AST") == [
        ("2026-10-23", 8),
        ("2026-10-22", 12),
    ]


def test_a_rebound_chances_range_keeps_only_games_inside_it(served, client):
    assert _rows(client.get(URL + "&self_filters[REB_CHANCES]=3,6"), "REB_CHANCES") == [
        ("2026-10-25", 6),
        ("2026-10-23", 3),
    ]


def test_a_null_value_fails_any_range_and_a_real_zero_does_not(served, client):
    assert _rows(client.get(URL + "&self_filters[POTENTIAL_AST]=0,100"), "POTENTIAL_AST") == [
        ("2026-10-24", 0),
        ("2026-10-23", 8),
        ("2026-10-22", 12),
    ]


def test_without_a_tracking_store_every_row_serves_null_and_a_filter_matches_nothing(
    dependencies, world, client
):
    built, _tracking = world
    dependencies.game_service = GameService(
        built.engine,
        Mock(),
        settings=built.settings.model_copy(
            update={"cache": built.settings.cache.model_copy(update={"enabled": False})}
        ),
        game_logs_source=StoredGameLogsSource(built.logs),
        publication_reader=built.reader,
        game_log_seasons=built.logs,
        athlete_catalog=AthleteCatalogReader(built.engine),
    )

    assert {value for _date, value in _rows(client.get(URL), "POTENTIAL_AST")} == {None}
    assert _rows(client.get(URL + "&self_filters[POTENTIAL_AST]=0,100"), "POTENTIAL_AST") == []


def test_the_batched_read_joins_the_same_values(world):
    built, tracking = world
    source = StoredGameLogsSource(built.logs, tracking=tracking)

    frames = source.get_players_logs([seasons.bt.LEBRON, 12345], NEW)

    lebron = frames[seasons.bt.LEBRON].sort_values("GAME_DATE")
    assert lebron["REB_CHANCES"].tolist()[:4] == [9, 3, 0, 6]
    assert lebron["POTENTIAL_AST"].isna().tolist() == [False, False, False, True, True]
    assert frames[12345].empty
