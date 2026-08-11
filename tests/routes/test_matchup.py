"""HTTP contract tests for the authenticated matchup read."""

from unittest.mock import Mock

import pytest

from app.errors import ResourceNotFoundError


def test_authenticated_matchup_preserves_the_nba_game_id_string(client, dependencies):
    dependencies.matchup_service = Mock()
    dependencies.matchup_service.get_matchup.return_value = {
        "game": {"game_id": "0022500001"},
        "league": {},
        "teams": [],
        "players": [],
        "injuries": {},
        "freshness": {},
    }

    response = client.get("/api/games/matchup?game_id=0022500001")

    assert response.status_code == 200
    assert response.get_json()["game"]["game_id"] == "0022500001"
    dependencies.matchup_service.get_matchup.assert_called_once_with(
        game_id="0022500001"
    )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "game_id=",
        "game_id=%200022500001",
        "game_id=0022500001%20",
        "game_id=0022500001&game_id=0022500002",
        "game_id=0022500001&extra=true",
    ],
)
def test_matchup_rejects_malformed_parameters(client, dependencies, query):
    dependencies.matchup_service = Mock()

    response = client.get(f"/api/games/matchup?{query}")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "The matchup parameters are invalid.",
        }
    }
    dependencies.matchup_service.get_matchup.assert_not_called()


def test_matchup_preserves_unknown_resource_errors(client, dependencies):
    dependencies.matchup_service = Mock()
    dependencies.matchup_service.get_matchup.side_effect = ResourceNotFoundError()

    response = client.get("/api/games/matchup?game_id=unknown")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "resource_not_found"


def test_matchup_requires_authentication_before_calling_service(
    client, dependencies, monkeypatch
):
    dependencies.matchup_service = Mock()
    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())

    response = client.get("/api/games/matchup?game_id=0022500001")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    dependencies.matchup_service.get_matchup.assert_not_called()
