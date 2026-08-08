"""HTTP behavior for the configured browser-origin allowlist."""

import pytest

from app.config.settings import ConfigurationError, load_settings


def test_allowed_origin_receives_cors_header(client):
    response = client.get("/api/players", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"


def test_disallowed_origin_does_not_receive_cors_header(client):
    response = client.get(
        "/api/players", headers={"Origin": "https://attacker.example"}
    )

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_allowed_origin_preflight_is_configured(client):
    response = client.options(
        "/api/players",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "GET" in response.headers["Access-Control-Allow-Methods"]
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]


def test_missing_origin_is_not_treated_as_cross_origin(client):
    response = client.get("/api/players")

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_settings_parse_explicit_cors_origins():
    settings = load_settings(
        environ={
            "FLASK_ENV": "development",
            "CORS_ALLOWED_ORIGINS": "https://stats.example, https://admin.example",
        }
    )

    assert settings.cors.allowed_origins == (
        "https://stats.example",
        "https://admin.example",
    )


def test_production_requires_explicit_cors_origins():
    with pytest.raises(ConfigurationError, match="CORS_ALLOWED_ORIGINS"):
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/statsplus",
                "FIREBASE_SERVICE_ACCOUNT_JSON": (
                    '{"project_id":"p","private_key":"k","client_email":"e"}'
                ),
            }
        )


def test_production_rejects_local_default_and_wildcard_origins():
    production = {
        "FLASK_ENV": "production",
        "DATABASE_URL": "postgresql://example/statsplus",
        "FIREBASE_SERVICE_ACCOUNT_JSON": (
            '{"project_id":"p","private_key":"k","client_email":"e"}'
        ),
    }

    with pytest.raises(ConfigurationError, match="CORS_ALLOWED_ORIGINS"):
        load_settings(
            environ={**production, "CORS_ALLOWED_ORIGINS": "http://localhost:3000"}
        )

    with pytest.raises(ConfigurationError, match="CORS_ALLOWED_ORIGINS"):
        load_settings(environ={**production, "CORS_ALLOWED_ORIGINS": "*"})


def test_production_accepts_a_configured_https_origin_allowlist():
    settings = load_settings(
        environ={
            "FLASK_ENV": "production",
            "DATABASE_URL": "postgresql://example/statsplus",
            "FIREBASE_SERVICE_ACCOUNT_JSON": (
                '{"project_id":"p","private_key":"k","client_email":"e"}'
            ),
            "CORS_ALLOWED_ORIGINS": "https://stats.example.com",
        }
    )

    assert settings.cors.allowed_origins == ("https://stats.example.com",)
