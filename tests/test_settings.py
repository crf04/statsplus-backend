"""Tests for the validated runtime settings interface."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config.settings import (
    ConfigurationError,
    NBASeasonSettings,
    RuntimeSettings,
    current_nba_season,
    load_settings,
)


def test_current_nba_season_uses_october_boundary():
    assert current_nba_season(date(2026, 9, 30)) == "2025-26"
    assert current_nba_season(date(2026, 10, 1)) == "2026-27"


def test_event_catalog_max_age_is_configurable(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setenv("EVENT_CATALOG_MAX_AGE_HOURS", "24")

    settings = load_settings()

    assert settings.catalog.event_max_age_hours == 24


def test_slate_schedule_max_age_defaults_to_thirty_hours_and_is_configurable():
    defaults = load_settings(environ={"FLASK_ENV": "testing"})
    configured = load_settings(
        environ={"FLASK_ENV": "testing", "SLATE_SCHEDULE_MAX_AGE_HOURS": "36"}
    )

    assert defaults.catalog.slate_schedule_max_age_hours == 30
    assert configured.catalog.slate_schedule_max_age_hours == 36


def test_player_game_log_max_age_defaults_to_thirty_hours_and_is_configurable():
    defaults = load_settings(environ={"FLASK_ENV": "testing"})
    configured = load_settings(
        environ={"FLASK_ENV": "testing", "PLAYER_GAME_LOG_MAX_AGE_HOURS": "36"}
    )

    assert defaults.catalog.player_game_log_max_age_hours == 30
    assert configured.catalog.player_game_log_max_age_hours == 36


def test_player_game_log_sport_minimum_defaults_to_five_and_is_configurable():
    defaults = load_settings(environ={"FLASK_ENV": "testing"})
    configured = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME": "7",
        }
    )

    assert defaults.catalog.player_game_log_min_active_players_per_team_game == 5
    assert configured.catalog.player_game_log_min_active_players_per_team_game == 7


def test_matchup_selection_thin_thresholds_are_named_configuration():
    defaults = load_settings(environ={"FLASK_ENV": "testing"})
    configured = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "MATCHUP_SELECTION_H2H_MIN_GAMES": "2",
            "MATCHUP_SELECTION_ARCHETYPE_MIN_GAMES": "8",
        }
    )

    assert defaults.catalog.matchup_selection_h2h_min_games == 1
    assert defaults.catalog.matchup_selection_archetype_min_games == 5
    assert configured.catalog.matchup_selection_h2h_min_games == 2
    assert configured.catalog.matchup_selection_archetype_min_games == 8


def test_matchup_score_thin_thresholds_are_named_configuration():
    defaults = load_settings(environ={"FLASK_ENV": "testing"})
    configured = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "MATCHUP_SCORE_MIN_GAMES": "8",
            "MATCHUP_SCORE_PLAY_TYPES_MIN_VOLUME_PER_GAME": "2.5",
            "MATCHUP_SCORE_SHOT_ZONES_MIN_VOLUME_PER_GAME": "3.5",
            "MATCHUP_SCORE_SHOT_TYPES_MIN_VOLUME_PER_GAME": "5.5",
            "MATCHUP_SCORE_ASSIST_LOCATIONS_MIN_VOLUME_PER_GAME": "1.5",
        }
    )

    assert defaults.matchup_scores.min_games == 5
    assert defaults.matchup_scores.play_types_min_volume_per_game == 1
    assert defaults.matchup_scores.shot_zones_min_volume_per_game == 1
    assert defaults.matchup_scores.shot_types_min_volume_per_game == 4
    assert defaults.matchup_scores.assist_locations_min_volume_per_game == 1
    assert configured.matchup_scores.min_games == 8
    assert configured.matchup_scores.play_types_min_volume_per_game == 2.5
    assert configured.matchup_scores.shot_zones_min_volume_per_game == 3.5
    assert configured.matchup_scores.shot_types_min_volume_per_game == 5.5
    assert configured.matchup_scores.assist_locations_min_volume_per_game == 1.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MATCHUP_SCORE_MIN_GAMES", "0"),
        ("MATCHUP_SCORE_MIN_GAMES", "not-an-integer"),
        ("MATCHUP_SCORE_SHOT_TYPES_MIN_VOLUME_PER_GAME", "-1"),
        ("MATCHUP_SCORE_SHOT_TYPES_MIN_VOLUME_PER_GAME", "not-a-number"),
        ("MATCHUP_SCORE_SHOT_TYPES_MIN_VOLUME_PER_GAME", "nan"),
        ("MATCHUP_SCORE_SHOT_TYPES_MIN_VOLUME_PER_GAME", "inf"),
    ],
)
def test_matchup_score_thin_thresholds_reject_invalid_values(name, value):
    with pytest.raises(ConfigurationError):
        load_settings(environ={"FLASK_ENV": "testing", name: value})


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_matchup_selection_thin_thresholds_must_be_positive_integers(value):
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={
                "FLASK_ENV": "testing",
                "MATCHUP_SELECTION_ARCHETYPE_MIN_GAMES": value,
            }
        )


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_player_game_log_sport_minimum_must_be_a_positive_integer(value):
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={
                "FLASK_ENV": "testing",
                "PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME": value,
            }
        )


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
    assert settings.cors.allowed_origins == ("http://localhost:3000",)
    assert settings.nba.current_season == current_nba_season()
    assert isinstance(settings.providers.nba_stats_timeout_seconds, float)
    assert settings.features.injury_report_enabled is False
    assert settings.providers.rotowire_permission_granted is False
    assert settings.providers.rotowire_connect_timeout_seconds == 3.0
    assert settings.providers.rotowire_read_timeout_seconds == 8.0


def test_injury_collection_requires_two_explicit_runtime_gates():
    enabled_without_permission = load_settings(
        environ={"FLASK_ENV": "testing", "INJURY_REPORT_ENABLED": "true"}
    )
    permitted = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "INJURY_REPORT_ENABLED": "true",
            "ROTOWIRE_PERMISSION_GRANTED": "true",
        }
    )

    assert enabled_without_permission.features.injury_report_enabled is True
    assert enabled_without_permission.providers.rotowire_permission_granted is False
    assert permitted.features.injury_report_enabled is True
    assert permitted.providers.rotowire_permission_granted is True


def test_rotowire_transport_timeouts_are_typed_named_settings():
    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "ROTOWIRE_CONNECT_TIMEOUT_SECONDS": "1.25",
            "ROTOWIRE_READ_TIMEOUT_SECONDS": "4.5",
        }
    )

    assert settings.providers.rotowire_connect_timeout_seconds == 1.25
    assert settings.providers.rotowire_read_timeout_seconds == 4.5


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


def test_production_uses_valid_json_when_legacy_credential_path_is_stale():
    load_settings(
        environ={
            "FLASK_ENV": "production",
            "DATABASE_URL": "postgresql://example/db",
            "CORS_ALLOWED_ORIGINS": "https://courtai.app",
            "DFS_ENABLED_PROVIDERS": "prizepicks",
            "FIREBASE_SERVICE_ACCOUNT_PATH": "/missing/legacy-service-account.json",
            "FIREBASE_SERVICE_ACCOUNT_JSON": (
                '{"project_id":"courtai-test","private_key":"test-key",'
                '"client_email":"firebase@example.com"}'
            ),
        }
    )


def test_production_uses_individual_fields_when_legacy_credential_path_is_stale():
    load_settings(
        environ={
            "FLASK_ENV": "production",
            "DATABASE_URL": "postgresql://example/db",
            "CORS_ALLOWED_ORIGINS": "https://courtai.app",
            "DFS_ENABLED_PROVIDERS": "prizepicks",
            "FIREBASE_SERVICE_ACCOUNT_PATH": "/missing/legacy-service-account.json",
            "FIREBASE_PROJECT_ID": "courtai-test",
            "FIREBASE_PRIVATE_KEY": "test-key",
            "FIREBASE_CLIENT_EMAIL": "firebase@example.com",
        }
    )


def test_production_rejects_stale_credential_path_without_an_alternative():
    with pytest.raises(ConfigurationError, match="must point to an existing file"):
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_SERVICE_ACCOUNT_PATH": "/missing/service-account.json",
            }
        )


def test_production_names_missing_individual_field_with_stale_credential_path():
    with pytest.raises(ConfigurationError) as error:
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_SERVICE_ACCOUNT_PATH": "/missing/service-account.json",
                "FIREBASE_PROJECT_ID": "courtai-test",
                "FIREBASE_CLIENT_EMAIL": "firebase@example.com",
            }
        )

    message = str(error.value)
    assert "FIREBASE_PRIVATE_KEY" in message
    assert "FIREBASE_SERVICE_ACCOUNT_PATH" in message


def test_production_names_missing_individual_field_without_credential_path():
    with pytest.raises(ConfigurationError) as error:
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_PROJECT_ID": "courtai-test",
                "FIREBASE_CLIENT_EMAIL": "firebase@example.com",
            }
        )

    assert "FIREBASE_PRIVATE_KEY" in str(error.value)


def test_production_rejects_directory_credential_path_before_json(tmp_path):
    with pytest.raises(ConfigurationError, match="must point to a regular file"):
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_SERVICE_ACCOUNT_PATH": str(tmp_path),
                "FIREBASE_SERVICE_ACCOUNT_JSON": (
                    '{"project_id":"courtai-test","private_key":"test-key",'
                    '"client_email":"firebase@example.com"}'
                ),
            }
        )


def test_production_rejects_corrupt_file_as_the_only_credential_source(tmp_path):
    credential_path = tmp_path / "service-account.json"
    credential_path.write_text("not valid JSON", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="readable, valid JSON"):
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_SERVICE_ACCOUNT_PATH": str(credential_path),
            }
        )


def test_production_accepts_valid_file_as_the_only_credential_source(tmp_path):
    credential_path = tmp_path / "service-account.json"
    credential_path.write_text(
        '{"project_id":"courtai-test","private_key":"test-key",'
        '"client_email":"firebase@example.com"}',
        encoding="utf-8",
    )

    load_settings(
        environ={
            "FLASK_ENV": "production",
            "DATABASE_URL": "postgresql://example/db",
            "CORS_ALLOWED_ORIGINS": "https://courtai.app",
            "DFS_ENABLED_PROVIDERS": "prizepicks",
            "FIREBASE_SERVICE_ACCOUNT_PATH": str(credential_path),
        }
    )


def test_production_rejects_non_object_credential_file(tmp_path):
    credential_path = tmp_path / "service-account.json"
    credential_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="must contain a JSON object"):
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_SERVICE_ACCOUNT_PATH": str(credential_path),
            }
        )


def test_production_names_missing_credential_file_field(tmp_path):
    credential_path = tmp_path / "service-account.json"
    credential_path.write_text(
        '{"project_id":"courtai-test","client_email":"firebase@example.com"}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as error:
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://example/db",
                "CORS_ALLOWED_ORIGINS": "https://courtai.app",
                "DFS_ENABLED_PROVIDERS": "prizepicks",
                "FIREBASE_SERVICE_ACCOUNT_PATH": str(credential_path),
            }
        )

    message = str(error.value)
    assert "FIREBASE_SERVICE_ACCOUNT_PATH is missing required fields" in message
    assert "private_key" in message


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


def test_database_pool_settings_have_typed_defaults():
    settings = load_settings(environ={"FLASK_ENV": "testing"})

    assert settings.database.pool_size == 3
    assert settings.database.max_overflow == 4
    assert settings.database.pool_recycle_seconds == 300
    assert settings.database.connect_timeout_seconds == 5


def test_database_pool_settings_parse_env_values(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "10")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "20")
    monkeypatch.setenv("DATABASE_POOL_RECYCLE_SECONDS", "600")
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "15")

    settings = load_settings()

    assert settings.database.pool_size == 10
    assert settings.database.max_overflow == 20
    assert settings.database.pool_recycle_seconds == 600
    assert settings.database.connect_timeout_seconds == 15


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_POOL_SIZE", "0"),
        ("DATABASE_POOL_SIZE", "not-an-integer"),
        ("DATABASE_MAX_OVERFLOW", "-1"),
        ("DATABASE_POOL_RECYCLE_SECONDS", "0"),
        ("DATABASE_CONNECT_TIMEOUT_SECONDS", "0"),
    ],
)
def test_database_pool_settings_reject_out_of_range_values(name, value):
    with pytest.raises(ConfigurationError):
        load_settings(environ={"FLASK_ENV": "testing", name: value})


def test_llm_fallback_defaults_to_one_bounded_attempt(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///nba_play_types.db")
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)

    settings = load_settings()

    assert settings.llm.timeout_seconds == 8.0
    assert settings.llm.max_retries == 1


def test_settings_parse_athlete_catalog_freshness_window(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setenv("ATHLETE_CATALOG_FRESHNESS_DAYS", "14")

    settings = load_settings()

    assert settings.catalog.athlete_freshness_days == 14
    assert not hasattr(settings, "athlete_catalog_freshness_days")


def test_settings_parse_internal_dfs_board_registry_and_bounds(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("DFS_ENABLED_PROVIDERS", "Underdog, dabble,underdog")
    monkeypatch.setenv("DFS_BOARD_DEADLINE_SECONDS", "12")
    monkeypatch.setenv("DFS_PROVIDER_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DFS_PROVIDER_READ_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DFS_DABBLE_DETAIL_CONCURRENCY", "2")

    settings = load_settings()

    assert settings.providers.dfs_enabled_providers == ("underdog", "dabble")
    assert settings.providers.dfs_board_deadline_seconds == 12.0
    assert settings.providers.dfs_provider_connect_timeout_seconds == 2.0
    assert settings.providers.dfs_provider_read_timeout_seconds == 7.0
    assert settings.providers.dfs_dabble_detail_concurrency == 2


def test_settings_parse_provider_snapshot_cache_windows(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("DFS_CACHE_FRESH_SECONDS", "301")
    monkeypatch.setenv("DFS_CACHE_STALE_IF_ERROR_SECONDS", "1801")
    monkeypatch.setenv("DFS_DABBLE_CACHE_FRESH_SECONDS", "45")
    monkeypatch.setenv("DFS_DABBLE_CACHE_STALE_IF_ERROR_SECONDS", "240")

    settings = load_settings()

    assert settings.providers.dfs_cache_fresh_seconds_for("dabble") == 45
    assert settings.providers.dfs_cache_stale_if_error_seconds_for("dabble") == 240
    assert settings.providers.dfs_cache_fresh_seconds_for("underdog") == 301
    assert settings.providers.dfs_cache_stale_if_error_seconds_for("underdog") == 1801


def test_settings_ignore_undocumented_dfs_cache_window_spellings(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("DFS_CACHE_MAX_STALE_SECONDS", "99")
    monkeypatch.setenv("DFS_CACHE_DABBLE_FRESH_SECONDS", "98")
    monkeypatch.setenv("DFS_DABBLE_SNAPSHOT_CACHE_FRESH_SECONDS", "97")
    monkeypatch.setenv("DFS_DABBLE_CACHE_MAX_STALE_SECONDS", "96")
    monkeypatch.setenv("DFS_DABBLE_SNAPSHOT_CACHE_STALE_IF_ERROR_SECONDS", "95")

    settings = load_settings()

    assert settings.providers.dfs_cache_fresh_seconds_for("dabble") == 300.0
    assert settings.providers.dfs_cache_stale_if_error_seconds_for("dabble") == 1800.0
    assert not hasattr(settings.providers, "dfs_snapshot_cache_fresh_seconds")
    assert not hasattr(settings.providers, "dfs_snapshot_cache_stale_if_error_seconds")


@pytest.mark.parametrize(
    "overrides",
    [
        {"DFS_CACHE_FRESH_SECONDS": True},
        {"DFS_CACHE_STALE_IF_ERROR_SECONDS": False},
        {"DFS_DABBLE_CACHE_FRESH_SECONDS": True},
        {"DFS_DABBLE_CACHE_STALE_IF_ERROR_SECONDS": True},
    ],
)
def test_settings_reject_boolean_dfs_cache_windows(overrides):
    # ``True`` is an int in Python, so an unguarded float() would silently
    # configure a one-second window.
    with pytest.raises(ConfigurationError):
        load_settings(environ={"FLASK_ENV": "testing"}, overrides=overrides)


@pytest.mark.parametrize(
    "window",
    [True, {"dabble": True}],
)
def test_provider_settings_reject_boolean_cache_windows(window):
    from app.config.settings import ProviderSettings

    with pytest.raises(ValueError):
        ProviderSettings(dfs_cache_fresh_seconds=window)
    with pytest.raises(ValueError):
        ProviderSettings(dfs_cache_stale_if_error_seconds=window)


def test_local_dfs_registry_defaults_to_none_and_is_not_feature_flagged(monkeypatch):
    monkeypatch.delenv("DFS_ENABLED_PROVIDERS", raising=False)
    monkeypatch.delenv("DFS_BOARD_ENABLED", raising=False)
    settings = load_settings(environ={"FLASK_ENV": "testing"})
    assert settings.providers.dfs_enabled_providers == ()
    assert not hasattr(settings.providers, "dfs_board_enabled")
    assert not hasattr(settings.providers, "enabled_dfs_providers")


def test_board_deadline_setting_cannot_exceed_fifteen_seconds():
    with pytest.raises(Exception):
        load_settings(environ={"DFS_BOARD_DEADLINE_SECONDS": "15.1"})


def test_app_startup_exposes_one_settings_object(monkeypatch):
    from app import create_app

    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")

    app = create_app({"SKIP_FIREBASE_INIT": True, "SKIP_TABLE_CREATE": True})

    assert app.extensions["runtime_settings"] is app.config["RUNTIME_SETTINGS"]
    assert "settings" not in app.extensions
    assert isinstance(app.extensions["runtime_settings"], RuntimeSettings)


def test_app_factory_isolates_request_settings_and_services(monkeypatch):
    """Each app's request defaults and services use its injected settings."""
    from app import create_app
    from app.routes import game_routes
    from app.routes import user_routes
    from app.routes._service_proxy import CurrentAppService

    first_settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        nba=NBASeasonSettings(current_season="2030-31"),
    )
    second_settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        nba=NBASeasonSettings(current_season="2040-41"),
    )
    first_dependencies = SimpleNamespace(
        settings=first_settings,
        game_service=SimpleNamespace(settings=first_settings),
        user_service=SimpleNamespace(settings=first_settings),
        player_service=Mock(),
        team_service=Mock(),
        data_service=Mock(),
        nl_service=Mock(),
        engine=Mock(),
        redis_client=None,
        nba_stats_provider=Mock(),
        pbp_stats_provider=Mock(),
    )
    second_dependencies = SimpleNamespace(
        settings=second_settings,
        game_service=SimpleNamespace(settings=second_settings),
        user_service=SimpleNamespace(settings=second_settings),
        player_service=Mock(),
        team_service=Mock(),
        data_service=Mock(),
        nl_service=Mock(),
        engine=Mock(),
        redis_client=None,
        nba_stats_provider=Mock(),
        pbp_stats_provider=Mock(),
    )
    first_app = create_app(
        {
            "RUNTIME_SETTINGS": first_settings,
            "DEPENDENCIES": first_dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    second_app = create_app(
        {
            "RUNTIME_SETTINGS": second_settings,
            "DEPENDENCIES": second_dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    assert first_app.extensions["runtime_settings"] is first_settings
    assert second_app.extensions["runtime_settings"] is second_settings

    with first_app.test_request_context(
        "/api/games/game_logs?player_name=LeBron%20James"
    ):
        _, first_query = game_routes._parse_game_log_filters()
        assert first_query.season_filter == "2030-31"
        assert game_routes.game_service.settings is first_settings

        assert isinstance(user_routes.user_service, CurrentAppService)
        first_user_service = user_routes.user_service._resolve()
        assert first_user_service.settings is first_settings

    with second_app.test_request_context(
        "/api/games/game_logs?player_name=LeBron%20James"
    ):
        _, second_query = game_routes._parse_game_log_filters()
        assert second_query.season_filter == "2040-41"
        assert game_routes.game_service.settings is second_settings

        second_user_service = user_routes.user_service._resolve()
        assert second_user_service.settings is second_settings

    assert (
        first_app.extensions["dependencies"].game_service
        is not second_app.extensions["dependencies"].game_service
    )
    assert (
        first_app.extensions["dependencies"].user_service
        is not second_app.extensions["dependencies"].user_service
    )


# -- the exact time-window domain -------------------------------------------


@pytest.mark.parametrize(
    "variable",
    ["DFS_CACHE_FRESH_SECONDS", "DFS_CACHE_STALE_IF_ERROR_SECONDS"],
)
@pytest.mark.parametrize(
    "value",
    ["1e129", "1e-200", "0", "-1", "nan", "inf", "", "not-a-number", "1000000000.000001"],
)
def test_settings_reject_window_env_values_outside_the_time_window_domain(
    variable, value
):
    with pytest.raises(ConfigurationError) as error:
        load_settings(environ={"FLASK_ENV": "testing", variable: value})

    assert variable in str(error.value)


@pytest.mark.parametrize(
    "variable",
    [
        "DFS_DABBLE_CACHE_FRESH_SECONDS",
        "DFS_DABBLE_CACHE_STALE_IF_ERROR_SECONDS",
    ],
)
def test_settings_reject_provider_overrides_outside_the_time_window_domain(variable):
    with pytest.raises(ConfigurationError) as error:
        load_settings(environ={"FLASK_ENV": "testing", variable: "1e129"})

    assert variable in str(error.value)


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("0.000001", True),
        ("0.0000009", False),
        ("1000000000", True),  # the domain ceiling, and equal to the stale ceiling
        ("1000000001", False),
        ("300", True),
    ],
)
def test_settings_accept_the_time_window_domain_boundaries(value, accepted):
    from decimal import Decimal

    environ = {
        "FLASK_ENV": "testing",
        "DFS_CACHE_FRESH_SECONDS": value,
        "DFS_CACHE_STALE_IF_ERROR_SECONDS": "1000000000",
    }
    if not accepted:
        with pytest.raises(ConfigurationError):
            load_settings(environ=environ)
        return

    settings = load_settings(environ=environ)
    assert settings.providers.dfs_cache_fresh_seconds_for("dabble") == Decimal(value)


def test_a_configured_window_keeps_every_digit_it_was_written_with():
    from decimal import Decimal

    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "DFS_CACHE_FRESH_SECONDS": "300.000001",
        }
    )

    assert settings.providers.dfs_cache_fresh_seconds_for("dabble") == Decimal(
        "300.000001"
    )


@pytest.mark.parametrize(
    "windows",
    [
        {"dfs_cache_fresh_seconds": 1801},
        {"dfs_cache_fresh_seconds": {"dabble": 1801}},
        {
            "dfs_cache_fresh_seconds": {"*": 100, "dabble": 400},
            "dfs_cache_stale_if_error_seconds": {"*": 500, "dabble": 300},
        },
    ],
)
def test_provider_settings_reject_a_fresh_window_past_the_stale_ceiling(windows):
    from app.config.settings import ProviderSettings

    with pytest.raises(ValueError):
        ProviderSettings(**windows)


def test_settings_reject_an_environment_fresh_window_past_the_stale_ceiling():
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={
                "FLASK_ENV": "testing",
                "DFS_CACHE_FRESH_SECONDS": "600",
                "DFS_DABBLE_CACHE_STALE_IF_ERROR_SECONDS": "300",
            }
        )


def test_provider_settings_accept_ordinary_windows_and_overrides():
    from decimal import Decimal

    from app.config.settings import ProviderSettings

    settings = ProviderSettings(
        dfs_cache_fresh_seconds={"*": 300, "dabble": 45},
        dfs_cache_stale_if_error_seconds={"*": 1800, "dabble": 240},
    )

    assert settings.dfs_cache_fresh_seconds_for("dabble") == Decimal(45)
    assert settings.dfs_cache_stale_if_error_seconds_for("underdog") == Decimal(1800)


@pytest.mark.parametrize("value", ["1e129", "0", "-1", "1e-200"])
def test_settings_reject_event_catalog_ttl_outside_the_time_window_domain(value):
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={"FLASK_ENV": "testing", "EVENT_CATALOG_MAX_AGE_HOURS": value}
        )


@pytest.mark.parametrize(
    ("variable", "unit_seconds"),
    [
        ("EVENT_CATALOG_MAX_AGE_HOURS", 3600),
        ("EVENT_MAPPING_MATCH_WINDOW_HOURS", 3600),
        ("PLAYER_GAME_LOG_MAX_AGE_HOURS", 3600),
        ("ATHLETE_CATALOG_FRESHNESS_DAYS", 86400),
    ],
)
@pytest.mark.parametrize(
    "value",
    ["0", "-1", "1e129", "1e-200", "nan", "inf", "true", "not-a-number"],
)
def test_settings_reject_a_catalog_window_outside_the_time_window_domain(
    variable, unit_seconds, value
):
    with pytest.raises(ConfigurationError) as error:
        load_settings(environ={"FLASK_ENV": "testing", variable: value})

    message = str(error.value)
    assert variable in message
    # The refusal names the variable and the domain, never the value read.
    assert "got" not in message
    assert value not in message.replace("0.000001", "").replace("1E+9", "")


@pytest.mark.parametrize(
    ("variable", "value", "attribute"),
    [
        (
            "EVENT_CATALOG_MAX_AGE_HOURS",
            "277777.777777777777777",
            "event_max_age_hours",
        ),
        (
            "EVENT_MAPPING_MATCH_WINDOW_HOURS",
            "0.000000000277777778",
            "event_match_window_hours",
        ),
    ],
)
def test_a_catalog_window_just_inside_the_domain_is_kept_exactly(
    variable, value, attribute
):
    from decimal import Decimal

    settings = load_settings(environ={"FLASK_ENV": "testing", variable: value})

    assert getattr(settings.catalog, attribute) == Decimal(value)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("EVENT_CATALOG_MAX_AGE_HOURS", "277777.777777777778"),
        ("EVENT_MAPPING_MATCH_WINDOW_HOURS", "0.00000000027777777"),
        ("ATHLETE_CATALOG_FRESHNESS_DAYS", "11575"),
    ],
)
def test_a_catalog_window_just_outside_the_domain_is_refused(variable, value):
    with pytest.raises(ConfigurationError):
        load_settings(environ={"FLASK_ENV": "testing", variable: value})


def test_catalog_windows_are_exact_decimals_never_floats():
    from decimal import Decimal

    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "EVENT_CATALOG_MAX_AGE_HOURS": "0.1",
            "EVENT_MAPPING_MATCH_WINDOW_HOURS": "0.3",
        }
    )

    assert isinstance(settings.catalog.event_max_age_hours, Decimal)
    assert isinstance(settings.catalog.event_match_window_hours, Decimal)
    assert settings.catalog.event_max_age_hours == Decimal("0.1")
    assert settings.catalog.event_match_window_hours == Decimal("0.3")


@pytest.mark.parametrize(
    "values",
    [
        {"event_max_age_hours": 0},
        {"event_max_age_hours": float("nan")},
        {"event_max_age_hours": True},
        {"event_match_window_hours": "1e129"},
        {"event_match_window_hours": -1},
        {"player_game_log_max_age_hours": 0},
        {"player_game_log_min_active_players_per_team_game": 0},
        {"player_game_log_min_active_players_per_team_game": True},
        {"athlete_freshness_days": 0},
        {"athlete_freshness_days": 11575},
    ],
)
def test_catalog_settings_refuse_a_direct_window_outside_the_domain(values):
    from app.config.settings import CatalogSettings

    with pytest.raises(ValueError):
        CatalogSettings(**values)


def test_dfs_board_feature_flag_is_off_by_default(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("DFS_BOARD_ENABLED", raising=False)

    settings = load_settings()

    assert settings.features.dfs_board_enabled is False


def test_projection_archive_reader_is_activation_gated(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("PROJECTION_ARCHIVE_READ_ENABLED", raising=False)
    monkeypatch.delenv("PROJECTION_ARCHIVE_READ_PROVIDER", raising=False)

    assert load_settings().features.projection_archive_read_enabled is False
    assert load_settings().features.projection_archive_read_provider == "dabble"

    monkeypatch.setenv("PROJECTION_ARCHIVE_READ_ENABLED", "true")
    monkeypatch.setenv("PROJECTION_ARCHIVE_READ_PROVIDER", "PrizePicks")

    assert load_settings().features.projection_archive_read_enabled is True
    assert load_settings().features.projection_archive_read_provider == "prizepicks"


def test_projection_archive_market_limit_is_independent_from_board_limit():
    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "DFS_COMPARISON_MAX_MARKETS": "1",
            "PROJECTION_ARCHIVE_MAX_MARKETS": "2",
        }
    )

    assert settings.providers.dfs_comparison_max_markets == 1
    assert settings.providers.projection_archive_max_markets == 2

    with pytest.raises(ConfigurationError, match="projection_archive_max_markets"):
        load_settings(
            environ={
                "FLASK_ENV": "testing",
                "PROJECTION_ARCHIVE_MAX_MARKETS": "0",
            }
        )


def test_dfs_board_feature_flag_opts_in_explicitly(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("DFS_BOARD_ENABLED", "true")
    monkeypatch.setenv("DFS_ENABLED_PROVIDERS", "prizepicks")

    settings = load_settings()

    assert settings.features.dfs_board_enabled is True
    assert settings.providers.dfs_enabled_providers == ("prizepicks",)


def test_dfs_board_feature_flag_requires_an_enabled_provider(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("DFS_BOARD_ENABLED", "true")
    monkeypatch.delenv("DFS_ENABLED_PROVIDERS", raising=False)

    with pytest.raises(ConfigurationError) as error:
        load_settings()

    assert "DFS_ENABLED_PROVIDERS" in str(error.value)


def test_production_requires_an_explicit_provider_registry(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("DFS_BOARD_ENABLED", "true")
    monkeypatch.delenv("DFS_ENABLED_PROVIDERS", raising=False)

    with pytest.raises(ConfigurationError) as error:
        load_settings()

    assert "DFS_ENABLED_PROVIDERS" in str(error.value)


def test_production_accepts_an_explicit_empty_provider_registry():
    settings = load_settings(
        environ={
            "FLASK_ENV": "production",
            "DATABASE_URL": "postgresql://example/db",
            "CORS_ALLOWED_ORIGINS": "https://courtai.app",
            "DFS_ENABLED_PROVIDERS": "",
            "FIREBASE_SERVICE_ACCOUNT_JSON": (
                '{"project_id":"courtai-test","private_key":"test-key",'
                '"client_email":"firebase@example.com"}'
            ),
        }
    )

    assert settings.providers.dfs_enabled_providers == ()
