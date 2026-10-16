"""ODDS_API_* settings (#377): off by default, key from the environment only."""

from __future__ import annotations

import pytest

from app.config.settings import OddsApiSettings, load_settings


def test_defaults_leave_collection_off_without_a_key():
    odds = load_settings(overrides={"DATABASE_URL": "sqlite:///x.db"}).odds_api

    assert (odds.enabled, odds.api_key, odds.reset_day, odds.plan_credits) == (False, None, 1, 20000)
    assert (odds.reserve_credits, odds.assumed_lead_hours) == (1000, 10)
    assert odds.regions == "us_dfs"  # PrizePicks only; the sportsbook region stays off
    assert OddsApiSettings().regions == "us_dfs"


def test_environment_values_are_read():
    odds = load_settings(
        overrides={
            "DATABASE_URL": "sqlite:///x.db",
            "ODDS_API_ENABLED": "true",
            "ODDS_API_KEY": "abc123",
            "ODDS_API_RESET_DAY": "17",
            "ODDS_API_RESERVE_CREDITS": "500",
        }
    ).odds_api

    assert (odds.enabled, odds.api_key, odds.reset_day, odds.reserve_credits) == (True, "abc123", 17, 500)


@pytest.mark.parametrize("day", [0, 32])
def test_reset_day_must_be_a_calendar_day(day):
    with pytest.raises(ValueError):
        OddsApiSettings(reset_day=day)


def test_enabled_without_a_key_fails_only_the_collector_never_the_web_settings(tmp_path):
    from app.services.odds_collection import build_odds_collection

    settings = load_settings(
        overrides={"DATABASE_URL": "sqlite:///x.db", "ODDS_API_ENABLED": "true"}
    )  # loading (the web app's path) must not raise

    with pytest.raises(RuntimeError, match="ODDS_API_KEY is not set"):
        build_odds_collection(object(), settings)


def test_disabled_builds_nothing():
    from app.services.odds_collection import build_odds_collection

    settings = load_settings(overrides={"DATABASE_URL": "sqlite:///x.db"})

    assert build_odds_collection(object(), settings) is None


def test_the_region_is_read_from_the_environment_and_normalized():
    odds = load_settings(
        overrides={"DATABASE_URL": "sqlite:///x.db", "ODDS_API_REGIONS": " US "}
    ).odds_api

    assert odds.regions == "us"


@pytest.mark.parametrize("value", ["us2", "us,us_dfs", "us_ex", "all"])
def test_only_one_known_region_can_be_configured(value):
    with pytest.raises(ValueError, match="ODDS_API_REGIONS must be one of us_dfs, us"):
        OddsApiSettings(regions=value)
    with pytest.raises(Exception, match="ODDS_API_REGIONS must be one of"):
        load_settings(
            overrides={"DATABASE_URL": "sqlite:///x.db", "ODDS_API_REGIONS": value}
        )


@pytest.mark.parametrize("regions", ["us_dfs", "us"])
def test_enabled_collector_builds_from_the_real_dependency_container(monkeypatch, regions):
    """The collector composes from ``build_dependencies``, not a stand-in.

    Production's first enabled beat crashed with ``'EventCatalogService' object
    has no attribute 'engine'``: every other test handed the composer a fake
    container, so a wrong attribute path was never exercised.
    """

    from unittest.mock import Mock

    from sqlalchemy import create_engine

    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies
    from app.migrations import run_migrations
    from app.services.odds_collection import OddsCollectionCoordinator, build_odds_collection

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database={"url": "sqlite:///:memory:"},
        odds_api={"enabled": True, "api_key": "test-key", "regions": regions},
    )
    engine = create_engine("sqlite:///:memory:")
    run_migrations(engine)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    dependencies = build_dependencies(settings)

    coordinator = build_odds_collection(dependencies, settings)

    assert isinstance(coordinator, OddsCollectionCoordinator)
    assert coordinator.engine is engine
