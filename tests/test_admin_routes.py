"""Authorization and HTTP method tests for privileged data refresh routes."""

from __future__ import annotations

import pytest


ADMIN_TOKEN = "admin-test-token"
USER_TOKEN = "user-test-token"


def _set_token_verifier(monkeypatch, claims: dict) -> None:
    """Configure the auth module without requiring Firebase credentials."""
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": token, "email": f"{token}@example.com", **claims},
    )
    monkeypatch.setattr(
        auth.UserService,
        "create_or_update_user",
        lambda self, user_data: None,
    )


def _admin_auth(monkeypatch) -> dict[str, str]:
    _set_token_verifier(
        monkeypatch,
        {
            "admin": True,
            "is_admin": True,
            "role": "admin",
            "roles": ["admin"],
        },
    )
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _user_auth(monkeypatch) -> dict[str, str]:
    _set_token_verifier(
        monkeypatch,
        {"admin": False, "is_admin": False, "role": "user", "roles": ["user"]},
    )
    return {"Authorization": f"Bearer {USER_TOKEN}"}


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/data/update_database", "get"),
        ("/api/data/player_PBP", "get"),
        ("/api/data/opponent_PBP", "get"),
        ("/api/data/fetch_players_with_teams", "get"),
        ("/api/players/fetch", "get"),
    ],
)
def test_privileged_mutations_reject_wrong_http_method(client, path, method):
    response = getattr(client, method)(path)

    assert response.status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/api/data/update_database",
        "/api/data/player_PBP",
        "/api/data/opponent_PBP",
        "/api/data/fetch_players_with_teams",
        "/api/data/fetch_playtypes",
        "/api/players/fetch",
    ],
)
def test_privileged_routes_reject_missing_authentication(client, monkeypatch, path):
    _set_token_verifier(monkeypatch, {})

    method = "get" if path.endswith("fetch_playtypes") else (
        "post" if path.endswith(("update_database", "fetch_players_with_teams")) else "put"
    )
    response = getattr(client, method)(path)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/data/update_database",
        "/api/data/player_PBP",
        "/api/data/opponent_PBP",
        "/api/data/fetch_players_with_teams",
        "/api/data/fetch_playtypes",
        "/api/players/fetch",
    ],
)
def test_privileged_routes_reject_non_admin(client, monkeypatch, path):
    headers = _user_auth(monkeypatch)
    method = "get" if path.endswith("fetch_playtypes") else (
        "post" if path.endswith(("update_database", "fetch_players_with_teams")) else "put"
    )
    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("path", "method", "service_method", "service_args", "result"),
    [
        (
            "/api/data/update_database",
            "post",
            "update_all_data",
            (),
            True,
        ),
        (
            "/api/data/player_PBP",
            "put",
            "fetch_PBP_data",
            ("player",),
            True,
        ),
        (
            "/api/data/opponent_PBP",
            "put",
            "fetch_PBP_data",
            ("opponent",),
            True,
        ),
        (
            "/api/data/fetch_players_with_teams",
            "post",
            "map_id_to_team",
            (),
            [{"player": "Test Player", "team": "TST"}],
        ),
        (
            "/api/data/fetch_playtypes",
            "get",
            "get_playtypes",
            (),
            [{"play_type": "Transition"}],
        ),
    ],
)
def test_data_routes_allow_admin_without_provider_or_database_calls(
    client,
    monkeypatch,
    path,
    method,
    service_method,
    service_args,
    result,
):
    from app.routes import data_update_routes

    headers = _admin_auth(monkeypatch)

    if service_method == "map_id_to_team":
        monkeypatch.setattr(data_update_routes.data_service, "save_team", lambda: None)
    monkeypatch.setattr(
        data_update_routes.data_service,
        service_method,
        lambda *args: result,
    )
    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 200
    if path.endswith("update_database"):
        assert response.get_json() == {"message": "Database updated successfully"}
    elif path.endswith(("player_PBP", "opponent_PBP")):
        assert "processed and stored successfully" in response.get_json()["message"]
    else:
        assert response.get_json() == result


def test_player_fetch_allows_admin_without_provider_or_database_calls(
    client, monkeypatch
):
    from app.routes import player_routes

    headers = _admin_auth(monkeypatch)
    monkeypatch.setattr(
        player_routes.player_service,
        "store_player_information",
        lambda: True,
    )

    response = client.put("/api/players/fetch", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {
        "message": "Player data processed and stored successfully"
    }


def test_admin_stats_rejects_non_admin(client, monkeypatch):
    headers = _user_auth(monkeypatch)

    response = client.get("/api/user/admin/stats", headers=headers)

    assert response.status_code == 403


def test_admin_stats_allows_admin_without_database_call(client, monkeypatch):
    from app.routes import user_routes

    headers = _admin_auth(monkeypatch)
    monkeypatch.setattr(
        user_routes.user_service,
        "get_all_active_users_count",
        lambda: 7,
    )

    response = client.get("/api/user/admin/stats", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "admin_stats": {"total_active_users": 7},
    }
