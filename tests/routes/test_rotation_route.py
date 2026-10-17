"""``GET /api/games/rotation`` over the Flask test client (crf04/statsplus#223).

The real service reads a migrated SQLite database seeded with a real recorded
game (the overtime fixture) beside two synthetic DET games.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import event

from app.services.rotation_derivation import derive_rotation
from tests.support.rotation_world import (
    Catalog,
    game,
    line,
    migrated_repository,
    service_for,
    store,
)

ROTATION = "/api/games/rotation"
FIXTURES = Path("tests/fixtures/nba_live_data")
MAXEY = 1630178
SAME_NAME_AS_MAXEY = 999


@pytest.fixture
def seeded(tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    # SQLite returns unordered selects in insertion order, hiding a missing
    # ORDER BY that PostgreSQL would not honour; make it return them reversed.
    event.listen(
        repository.engine, "connect",
        lambda connection, _: connection.execute("PRAGMA reverse_unordered_selects=ON"),
    )
    repository.engine.dispose()
    recorded = derive_rotation(
        json.loads((FIXTURES / "boxscore_overtime.json").read_text()),
        json.loads((FIXTURES / "playbyplay_overtime.json").read_text()),
    )
    store(
        repository,
        recorded,
        game("0022500900", "2026-01-02", "DET", "BOS", [
            line(1, "Unit One", "DET", minutes=30, started=True, closed=True),
            line(2, "Unit Two", "DET", minutes=10),
        ]),
        game("0022500901", "2026-01-04", "BOS", "DET", [
            line(1, "Unit One", "DET", minutes=20, started=True),
            line(2, "Unit Two", "DET", minutes=20, started=True),
        ]),
    )
    dependencies.rotation_service = service_for(
        repository,
        Catalog(
            (MAXEY, "Tyrese Maxey", "PHI"),
            (1, "Unit One", "DET"),
            (2, "Unit Two", "DET"),
            (SAME_NAME_AS_MAXEY, "Tyrese Maxey", "BOS"),
        ),
    )
    return dependencies


def test_player_rotation_serves_a_recorded_overtime_game(client, seeded):
    response = client.get(f"{ROTATION}?player_name=Tyrese%20Maxey&player_team=phi&last_n=1")

    assert response.status_code == 200
    body = response.get_json()
    assert body["player"] == {"name": "Tyrese Maxey", "team": "PHI"}
    assert body["recent"] == {
        "games": 1, "played": 1, "starts": 1, "minutes": 47.4, "stints": 3.0,
        "closed": 1,
        "minutes_by_period": {"Q1": 10.3, "Q2": 12.0, "Q3": 8.2, "Q4": 12.0, "OT": 5.0},
    }
    assert body["games"][0]["matchup"] == "PHI @ WAS"
    assert body["games"][0]["game_date"] == "2025-10-28"
    assert body["games"][0]["stints"] == [
        {"in": {"period": 1, "clock": "12:00"}, "out": {"period": 1, "clock": "3:14"}, "minutes": 8.8},
        {"in": {"period": 1, "clock": "1:29"}, "out": {"period": 3, "clock": "3:49"}, "minutes": 21.7},
        {"in": {"period": 4, "clock": "12:00"}, "out": {"period": 5, "clock": "0:00"}, "minutes": 17.0},
    ]


def test_team_rotation_lists_starters_and_minute_changes(client, seeded):
    response = client.get(f"{ROTATION}?team=det&last_n=1")

    assert response.status_code == 200
    assert response.get_json() == {
        "season": "2025-26",
        "last_n": 1,
        "team": "DET",
        "games": [{"game_date": "2026-01-04", "matchup": "DET @ BOS",
                   "starters": ["Unit One", "Unit Two"]}],
        "players": [
            {"name": "Unit One",
             "recent": {"games": 1, "played": 1, "starts": 1, "minutes": 20.0, "closed": 0},
             "season_summary": {"games": 2, "played": 2, "starts": 2, "minutes": 25.0, "closed": 1},
             "minutes_change": -5.0},
            {"name": "Unit Two",
             "recent": {"games": 1, "played": 1, "starts": 1, "minutes": 20.0, "closed": 0},
             "season_summary": {"games": 2, "played": 2, "starts": 1, "minutes": 15.0, "closed": 0},
             "minutes_change": 5.0},
        ],
    }


def test_last_n_defaults_to_five(client, seeded):
    body = client.get(f"{ROTATION}?team=DET").get_json()

    assert body["last_n"] == 5
    assert len(body["games"]) == 2


def test_a_namesake_without_rotation_data_does_not_make_a_name_ambiguous(client, seeded):
    response = client.get(f"{ROTATION}?player_name=Tyrese%20Maxey")

    # Only the PHI namesake has stored rotation data.
    assert response.status_code == 200
    assert response.get_json()["player"]["team"] == "PHI"


def test_a_namesake_with_data_makes_the_name_ambiguous(client, seeded):
    repository = seeded.rotation_service.repository
    store(repository, game("0022500902", "2026-01-06", "BOS", "LAL", [
        line(SAME_NAME_AS_MAXEY, "Tyrese Maxey", "BOS", minutes=5),
    ]))

    response = client.get(f"{ROTATION}?player_name=Tyrese%20Maxey")

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "invalid_input",
        "message": "The player_name matches more than one player; narrow it with player_team.",
        "details": {
            "parameter": "player_name",
            "parameters": ["player_name"],
            "candidates": [
                {"name": "Tyrese Maxey", "team": "BOS"},
                {"name": "Tyrese Maxey", "team": "PHI"},
            ],
        },
    }


@pytest.mark.parametrize("query, parameters", [
    ("", ["player_name", "team", "game_id"]),
    ("player_name=Unit%20One&team=DET", ["player_name", "team"]),
    ("team=DET&player_team=DET", ["player_team"]),
    ("team=DET&date=2026-01-01", ["date"]),
    ("team=DET&team=BOS", ["team"]),
    ("team=DETROIT", ["team"]),
    ("player_name=%20", ["player_name"]),
    ("player_name=Unit%20One&player_team=D3T", ["player_team"]),
    ("team=DET&last_n=0", ["last_n"]),
    ("team=DET&last_n=21", ["last_n"]),
    ("team=DET&last_n=five", ["last_n"]),
    ("team=DET&last_n=-1", ["last_n"]),
    ("team=DET&season=2025", ["season"]),
    ("team=DET&season=2025-27", ["season"]),
    ("game_id=0022500777&team=DET", ["team", "game_id"]),
    ("game_id=0022500777&player_name=Bench%20Bo", ["player_name", "game_id"]),
    ("game_id=0022500777&last_n=5", ["last_n"]),
    ("game_id=0022500777&season=2025-26", ["season"]),
    ("game_id=0022500777&last_n=5&season=2025-26", ["last_n", "season"]),
    ("game_id=0022500777&player_team=DET", ["player_team"]),
    ("game_id=22500777", ["game_id"]),
    ("game_id=00225007770", ["game_id"]),
    ("game_id=002250077x", ["game_id"]),
    ("game_id=", ["game_id"]),
    ("game_id=0022500777&game_id=0022500778", ["game_id"]),
])
def test_invalid_requests_are_invalid_input_naming_the_parameters(client, seeded, query, parameters):
    response = client.get(f"{ROTATION}?{query}")

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    assert error["details"] == {"parameters": parameters}


def test_an_unknown_tricode_is_invalid_input(client, seeded):
    response = client.get(f"{ROTATION}?team=ZZZ")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("query, message", [
    ("player_name=Nobody%20Real", "The requested player was not found."),
    ("team=MIL", "The requested team has no rotation data for the season."),
    ("team=DET&season=2024-25", "There is no rotation data for the requested season."),
])
def test_missing_rotation_data_is_not_found(client, seeded, query, message):
    response = client.get(f"{ROTATION}?{query}")

    assert response.status_code == 404
    assert response.get_json()["error"]["message"] == message


def test_the_route_requires_authentication(app):
    view = app.view_functions["games.get_rotation"]

    assert view.requires_auth is True


GAME = "0022500777"


@pytest.fixture
def overtime_game(seeded):
    """BOS (home) beat DET (away) in overtime.  Listed deliberately out of order."""
    store(seeded.rotation_service.repository, game(GAME, "2026-02-03", "BOS", "DET", [
        line(31, "Zed Zane", "BOS", minutes=14, periods=(6, 0, 8, 0, 0),
             stints=((1, "12:00", 1, "6:00", 6), (3, "8:00", 3, "0:00", 8))),
        line(32, "Starter Sam", "BOS", minutes=29, started=True, closed=True,
             periods=(12, 6, 0, 6, 5),
             stints=((1, "12:00", 2, "6:00", 18), (4, "6:00", 4, "0:00", 6),
                     (5, "5:00", 5, "0:00", 5))),
        line(33, "Starter Stu", "BOS", minutes=35, started=True, closed=True,
             periods=(0, 6, 12, 12, 5), stints=((2, "6:00", 5, "0:00", 35),)),
        line(34, "Sat Sid", "BOS", minutes=0),
        line(35, "Mo Mills", "BOS", minutes=9, periods=(0, 9, 0, 0, 0),
             stints=((2, "9:00", 2, "0:00", 9),)),
        line(38, "Abe Ames", "BOS", minutes=6, periods=(0, 0, 0, 6, 0),
             stints=((4, "6:00", 4, "0:00", 6),)),
        line(21, "Road Ray", "DET", minutes=25, periods=(12, 6, 0, 7, 0),
             stints=((1, "12:00", 2, "6:00", 18), (4, "7:00", 4, "0:00", 7))),
        line(20, "Road Ben", "DET", minutes=10, periods=(0, 0, 10, 0, 0),
             stints=((3, "10:00", 3, "0:00", 10),)),
        line(22, "Road Rex", "DET", minutes=22, started=True, closed=True,
             periods=(12, 6, 0, 4, 0),
             stints=((1, "12:00", 2, "6:00", 18), (4, "4:00", 4, "0:00", 4))),
        line(23, "Road Rob", "DET", minutes=0),
        line(24, "Road Rod", "DET", minutes=22, started=True,
             periods=(12, 0, 10, 0, 0),
             stints=((1, "12:00", 1, "0:00", 12), (3, "10:00", 3, "0:00", 10))),
    ], periods=5))


def test_game_rotation_serves_both_teams_in_order_with_exact_clocks(client, overtime_game):
    response = client.get(f"{ROTATION}?game_id={GAME}")

    assert response.status_code == 200
    body = response.get_json()
    assert "provenance" not in body
    assert body["game"] == {
        "game_id": GAME, "game_date": "2026-02-03", "season": "2025-26",
        "away": "DET", "home": "BOS", "periods": 5,
    }
    assert [team["team"] for team in body["teams"]] == ["DET", "BOS"]
    # Starters first (a 22-minute tie breaks by name), then bench by minutes high to low
    # (names and ids both ascend with minutes, so neither can stand in for them).
    assert [(p["name"], p["started"], p["minutes"]) for p in body["teams"][0]["players"]] == [
        ("Road Rex", True, 22.0), ("Road Rod", True, 22.0),
        ("Road Ray", False, 25.0), ("Road Ben", False, 10.0), ("Road Rob", False, 0.0),
    ]
    assert [(p["name"], p["started"], p["minutes"]) for p in body["teams"][1]["players"]] == [
        ("Starter Stu", True, 35.0), ("Starter Sam", True, 29.0),
        ("Zed Zane", False, 14.0), ("Mo Mills", False, 9.0), ("Abe Ames", False, 6.0),
        ("Sat Sid", False, 0.0),
    ]
    home = {p["name"]: p for p in body["teams"][1]["players"]}
    # Crosses the Q1-Q2 break; the overtime stint is a period 5 stint.
    assert home["Starter Sam"]["stints"] == [
        {"in": {"period": 1, "clock": "12:00"}, "out": {"period": 2, "clock": "6:00"}, "minutes": 18.0},
        {"in": {"period": 4, "clock": "6:00"}, "out": {"period": 4, "clock": "0:00"}, "minutes": 6.0},
        {"in": {"period": 5, "clock": "5:00"}, "out": {"period": 5, "clock": "0:00"}, "minutes": 5.0},
    ]
    assert home["Starter Sam"]["minutes_by_period"] == {
        "Q1": 12.0, "Q2": 6.0, "Q3": 0.0, "Q4": 6.0, "OT": 5.0,
    }
    assert home["Starter Sam"]["closed"] is True
    assert home["Zed Zane"]["stints"] == [
        {"in": {"period": 1, "clock": "12:00"}, "out": {"period": 1, "clock": "6:00"}, "minutes": 6.0},
        {"in": {"period": 3, "clock": "8:00"}, "out": {"period": 3, "clock": "0:00"}, "minutes": 8.0},
    ]
    assert home["Starter Stu"]["stints"] == [
        {"in": {"period": 2, "clock": "6:00"}, "out": {"period": 5, "clock": "0:00"}, "minutes": 35.0},
    ]


def test_game_rotation_lists_dnps_with_no_stints(client, overtime_game):
    away = client.get(f"{ROTATION}?game_id={GAME}").get_json()["teams"][0]["players"]

    assert away[-1] == {
        "name": "Road Rob", "started": False, "minutes": 0.0, "closed": False,
        "minutes_by_period": {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 0.0, "OT": 0.0},
        "stints": [],
    }


def test_game_rotation_of_the_recorded_overtime_game_has_its_overtime_stint(client, seeded):
    body = client.get(f"{ROTATION}?game_id=0022500123").get_json()

    maxey = next(p for team in body["teams"] for p in team["players"] if p["name"] == "Tyrese Maxey")
    assert (body["game"]["away"], body["game"]["home"], body["game"]["periods"]) == (
        "PHI", "WAS", 5,
    )
    assert maxey["stints"][-1] == {
        "in": {"period": 4, "clock": "12:00"}, "out": {"period": 5, "clock": "0:00"}, "minutes": 17.0,
    }


@pytest.mark.parametrize("game_id", ["0022509999", "0000000000"])
def test_a_game_without_stored_rotation_is_not_found(client, overtime_game, game_id):
    response = client.get(f"{ROTATION}?game_id={game_id}")

    assert response.status_code == 404
    error = response.get_json()["error"]
    assert error["code"] == "resource_not_found"
    assert error["details"] == {"reason": "no_rotation_data"}
