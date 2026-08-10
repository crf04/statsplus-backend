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
