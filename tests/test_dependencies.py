from types import SimpleNamespace
from unittest.mock import Mock

from app.config.settings import RuntimeSettings


def _fake_dependencies(settings: RuntimeSettings):
    return SimpleNamespace(
        settings=settings,
        engine=Mock(name="engine"),
        redis_client=None,
        nba_stats_provider=Mock(name="nba_stats_provider"),
        pbp_stats_provider=Mock(name="pbp_stats_provider"),
        dabble_provider=Mock(name="dabble_provider"),
        game_service=Mock(name="game_service"),
        player_service=Mock(name="player_service"),
        team_service=Mock(name="team_service"),
        data_service=Mock(name="data_service"),
        dabble_service=Mock(name="dabble_service"),
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
        "app.routes.dabble_routes",
        "app.routes.game_routes",
        "app.routes.health_routes",
        "app.routes.nl_routes",
        "app.routes.player_routes",
        "app.routes.team_routes",
        "app.routes.user_routes",
    ):
        importlib.reload(importlib.import_module(module_name))
