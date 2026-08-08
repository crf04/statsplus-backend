"""Tests for the validated runtime settings interface."""

from datetime import date

import pytest

from app.config.settings import (
    ConfigurationError,
    RuntimeSettings,
    current_nba_season,
    load_settings,
)


def test_current_nba_season_uses_october_boundary():
    assert current_nba_season(date(2026, 9, 30)) == "2025-26"
    assert current_nba_season(date(2026, 10, 1)) == "2026-27"


def test_local_settings_have_typed_safe_defaults(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FIREBASE_ADMIN_DISABLED", raising=False)

    settings = load_settings()

    assert isinstance(settings, RuntimeSettings)
    assert settings.database.url == "sqlite:///nba_play_types.db"
    assert settings.auth.firebase_admin_disabled is False
    assert settings.cache.enabled is True
    assert settings.llm.enable_fallback is False
    assert settings.nba.current_season == current_nba_season()
    assert isinstance(settings.providers.nba_stats_timeout_seconds, float)


def test_testing_settings_allow_credential_free_bypass(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")

    settings = load_settings()

    assert settings.environment == "testing"
    assert settings.auth.firebase_admin_disabled is True


def test_production_requires_database_and_firebase(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FIREBASE_ADMIN_DISABLED", raising=False)
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_PATH", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.delenv("FIREBASE_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("FIREBASE_CLIENT_EMAIL", raising=False)

    with pytest.raises(ConfigurationError) as error:
        load_settings()

    message = str(error.value)
    assert "DATABASE_URL" in message
    assert "Firebase" in message


def test_settings_parse_env_values(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    monkeypatch.setenv("ENABLE_CACHE", "off")
    monkeypatch.setenv("NBA_STATS_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("NBA_API_TIMEOUT_CONNECT", "2")
    monkeypatch.setenv("NBA_API_TIMEOUT_READ", "6")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.25")
    monkeypatch.setenv("LLM_MAX_TOKENS", "256")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_LLM_FALLBACK", "yes")

    settings = load_settings()

    assert settings.database.url == "postgresql://example/db"
    assert settings.cache.enabled is False
    assert settings.providers.nba_stats_timeout_seconds == 4.5
    assert settings.providers.pbp_connect_timeout_seconds == 2.0
    assert settings.providers.pbp_read_timeout_seconds == 6.0
    assert settings.llm.temperature == 0.25
    assert settings.llm.max_tokens == 256
    assert settings.llm.enable_fallback is True


def test_app_startup_exposes_one_settings_object(monkeypatch):
    from app import create_app

    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")

    app = create_app({"SKIP_FIREBASE_INIT": True, "SKIP_TABLE_CREATE": True})

    assert app.extensions["runtime_settings"] is app.config["RUNTIME_SETTINGS"]
    assert isinstance(app.extensions["runtime_settings"], RuntimeSettings)
