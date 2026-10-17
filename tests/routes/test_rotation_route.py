"""``GET /api/games/rotation`` over the Flask test client (crf04/statsplus#223).

The real service reads a migrated SQLite database seeded with a real recorded
game (the overtime fixture) beside two synthetic DET games.
"""

import json
from pathlib import Path

import pytest

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
    ("", ["player_name", "team"]),
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
