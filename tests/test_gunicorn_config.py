"""Offline tests for the committed pre-forking Gunicorn configuration.

The hooks are exercised with fakes; no server is started and no socket is
opened.
"""

from __future__ import annotations

import gc
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.services.job_service import DEFER_DISPATCHER_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "gunicorn.conf.py"


def _load_config(monkeypatch, port: str | None = "8123"):
    if port is None:
        monkeypatch.delenv("PORT", raising=False)
    else:
        monkeypatch.setenv("PORT", port)
    # Loading the module sets the deferral switch before Gunicorn preloads the
    # app; registering it here lets monkeypatch restore the variable afterwards.
    monkeypatch.setenv(DEFER_DISPATCHER_ENV, "")
    spec = importlib.util.spec_from_file_location("statsplus_gunicorn_conf", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_preloads_the_app_with_the_production_worker_settings(monkeypatch):
    config = _load_config(monkeypatch)

    assert config.preload_app is True
    assert config.wsgi_app == "wsgi:app"
    assert config.bind == ["0.0.0.0:8123"]
    assert config.workers == 4
    assert config.threads == 2
    assert config.timeout == 180
    assert config.keepalive == 5
    assert config.max_requests == 1000
    assert config.max_requests_jitter == 100
    assert config.control_socket_disable is True


def test_config_binds_the_default_port_without_platform_port(monkeypatch):
    config = _load_config(monkeypatch, port=None)

    assert config.bind == ["0.0.0.0:8000"]


def test_loading_the_config_defers_the_dispatcher_until_after_fork(monkeypatch):
    _load_config(monkeypatch)

    assert os.environ[DEFER_DISPATCHER_ENV] == "1"


def test_procfile_web_process_uses_the_committed_config():
    procfile = (REPO_ROOT / "Procfile").read_text().splitlines()
    web = next(line for line in procfile if line.startswith("web:"))

    assert web == "web: gunicorn --config gunicorn.conf.py wsgi:app"


def _fake_server(dependencies):
    application = SimpleNamespace(extensions={"dependencies": dependencies})
    return SimpleNamespace(app=SimpleNamespace(wsgi=lambda: application))


def test_post_fork_disposes_inherited_pool_then_starts_the_dispatcher(monkeypatch):
    config = _load_config(monkeypatch)
    calls = Mock()
    dependencies = SimpleNamespace(
        engine=calls.engine,
        data_refresh_jobs_service=calls.jobs,
    )

    config.post_fork(_fake_server(dependencies), SimpleNamespace(pid=4242))

    assert calls.mock_calls == [
        # The master's pooled DBAPI connections must never be reused here.
        call.engine.dispose(close=False),
        call.jobs.start_dispatcher(),
    ]
    # The worker's own children (if any) should start their own dispatcher.
    assert DEFER_DISPATCHER_ENV not in os.environ


def test_when_ready_closes_master_connections_then_freezes(monkeypatch):
    config = _load_config(monkeypatch)
    calls = Mock()
    monkeypatch.setattr(gc, "freeze", calls.freeze)
    dependencies = SimpleNamespace(engine=calls.engine)

    config.when_ready(_fake_server(dependencies))

    assert calls.mock_calls == [call.engine.dispose(), call.freeze()]


@pytest.mark.parametrize("missing", ["engine", "data_refresh_jobs_service"])
def test_post_fork_tolerates_a_graph_without_engine_or_jobs(monkeypatch, missing):
    config = _load_config(monkeypatch)
    calls = Mock()
    present = {
        "engine": calls.engine,
        "data_refresh_jobs_service": calls.jobs,
    }
    present.pop(missing)

    config.post_fork(_fake_server(SimpleNamespace(**present)), SimpleNamespace())

    assert len(calls.mock_calls) == 1


def test_post_fork_starts_sentry_in_the_worker_when_a_dsn_is_set(monkeypatch):
    import sentry_sdk

    config = _load_config(monkeypatch)
    monkeypatch.setenv("SENTRY_DSN", "https://publickey@o0.ingest.sentry.io/0")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    sentry_sdk.get_global_scope().set_client(None)
    assert sentry_sdk.get_client().is_active() is False

    try:
        config.post_fork(_fake_server(SimpleNamespace()), SimpleNamespace())

        assert sentry_sdk.get_client().is_active() is True
        assert sentry_sdk.get_client().options["release"] == "abc123"
    finally:
        sentry_sdk.get_client().close(timeout=0)
        sentry_sdk.get_global_scope().set_client(None)


def test_master_hooks_never_start_sentry(monkeypatch):
    import sentry_sdk

    config = _load_config(monkeypatch)
    monkeypatch.setenv("SENTRY_DSN", "https://publickey@o0.ingest.sentry.io/0")
    sentry_sdk.get_global_scope().set_client(None)
    monkeypatch.setattr(gc, "freeze", Mock())

    config.when_ready(_fake_server(SimpleNamespace()))

    assert sentry_sdk.get_client().is_active() is False


def test_post_fork_leaves_sentry_off_without_a_dsn(monkeypatch):
    import sentry_sdk

    config = _load_config(monkeypatch)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    sentry_sdk.get_global_scope().set_client(None)

    config.post_fork(_fake_server(SimpleNamespace()), SimpleNamespace())

    assert sentry_sdk.get_client().is_active() is False
