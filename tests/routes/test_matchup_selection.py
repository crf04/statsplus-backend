"""HTTP contract tests for the matchup selection read."""

from unittest.mock import Mock

import pytest

from app.errors import ResourceNotFoundError


def test_authenticated_selection_echoes_canonical_integer_player_id(
    client, dependencies
):
    dependencies.matchup_selection_service = Mock()
    dependencies.matchup_selection_service.get_selection.return_value = {
        "player_id": 2544,
        "h2h": {"thin": True, "rows": []},
        "archetype": {"thin": True, "rows": []},
    }

    response = client.get(
        "/api/games/matchup/selection?game_id=0022500001&player_id=2544"
    )

    assert response.status_code == 200
    assert response.get_json()["player_id"] == 2544
    dependencies.matchup_selection_service.get_selection.assert_called_once_with(
        game_id="0022500001", player_id=2544
    )


@pytest.mark.parametrize(
    "query",
    [
        "player_id=2544",
        "game_id=0022500001",
        "game_id=&player_id=2544",
        "game_id=0022500001&player_id=",
        "game_id=0022500001&player_id=25.44",
        "game_id=0022500001&player_id=-1",
        "game_id=0022500001&player_id=02544",
        "game_id=0022500001&player_id=9223372036854775808",
        "game_id=0022500001&player_id=2544&player_id=201939",
        "game_id=0022500001&player_id=2544&extra=true",
    ],
)
def test_selection_rejects_malformed_parameters(client, dependencies, query):
    dependencies.matchup_selection_service = Mock()

    response = client.get(f"/api/games/matchup/selection?{query}")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "The matchup selection parameters are invalid.",
        }
    }
    dependencies.matchup_selection_service.get_selection.assert_not_called()


def test_selection_preserves_unknown_resource_errors(client, dependencies):
    dependencies.matchup_selection_service = Mock()
    dependencies.matchup_selection_service.get_selection.side_effect = (
        ResourceNotFoundError()
    )

    response = client.get("/api/games/matchup/selection?game_id=unknown&player_id=2544")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "resource_not_found"


def test_selection_requires_authentication_before_calling_service(
    client, dependencies, monkeypatch
):
    dependencies.matchup_selection_service = Mock()
    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())

    response = client.get(
        "/api/games/matchup/selection?game_id=0022500001&player_id=2544"
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    dependencies.matchup_selection_service.get_selection.assert_not_called()
