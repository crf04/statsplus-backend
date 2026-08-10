from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config.settings import RuntimeSettings
from app.domain.freshness import time_window_timedelta


def _fake_dependencies(settings: RuntimeSettings):
    return SimpleNamespace(
        settings=settings,
        engine=Mock(name="engine"),
        redis_client=None,
        nba_stats_provider=Mock(name="nba_stats_provider"),
        pbp_stats_provider=Mock(name="pbp_stats_provider"),
        game_service=Mock(name="game_service"),
        player_service=Mock(name="player_service"),
        team_service=Mock(name="team_service"),
        data_service=Mock(name="data_service"),
        nl_service=Mock(name="nl_service"),
        user_service=Mock(name="user_service"),
    )


def test_app_factory_constructs_one_dependency_container(monkeypatch):
    from app import create_app

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
    )
    dependencies = _fake_dependencies(settings)
    builder = Mock(return_value=dependencies)
    monkeypatch.setattr("app.dependencies.build_dependencies", builder)

    application = create_app(
        {
            "RUNTIME_SETTINGS": settings,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    builder.assert_called_once_with(settings)
    assert application.extensions["dependencies"] is dependencies


def test_routes_use_injected_dependencies_without_global_patching():
    from app import create_app

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
    )
    dependencies = _fake_dependencies(settings)
    dependencies.team_service.get_all_teams.return_value = ["Chicago Bulls"]

    application = create_app(
        {
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    response = application.test_client().get("/api/teams")

    assert response.status_code == 200
    assert response.get_json() == ["Chicago Bulls"]
    dependencies.team_service.get_all_teams.assert_called_once_with()


def test_board_receives_cached_providers_governed_mappings_and_the_catalog(monkeypatch):
    """The one board service must carry every reviewed feature at once.

    Issue #24's cache seam, issue #26's governed athlete mappings, issue #29's
    statistic catalog, and issue #28's governed event mappings all assemble the
    same board service, so a wiring change that keeps only some of them still
    leaves every focused unit test green.
    Pin the combination here: the board reads through the snapshot caches that
    carry cache telemetry, holds both sets of governed mapping collaborators, and
    resolves statistics against the catalog the factory loaded.
    """

    from sqlalchemy import create_engine

    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies
    from app.services.dfs_snapshot_cache import ProviderSnapshotCache
    from app.services.statistic_catalog import StatisticCatalog, StatisticResolver

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database={"url": "sqlite:///:memory:"},
        providers={"dfs_enabled_providers": ("dabble", "prizepicks")},
    )
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))

    dependencies = build_dependencies(settings)
    board = dependencies.dfs_board_service

    assert set(board.provider_registry) == {"dabble", "prizepicks"}
    for provider in board.provider_registry.values():
        assert isinstance(provider, ProviderSnapshotCache)
        assert callable(getattr(provider, "get_last_result", None))
    assert dependencies.dfs_snapshot_cache is not None

    assert board.athlete_resolver is dependencies.athlete_resolver
    assert board.athlete_mapping_repository is dependencies.athlete_mapping_repository
    assert dependencies.athlete_resolver is not None
    assert dependencies.athlete_mapping_repository is not None

    assert board.event_resolver is dependencies.event_resolver
    assert board.event_mapping_repository is dependencies.event_mapping_repository
    assert dependencies.event_resolver is not None
    assert dependencies.event_mapping_repository is not None
    assert dependencies.event_catalog_service is not None
    assert dependencies.event_resolver.catalog is dependencies.event_catalog_service
    assert dependencies.player_game_log_service is not None
    assert dependencies.player_game_log_repository is not None
    assert (
        dependencies.player_game_log_service.repository
        is dependencies.player_game_log_repository
    )
    assert dependencies.event_resolver.match_window == time_window_timedelta(
        settings.catalog.event_match_window_hours,
        unit_seconds=3600,
        field="EVENT_MAPPING_MATCH_WINDOW_HOURS",
    )

    assert isinstance(dependencies.statistic_catalog, StatisticCatalog)
    assert board.statistic_catalog is dependencies.statistic_catalog
    assert isinstance(board.statistic_resolver, StatisticResolver)
    assert board.statistic_resolver.catalog is dependencies.statistic_catalog


def test_route_imports_do_not_construct_runtime_dependencies(monkeypatch):
    import importlib

    monkeypatch.setattr(
        "app.utils.db.get_engine",
        Mock(side_effect=AssertionError("route import selected a database engine")),
    )
    monkeypatch.setattr(
        "app.utils.cache_config.get_redis_client",
        Mock(side_effect=AssertionError("route import connected to Redis")),
    )

    for module_name in (
        "app.routes.data_update_routes",
        "app.routes.game_routes",
        "app.routes.health_routes",
        "app.routes.nl_routes",
        "app.routes.player_routes",
        "app.routes.team_routes",
        "app.routes.user_routes",
    ):
        importlib.reload(importlib.import_module(module_name))


def test_dependency_assembly_validates_catalog_before_provider_construction(monkeypatch):
    from app.dependencies import build_dependencies

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
    )
    constructor = Mock(side_effect=AssertionError("provider constructed before catalog"))
    monkeypatch.setattr("app.providers.dabble.DabbleAdapter", constructor)
    loader = Mock(side_effect=ValueError("invalid statistic schema"))

    with pytest.raises(ValueError, match="invalid statistic schema"):
        build_dependencies(settings, statistic_catalog_path="invalid.yaml", statistic_catalog_loader=loader)

    loader.assert_called_once_with("invalid.yaml")
    constructor.assert_not_called()


def test_dependency_assembly_fails_fast_on_malformed_catalog_yaml(monkeypatch, tmp_path):
    from app.dependencies import build_dependencies
    from app.services.statistic_catalog import StatisticCatalogError

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
    )
    constructor = Mock(side_effect=AssertionError("provider constructed before catalog"))
    monkeypatch.setattr("app.providers.dabble.DabbleAdapter", constructor)
    definition_path = tmp_path / "unhashable-key-statistics.yaml"
    definition_path.write_text("schema_version: 1\n? [points, assists]\n: value\n", encoding="utf-8")

    with pytest.raises(StatisticCatalogError, match="could not be loaded"):
        build_dependencies(settings, statistic_catalog_path=definition_path)

    constructor.assert_not_called()
