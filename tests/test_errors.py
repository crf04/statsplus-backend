"""Regression tests for the application's structured error contract."""

from __future__ import annotations

import logging

import pytest
from flask import Flask

from app.errors import (
    AppError,
    InvalidConfigurationError,
    InvalidInputError,
    OperationFailedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    register_error_handlers,
    route_error_boundary,
)


@pytest.fixture
def error_app() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_error_handlers(app)

    @app.get("/invalid-input")
    def invalid_input() -> None:
        raise InvalidInputError(
            "The minutes filter is invalid.",
            detail="minutes_filter=not-a-range",
        )

    @app.get("/missing-resource")
    def missing_resource() -> None:
        raise ResourceNotFoundError(
            "The requested player was not found.",
            detail="player_name=secret-player",
        )

    @app.get("/provider-unavailable")
    def provider_unavailable() -> None:
        raise ProviderUnavailableError(
            detail="stats.nba.com timed out with internal token=secret-token"
        )

    @app.get("/invalid-configuration")
    def invalid_configuration() -> None:
        raise InvalidConfigurationError(
            detail="DATABASE_URL=postgresql://user:password@example.invalid/stats"
        )

    @app.get("/unexpected")
    def unexpected() -> None:
        raise RuntimeError(
            "unexpected provider response: token=token-secret api_key=api-secret"
        )

    @app.get("/wrapped-unexpected")
    @route_error_boundary("The wrapped operation failed.")
    def wrapped_unexpected() -> None:
        raise RuntimeError(
            "provider request failed: "
            "DATABASE_URL=postgresql://db-user:db-password@example.invalid/stats "
            "token=token-secret api_key=api-secret password=password-secret "
            "private_key=private-key-secret Authorization: Bearer bearer-secret "
            "-----BEGIN PRIVATE KEY-----\nprivate-key-material\n"
            "-----END PRIVATE KEY-----"
        )

    return app


@pytest.mark.parametrize(
    ("path", "status", "code", "message"),
    [
        (
            "/invalid-input",
            400,
            "invalid_input",
            "The minutes filter is invalid.",
        ),
        (
            "/missing-resource",
            404,
            "resource_not_found",
            "The requested player was not found.",
        ),
        (
            "/provider-unavailable",
            503,
            "provider_unavailable",
            "An upstream provider is currently unavailable. Please try again later.",
        ),
        (
            "/invalid-configuration",
            500,
            "invalid_configuration",
            "The server configuration is invalid.",
        ),
    ],
)
def test_public_error_categories_have_stable_responses(
    error_app: Flask,
    path: str,
    status: int,
    code: str,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)

    response = error_app.test_client().get(path)

    assert response.status_code == status
    assert response.get_json() == {"error": {"code": code, "message": message}}


def test_internal_exception_details_are_sanitized_and_logged_once(
    error_app: Flask, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)

    response = error_app.test_client().get("/unexpected")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected server error occurred.",
        }
    }
    assert "unexpected provider response" in caplog.text
    assert "token-secret" not in caplog.text
    assert "api-secret" not in caplog.text
    assert len([record for record in caplog.records if record.name == "app.errors"]) == 1


def test_sensitive_diagnostic_details_are_redacted_without_losing_context(
    error_app: Flask, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)

    response = error_app.test_client().get("/wrapped-unexpected")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "The wrapped operation failed.",
        }
    }
    assert "provider request failed" in caplog.text
    for secret in (
        "db-password",
        "token-secret",
        "api-secret",
        "password-secret",
        "private-key-secret",
        "bearer-secret",
        "private-key-material",
    ):
        assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "[REDACTED PEM]" in caplog.text
    assert len([record for record in caplog.records if record.name == "app.errors"]) == 1
    assert not any(
        record.exc_info
        for record in caplog.records
        if record.name == "app.errors"
    )


def test_app_error_defaults_to_safe_public_message() -> None:
    error = AppError(detail="private implementation detail")

    assert error.public_message == "An unexpected server error occurred."
    assert error.detail == "private implementation detail"


def test_route_error_boundary_preserves_expected_application_errors() -> None:
    expected = InvalidInputError("The request is invalid.")

    @route_error_boundary("The operation failed.")
    def handler() -> None:
        raise expected

    with pytest.raises(InvalidInputError) as raised:
        handler()

    assert raised.value is expected


def test_route_error_boundary_translates_unexpected_errors_without_logging(caplog) -> None:
    @route_error_boundary("The operation failed.")
    def handler() -> None:
        raise RuntimeError("private provider detail")

    with pytest.raises(OperationFailedError) as raised:
        handler()

    assert raised.value.public_message == "The operation failed."
    assert raised.value.detail == "private provider detail"
    assert not caplog.records


def test_game_logs_invalid_input_uses_central_handler(client) -> None:
    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&minutes_filter=not-a-range"
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "One or more game log filters are invalid.",
        }
    }


def test_player_profile_missing_resource_uses_central_handler(client, monkeypatch) -> None:
    from app.routes import player_routes

    monkeypatch.setattr(
        player_routes.player_service,
        "get_player_profile",
        lambda *args, **kwargs: None,
    )

    response = client.get(
        "/api/players/profile?player_name=LeBron%20James&category=Playtypes"
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "error": {
            "code": "resource_not_found",
            "message": "The requested player profile was not found.",
        }
    }


def test_player_profile_provider_failure_uses_central_handler(client, monkeypatch) -> None:
    import requests

    from app.routes import player_routes

    def unavailable(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("profile-provider-secret")

    monkeypatch.setattr(
        player_routes.player_service,
        "get_player_profile",
        unavailable,
    )

    response = client.get(
        "/api/players/profile?player_name=LeBron%20James&category=Playtypes"
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "An upstream provider is currently unavailable. Please try again later.",
        }
    }


def test_missing_auth_header_uses_nested_error_contract(client, monkeypatch) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    response = client.get("/api/games/game_logs")

    assert response.status_code == 401
    assert response.get_json() == {
        "error": {
            "code": "authentication_required",
            "message": "Please provide a valid Firebase token.",
        }
    }


def test_non_admin_auth_uses_nested_error_contract(client, monkeypatch) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": "viewer",
            "email": "viewer@example.com",
            "role": "viewer",
        },
    )
    monkeypatch.setattr(auth.UserService, "create_or_update_user", lambda *args: None)

    response = client.get(
        "/api/data/fetch_playtypes",
        headers={"Authorization": "Bearer viewer-token"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": {
            "code": "forbidden",
            "message": "Administrator privileges are required.",
        }
    }


def test_invalid_token_details_are_not_exposed(client, monkeypatch) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    def reject_token(token):
        raise ValueError("firebase-token-secret")

    monkeypatch.setattr(auth, "verify_firebase_token", reject_token)

    response = client.get(
        "/api/games/game_logs",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": {
            "code": "invalid_token",
            "message": "The provided Firebase token is invalid.",
        }
    }
    assert "firebase-token-secret" not in response.get_data(as_text=True)


def test_route_exception_details_are_not_exposed(client, monkeypatch) -> None:
    from app.routes import player_routes

    def fail_to_load_players():
        raise RuntimeError("player-provider-secret")

    monkeypatch.setattr(
        player_routes.player_service,
        "get_all_players",
        fail_to_load_players,
    )

    response = client.get("/api/players")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "Failed to retrieve players.",
        }
    }
    assert "player-provider-secret" not in response.get_data(as_text=True)


def test_nl_query_missing_query_uses_nested_error_contract(client) -> None:
    response = client.post("/api/nl-query", json={})

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "A query is required.",
        }
    }


def test_health_failure_uses_nested_error_contract(client, monkeypatch) -> None:
    from app.routes import health_routes

    class FailingEngine:
        dialect = type("Dialect", (), {"name": "sqlite", "driver": "pysqlite"})()

        def connect(self):
            raise RuntimeError("database-password-secret")

    monkeypatch.setattr(health_routes, "get_engine", lambda: FailingEngine())

    response = client.get("/api/health/db")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "internal_error",
            "message": "The database health check failed.",
        }
    }
    assert "database-password-secret" not in response.get_data(as_text=True)


def test_data_route_failure_uses_nested_error_contract(client, monkeypatch) -> None:
    from app.routes import data_update_routes

    def fail_to_fetch_playtypes():
        raise RuntimeError("playtypes-provider-secret")

    monkeypatch.setattr(
        data_update_routes.data_service,
        "get_playtypes",
        fail_to_fetch_playtypes,
    )

    response = client.get("/api/data/fetch_playtypes")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "Failed to fetch play types.",
        }
    }
    assert "playtypes-provider-secret" not in response.get_data(as_text=True)


def test_team_missing_data_uses_nested_error_contract(client, monkeypatch) -> None:
    from app.routes import team_routes

    monkeypatch.setattr(
        team_routes.team_service,
        "get_team_stats",
        lambda *args, **kwargs: None,
    )

    response = client.get("/api/teams/stats?team=Example&category=Traditional")

    assert response.status_code == 404
    assert response.get_json() == {
        "error": {
            "code": "resource_not_found",
            "message": "No data found for the specified team and category.",
        }
    }


def test_user_route_exception_details_are_not_exposed(client, monkeypatch) -> None:
    from app.routes import user_routes

    def fail_to_load_user(*args, **kwargs):
        raise RuntimeError("user-database-secret")

    monkeypatch.setattr(
        user_routes.user_service,
        "get_user_by_firebase_uid",
        fail_to_load_user,
    )

    response = client.get("/api/user/profile")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "Failed to retrieve user profile.",
        }
    }
    assert "user-database-secret" not in response.get_data(as_text=True)
