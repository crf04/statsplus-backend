"""Regression tests for the application's structured error contract."""

from __future__ import annotations

import logging

import pytest
from flask import Flask

from app.errors import (
    AppError,
    InvalidConfigurationError,
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    register_error_handlers,
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
        raise RuntimeError("unexpected provider response: secret-provider-detail")

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


def test_internal_exception_details_are_logged_but_not_exposed(
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
    assert "secret-provider-detail" in caplog.text


def test_app_error_defaults_to_safe_public_message() -> None:
    error = AppError(detail="private implementation detail")

    assert error.public_message == "An unexpected server error occurred."
    assert error.detail == "private implementation detail"


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
