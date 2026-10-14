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


def _web_app(url: str, dependencies=None, **overrides):
    settings = load_settings(
        overrides={"DATABASE_URL": url, "FLASK_ENV": "testing", **overrides}
    )
    return create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies or SimpleNamespace(settings=settings),
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


# --- work that legitimately runs long keeps no limit --------------------------

SLOW_SECONDS = 1.5
LIMIT_MS = 300


def _sleep_through(engine) -> str:
    """Run a statement longer than the limit; return the timeout it ran under."""

    with engine.connect() as connection:
        connection.execute(text(f"SELECT pg_sleep({SLOW_SECONDS})"))
        return connection.execute(text("SHOW statement_timeout")).scalar_one()


@pytest.fixture
def admin_headers(monkeypatch):
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": "admin-uid", "email": "a@example.com", "admin": True},
    )
    monkeypatch.setattr(
        auth,
        "get_dependencies",
        lambda: SimpleNamespace(
            user_service=SimpleNamespace(create_or_update_user=lambda data: None)
        ),
    )
    return {"Authorization": "Bearer test-token"}


def test_an_admin_collection_request_runs_past_the_limit(
    postgres_url, admin_headers
):
    dependencies = SimpleNamespace()
    app = _web_app(postgres_url, dependencies, WEB_STATEMENT_TIMEOUT_MS=LIMIT_MS)
    engine = get_engine(app.extensions["runtime_settings"])
    dependencies.collection_operations = SimpleNamespace(
        diagnostics=lambda limit: {"statement_timeout": _sleep_through(engine)}
    )

    response = app.test_client().get(
        "/api/admin/collection/diagnostics", headers=admin_headers
    )

    assert response.status_code == 200
    assert response.get_json() == {"statement_timeout": "0"}


def test_a_collector_request_runs_past_the_limit(postgres_url):
    dependencies = SimpleNamespace()
    app = _web_app(postgres_url, dependencies, WEB_STATEMENT_TIMEOUT_MS=LIMIT_MS)
    engine = get_engine(app.extensions["runtime_settings"])
    dependencies.collector_tokens = SimpleNamespace(
        validate=lambda token, required_scope=None: SimpleNamespace(
            scopes={"poll", "bootstrap", "catalog_publish"}, collector_id="c1"
        )
    )
    dependencies.collection_operations = SimpleNamespace(
        record_usage=lambda collector_id, polls: None
    )
    dependencies.collection_control = SimpleNamespace(
        discover=lambda claims, limit: {"statement_timeout": _sleep_through(engine)}
    )

    response = app.test_client().get(
        "/api/collector/discovery", headers={"Authorization": "Bearer token"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"statement_timeout": "0"}


def test_an_admin_data_request_runs_past_the_limit(postgres_url, admin_headers):
    dependencies = SimpleNamespace()
    app = _web_app(postgres_url, dependencies, WEB_STATEMENT_TIMEOUT_MS=LIMIT_MS)
    engine = get_engine(app.extensions["runtime_settings"])
    dependencies.data_service = SimpleNamespace(
        get_playtypes=lambda: {"statement_timeout": _sleep_through(engine)}
    )

    response = app.test_client().get(
        "/api/data/fetch_playtypes", headers=admin_headers
    )

    assert response.status_code == 200
    assert response.get_json() == {"statement_timeout": "0"}


def test_a_normal_user_route_still_gets_a_query_timeout(postgres_url, monkeypatch):
    import app.utils.auth as auth
    from app.routes import user_routes

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": "u", "email": "u@example.com"},
    )
    monkeypatch.setattr(
        auth,
        "get_dependencies",
        lambda: SimpleNamespace(
            user_service=SimpleNamespace(
                create_or_update_user=lambda data: SimpleNamespace()
            )
        ),
    )
    app = _web_app(postgres_url, WEB_STATEMENT_TIMEOUT_MS=LIMIT_MS)
    engine = get_engine(app.extensions["runtime_settings"])
    monkeypatch.setattr(
        user_routes,
        "user_service",
        SimpleNamespace(get_user_stats=lambda uid: _sleep_through(engine)),
    )

    response = app.test_client().get(
        "/api/user/stats", headers={"Authorization": "Bearer test-token"}
    )

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "query_timeout"


def test_the_limit_returns_after_exempt_requests_on_a_reused_connection(
    postgres_url, admin_headers
):
    dependencies = SimpleNamespace()
    app = _web_app(postgres_url, dependencies, WEB_STATEMENT_TIMEOUT_MS=LIMIT_MS)
    engine = get_engine(app.extensions["runtime_settings"])
    dependencies.data_service = SimpleNamespace(
        get_playtypes=lambda: {"statement_timeout": _sleep_through(engine)}
    )
    client = app.test_client()

    first = client.get("/api/data/fetch_playtypes", headers=admin_headers)
    second = client.get("/api/data/fetch_playtypes", headers=admin_headers)

    assert first.get_json() == {"statement_timeout": "0"}
    assert second.get_json() == {"statement_timeout": "0"}
    assert _show(engine) == "300ms"


def _show(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SHOW statement_timeout")).scalar_one()


def test_a_refresh_job_runs_past_the_limit(migrated_postgres_url):
    from app.services.job_service import DataRefreshJobService, SynchronousExecutor

    settings = load_settings(
        overrides={
            "DATABASE_URL": migrated_postgres_url,
            "FLASK_ENV": "testing",
            "WEB_STATEMENT_TIMEOUT_MS": LIMIT_MS,
        }
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(settings=settings),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    engine = get_engine(app.extensions["runtime_settings"])
    ran_under = []

    def handler(*, progress_callback, publication_fence=None):
        ran_under.append(_sleep_through(engine))
        return True

    jobs = DataRefreshJobService(
        engine,
        executor=SynchronousExecutor(),
        handlers={"update_database": handler},
        settings=settings,
        dispatch_on_startup=False,
        start_poller=False,
    )

    state = jobs.start("update_database")

    assert ran_under == ["0"]
    assert jobs.get(state["job_id"])["status"] == "succeeded"
