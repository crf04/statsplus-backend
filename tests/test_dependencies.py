from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config.settings import ConfigurationError, FeatureSettings, RuntimeSettings
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


def test_production_app_factory_does_not_run_migrations_in_gunicorn_workers(monkeypatch):
    from app import create_app

    settings = RuntimeSettings(
        environment="production",
        database={"url": "postgresql://statsplus@example.invalid/statsplus"},
        auth={
            "firebase_admin_disabled": False,
            "collector_signing_secret": "test-only-signing-secret",
        },
    )
    dependencies = _fake_dependencies(settings)
    migrate = Mock()
    monkeypatch.setattr("app.models.create_all_tables", migrate)

    create_app(
        {
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
        }
    )

    migrate.assert_not_called()


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
    from app.migrations import run_migrations
    from app.services.dfs_snapshot_cache import ProviderSnapshotCache
    from app.services.statistic_catalog import StatisticCatalog, StatisticResolver

    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database={"url": "sqlite:///:memory:"},
        providers={"dfs_enabled_providers": ("dabble", "prizepicks")},
    )
    engine = create_engine("sqlite:///:memory:")
    run_migrations(engine)
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
    assert not hasattr(dependencies, "player_game_log_service")
    assert not hasattr(dependencies, "player_game_log_repository")
    assert (
        dependencies.matchup_selection_service.event_catalog
        is dependencies.event_catalog_service
    )
    selection_pool = dependencies.matchup_selection_service.player_pool
    assert selection_pool is not dependencies.slate_service.player_pool
    assert selection_pool.snapshot_repository is not None
    assert not hasattr(selection_pool, "board_service")
    assert not hasattr(selection_pool, "provider_registry")
    assert not hasattr(selection_pool, "get_pool")
    assert (
        dependencies.matchup_selection_service.player_logs.engine is dependencies.engine
    )
    assert (
        dependencies.matchup_service.event_catalog is dependencies.event_catalog_service
    )
    assert (
        dependencies.matchup_service.player_pool.snapshot_repository
        is dependencies.matchup_selection_service.player_pool.snapshot_repository
    )
    assert dependencies.matchup_service.player_diets is dependencies.player_diet_service
    assert dependencies.matchup_service.player_logs.engine is dependencies.engine
    assert (
        dependencies.matchup_service.team_matchups.repository.engine
        is dependencies.engine
    )
    assert not hasattr(dependencies.matchup_service, "nba_stats_provider")
    assert not hasattr(dependencies.matchup_service, "dfs_board_service")
    assert not hasattr(dependencies.matchup_selection_service, "nba_stats_provider")
    assert dependencies.event_resolver.match_window == time_window_timedelta(
        settings.catalog.event_match_window_hours,
        unit_seconds=3600,
        field="EVENT_MAPPING_MATCH_WINDOW_HOURS",
    )

    assert isinstance(dependencies.statistic_catalog, StatisticCatalog)
    assert board.statistic_catalog is dependencies.statistic_catalog
    assert isinstance(board.statistic_resolver, StatisticResolver)
    assert board.statistic_resolver.catalog is dependencies.statistic_catalog


def test_projection_archive_gate_selects_one_database_reader_for_every_request(monkeypatch):
    from sqlalchemy import create_engine

    from app.dependencies import build_dependencies
    from app.migrations import run_migrations
    from app.services.projection_archive import (
        LatestProjectionPlayerPoolReader,
        ProjectionArchive,
        ProjectionSelectionPlayerPoolReader,
    )

    engine = create_engine("sqlite:///:memory:")
    run_migrations(engine)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database={"url": "sqlite:///:memory:"},
        features=FeatureSettings(projection_archive_read_enabled=True),
    )

    dependencies = build_dependencies(settings)
    reader = dependencies.projection_player_pool_reader

    assert isinstance(reader, LatestProjectionPlayerPoolReader)
    assert isinstance(dependencies.projection_archive, ProjectionArchive)
    assert dependencies.projection_archive.engine is dependencies.engine
    assert dependencies.slate_service.player_pool is reader
    assert dependencies.matchup_service.player_pool is reader
    selection_reader = dependencies.matchup_selection_service.player_pool
    assert isinstance(selection_reader, ProjectionSelectionPlayerPoolReader)
    assert selection_reader.reader is reader
    assert selection_reader.get_pool_for_game(
        season=settings.nba.current_season,
        game_id="missing-game",
    ) is None
    assert reader.get_pool_for_game(
        season=settings.nba.current_season,
        game_id="missing-game",
    ).freshness["state"] == "missing"
    assert not hasattr(reader, "board_service")
    assert not hasattr(reader, "provider_registry")


def test_projection_archive_gate_refuses_the_read_only_demo_database(monkeypatch):
    from app.dependencies import build_dependencies

    monkeypatch.setattr("app.utils.db.get_engine", Mock(name="demo_engine"))
    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        features=FeatureSettings(projection_archive_read_enabled=True),
    )

    with pytest.raises(
        ConfigurationError,
        match="PROJECTION_ARCHIVE_READ_ENABLED.*demo database",
    ):
        build_dependencies(settings)


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


def test_rotowire_provider_is_constructed_only_after_both_runtime_gates(
    monkeypatch, tmp_path
):
    from sqlalchemy import create_engine

    from app.dependencies import build_dependencies
    from app.migrations import run_migrations

    engine = create_engine("sqlite:///:memory:")
    run_migrations(engine)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    constructor = Mock(return_value=Mock(name="rotowire_provider"))
    monkeypatch.setattr("app.providers.rotowire.RotoWireInjuryProvider", constructor)
    database = {"url": f"sqlite:///{tmp_path / 'application.sqlite3'}"}

    disabled = build_dependencies(
        RuntimeSettings(environment="testing", database=database)
    )
    permission_missing = build_dependencies(
        RuntimeSettings(
            environment="testing",
            database=database,
            features={"injury_report_enabled": True},
        )
    )

    assert disabled.matchup_service.injuries.provider is None
    assert permission_missing.matchup_service.injuries.provider is None
    constructor.assert_not_called()

    permitted = build_dependencies(
        RuntimeSettings(
            environment="testing",
            database=database,
            features={"injury_report_enabled": True},
            providers={"rotowire_permission_granted": True},
        )
    )

    constructor.assert_called_once_with(settings=permitted.settings)
    assert permitted.matchup_service.injuries.provider is constructor.return_value
    assert permitted.slate_service.injuries is permitted.matchup_service.injuries


def test_demo_database_never_constructs_rotowire_and_degrades_with_closed_reason(
    monkeypatch,
):
    from sqlalchemy import create_engine

    from app.dependencies import build_dependencies

    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    constructor = Mock(side_effect=AssertionError("demo database constructed provider"))
    monkeypatch.setattr("app.providers.rotowire.RotoWireInjuryProvider", constructor)
    dependencies = build_dependencies(
        RuntimeSettings(
            environment="testing",
            features={"injury_report_enabled": True},
            providers={"rotowire_permission_granted": True},
        )
    )

    result = dependencies.matchup_service.injuries.get_injuries(
        event={"nba_game_id": "1"}, season="2025-26", pool_players=()
    )

    constructor.assert_not_called()
    assert result.block["status"] == "unavailable"
    assert result.block["unavailable_reason"] == "fetch_failed"


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
