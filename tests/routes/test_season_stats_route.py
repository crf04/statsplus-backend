"""``GET /api/players/season_stats`` over the Flask test client (crf04/statsplus-backend#509).

The real service reads a migrated SQLite database seeded with two seasons.
"""

import pytest

from tests.support.season_stats_world import (
    EVEN_LINE,
    JOKIC,
    JOKIC_ADVANCED,
    JOKIC_ADVANCED_LINE,
    JOKIC_LINE,
    RESERVE_LINE,
    RETIRED_LINE,
    migrated_repository,
    rows,
    service_for,
)

SEASON_STATS = "/api/players/season_stats"


@pytest.fixture
def seeded(tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    repository.replace_season(
        "2023-24", "Regular Season", rows("2023-24", JOKIC_LINE, RESERVE_LINE, RETIRED_LINE, EVEN_LINE)
    )
    repository.replace_season(
        "2024-25",
        "Regular Season",
        rows("2024-25", {**JOKIC_LINE, "GP": 70, "PTS": 1700, "AGE": 29.0}),
    )
    dependencies.player_season_stats_service = service_for(repository)
    return repository


JOKIC_DOCUMENT = {
    "player_id": 203999,
    "player_name": "Nikola Jokić",
    "team": "DEN",
    "age": 29,
    "gp": 79,
    "w": 55,
    "l": 24,
    "totals": {
        "min": 2736.5333333333333,
        "pts": 2085,
        "reb": 976,
        "oreb": 223,
        "dreb": 753,
        "ast": 708,
        "stl": 108,
        "blk": 68,
        "tov": 237,
        "pf": 194,
        "fgm": 822,
        "fga": 1411,
        "fg3m": 83,
        "fg3a": 231,
        "ftm": 358,
        "fta": 438,
        "plus_minus": 682,
    },
    "per_game": {
        "min": 34.6,
        "pts": 26.4,
        "reb": 12.4,
        "oreb": 2.8,
        "dreb": 9.5,
        "ast": 9.0,
        "stl": 1.4,
        "blk": 0.9,
        "tov": 3.0,
        "pf": 2.5,
        "fgm": 10.4,
        "fga": 17.9,
        "fg3m": 1.1,
        "fg3a": 2.9,
        "ftm": 4.5,
        "fta": 5.5,
        "plus_minus": 8.6,
    },
    "pct": {"fg": 0.583, "fg3": 0.359, "ft": 0.817},
    "advanced": JOKIC_ADVANCED,
}


def test_a_loaded_season_answers_every_player_in_player_id_order(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2023-24")

    assert response.status_code == 200
    body = response.get_json()
    assert (body["season"], body["season_type"]) == ("2023-24", "Regular Season")
    assert [player["player_id"] for player in body["players"]] == [2200, JOKIC, 1700001, 1700002]
    assert body["players"][1] == JOKIC_DOCUMENT


def test_player_id_filters_to_that_player_in_the_requested_season(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2024-25&player_id={JOKIC}")

    body = response.get_json()
    assert response.status_code == 200
    assert body["season"] == "2024-25"
    (player,) = body["players"]
    assert (player["player_id"], player["age"], player["gp"], player["totals"]["pts"]) == (
        JOKIC,
        29,
        70,
        1700,
    )
    assert player["per_game"]["pts"] == 24.3


def test_a_player_missing_from_a_loaded_season_answers_an_empty_list(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2024-25&player_id=2200")

    assert response.status_code == 200
    assert response.get_json() == {"season": "2024-25", "season_type": "Regular Season", "players": []}


def test_per_game_values_round_half_up(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2023-24&player_id=1700001")

    (player,) = response.get_json()["players"]
    assert player["per_game"]["pts"] == 0.2
    assert player["per_game"]["min"] == 3.1
    assert player["per_game"]["plus_minus"] == -0.1


def test_a_tiny_negative_per_game_value_prints_as_zero(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2023-24&player_id=1700002")

    (player,) = response.get_json()["players"]
    assert player["per_game"]["plus_minus"] == 0.0
    assert b"-0.0" not in response.data


def test_a_player_with_no_attempts_has_null_percentages(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2023-24&player_id=1700001")

    (player,) = response.get_json()["players"]
    assert player["pct"] == {"fg": 0.333, "fg3": None, "ft": 0.5}


def test_advanced_stats_are_served_as_stored_with_turnover_percent_a_fraction(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2023-24&player_id={JOKIC}")

    (player,) = response.get_json()["players"]
    assert player["advanced"] == {
        "off_rating": 122.4,
        "def_rating": 110.6,
        "net_rating": 11.8,
        "ts_pct": 0.65,
        "efg_pct": 0.612,
        "usg_pct": 0.288,
        "ast_pct": 0.391,
        "ast_to": 2.99,
        "ast_ratio": 27.9,
        "oreb_pct": 0.086,
        "dreb_pct": 0.27,
        "reb_pct": 0.182,
        "tov_pct": 0.093,
        "pace": 98.05,
        "pie": 0.211,
        "poss": 5599,
    }


def test_a_player_with_no_advanced_row_has_null_advanced(client, tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    repository.replace_season(
        "2023-24",
        "Regular Season",
        rows("2023-24", JOKIC_LINE, RESERVE_LINE, advanced=[JOKIC_ADVANCED_LINE]),
    )
    dependencies.player_season_stats_service = service_for(repository)

    players = client.get(f"{SEASON_STATS}?season=2023-24").get_json()["players"]

    assert [(player["player_id"], player["advanced"] is None) for player in players] == [
        (JOKIC, False),
        (1700001, True),
    ]


def test_one_missing_advanced_value_is_null_inside_an_otherwise_present_object(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2023-24&player_id=1700001")

    (player,) = response.get_json()["players"]
    assert player["advanced"]["ast_to"] is None
    assert (player["advanced"]["tov_pct"], player["advanced"]["off_rating"]) == (0.125, 80.0)


def test_an_unloaded_season_answers_404_listing_the_loaded_seasons(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2022-23")

    assert response.status_code == 404
    assert response.get_json() == {
        "error": {
            "code": "season_not_loaded",
            "message": "No player stats are loaded for the requested season.",
            "details": {"loaded_seasons": ["2023-24", "2024-25"]},
        }
    }


def test_an_unloaded_season_with_a_player_id_still_answers_404(client, seeded):
    response = client.get(f"{SEASON_STATS}?season=2022-23&player_id={JOKIC}")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "season_not_loaded"


def test_nothing_loaded_answers_404_with_an_empty_list(client, tmp_path, dependencies):
    dependencies.player_season_stats_service = service_for(migrated_repository(tmp_path))

    response = client.get(f"{SEASON_STATS}?season=2023-24")

    assert response.status_code == 404
    assert response.get_json()["error"]["details"] == {"loaded_seasons": []}


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        ("", ["season"]),
        ("season=2023", ["season"]),
        ("season=2023-25", ["season"]),
        ("season=23-24", ["season"]),
        ("season=%202023-24", ["season"]),
        ("season=2023-24&season=2024-25", ["season"]),
        ("season=2023-24&player_id=abc", ["player_id"]),
        ("season=2023-24&player_id=-1", ["player_id"]),
        ("season=2023-24&player_id=", ["player_id"]),
        ("season=2023-24&season_type=Playoffs", ["season_type"]),
    ],
)
def test_malformed_requests_answer_400_naming_the_parameters(client, seeded, query, parameters):
    response = client.get(f"{SEASON_STATS}?{query}")

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    assert error["details"] == {"parameters": parameters}


def test_the_route_requires_authentication(app):
    view = app.view_functions["players.get_season_stats"]

    assert view.requires_auth is True
