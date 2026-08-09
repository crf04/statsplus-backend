from __future__ import annotations


def test_dabble_lines_route_passes_typed_query(client, dependencies):
    dependencies.dabble_service.get_lines.return_value = {
        "provider": "dabble",
        "count": 1,
        "lines": [{"player_name": "LeBron James", "stat": "points", "line": 25.5}],
    }

    response = client.get(
        "/api/dabble/lines?competition=NBA&player=LeBron&stat=points"
        "&limit=2&include_in_play=true"
    )

    assert response.status_code == 200
    assert response.get_json()["count"] == 1
    dependencies.dabble_service.get_lines.assert_called_once_with(
        competition="NBA",
        competition_id=None,
        fixture_id=None,
        player="LeBron",
        stat="points",
        fixture_limit=2,
        include_in_play=True,
    )


def test_dabble_competitions_route_supports_sport_discovery(client, dependencies):
    dependencies.dabble_service.list_competitions.return_value = {
        "provider": "dabble",
        "count": 1,
        "competitions": [{"id": "nba", "name": "NBA", "sport": "Basketball"}],
    }

    response = client.get("/api/dabble/competitions?sport=Basketball")

    assert response.status_code == 200
    assert response.get_json()["competitions"][0]["name"] == "NBA"
    dependencies.dabble_service.list_competitions.assert_called_once_with(
        sport="Basketball", sport_id=None
    )


def test_dabble_lines_route_rejects_bad_limit(client):
    response = client.get("/api/dabble/lines?competition=NBA&limit=many")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_dabble_lines_route_rejects_bad_boolean(client):
    response = client.get(
        "/api/dabble/lines?competition=NBA&include_in_play=perhaps"
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
