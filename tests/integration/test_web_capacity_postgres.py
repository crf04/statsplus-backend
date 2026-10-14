"""The web engine limits statements; every other engine stays unlimited.

Skipped unless ``TEST_DATABASE_URL`` names a disposable Postgres database.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app import create_app
from app.config.settings import load_settings
from app.dependencies import build_dependencies
from app.migrations import run_migrations
from app.models import Base
from app.utils.db import _normalize_database_url, get_engine

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    get_engine.cache_clear()
    yield url
    get_engine.cache_clear()


def _web_app(url: str, **overrides):
    settings = load_settings(
        overrides={"DATABASE_URL": url, "FLASK_ENV": "testing", **overrides}
    )
    return create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(settings=settings),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )


def _show_statement_timeout(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SHOW statement_timeout")).scalar_one()


def test_web_engine_limits_every_statement_to_thirty_seconds(postgres_url):
    app = _web_app(postgres_url)

    engine = get_engine(app.extensions["runtime_settings"])

    assert _show_statement_timeout(engine) == "30s"


def test_web_statement_timeout_follows_the_environment_setting(postgres_url):
    app = _web_app(postgres_url, WEB_STATEMENT_TIMEOUT_MS=2500)

    engine = get_engine(app.extensions["runtime_settings"])

    assert _show_statement_timeout(engine) == "2500ms"


def test_a_statement_past_the_timeout_is_a_503_query_timeout(postgres_url):
    app = _web_app(postgres_url, WEB_STATEMENT_TIMEOUT_MS=300)
    engine = get_engine(app.extensions["runtime_settings"])

    @app.get("/test-slow-query")
    def slow_query():
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_sleep(5)"))
        return "finished", 200

    response = app.test_client().get("/test-slow-query")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "query_timeout",
            "message": "The request took too long to answer. Try a narrower query.",
        }
    }


@pytest.fixture
def migrated_postgres_url(postgres_url):
    engine = create_engine(postgres_url)

    def reset():
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")

    reset()
    run_migrations(engine)
    yield postgres_url
    reset()
    engine.dispose()


def test_collector_dependencies_engine_has_no_statement_timeout(migrated_postgres_url):
    postgres_url = migrated_postgres_url
    settings = load_settings(overrides={"DATABASE_URL": postgres_url})

    dependencies = build_dependencies(settings)

    assert _show_statement_timeout(dependencies.engine) == "0"


class _EngineBuilt(Exception):
    pass


@pytest.mark.parametrize("module_name", ["scripts.nightly_refresh", "scripts.migrate"])
def test_command_engines_have_no_statement_timeout(
    postgres_url, monkeypatch, module_name
):
    import importlib

    module = importlib.import_module(module_name)
    built = []

    def record_then_stop(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        built.append(engine)
        raise _EngineBuilt

    monkeypatch.setattr(module, "create_engine", record_then_stop)

    with pytest.raises(_EngineBuilt):
        module._run(postgres_url)

    assert module.create_engine is record_then_stop
    assert _show_statement_timeout(built[0]) == "0"
    assert str(built[0].url) == str(
        create_engine(_normalize_database_url(postgres_url)).url
    )
