"""Postgres engines apply pool configuration; SQLite engines stay unchanged."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.config.settings import DatabaseSettings, RuntimeSettings
from app.utils.db import get_engine


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    get_engine.cache_clear()
    yield
    get_engine.cache_clear()


def _postgres_settings(**overrides) -> RuntimeSettings:
    return RuntimeSettings(
        database=DatabaseSettings(
            url="postgresql://user:pass@localhost:5432/db", **overrides
        )
    )


def test_postgres_engine_applies_configured_pool_settings():
    settings = _postgres_settings(
        pool_size=7, max_overflow=9, pool_recycle_seconds=120
    )

    engine = get_engine(settings)

    assert engine.pool._pre_ping is True
    assert engine.pool.size() == 7
    assert engine.pool._max_overflow == 9
    assert engine.pool._recycle == 120


def test_postgres_engine_defaults_to_the_documented_pool_settings():
    engine = get_engine(_postgres_settings())

    assert engine.pool._pre_ping is True
    assert engine.pool.size() == 3
    assert engine.pool._max_overflow == 4
    assert engine.pool._recycle == 300


def test_postgres_engine_passes_the_configured_connect_timeout(monkeypatch):
    mock_create_engine = Mock()
    monkeypatch.setattr("app.utils.db.create_engine", mock_create_engine)

    get_engine(_postgres_settings(connect_timeout_seconds=17))

    _, kwargs = mock_create_engine.call_args
    assert kwargs["connect_args"] == {"connect_timeout": 17}
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 3
    assert kwargs["max_overflow"] == 4
    assert kwargs["pool_recycle"] == 300


def test_sqlite_engine_is_built_with_no_extra_keyword_arguments(monkeypatch, tmp_path):
    mock_create_engine = Mock()
    monkeypatch.setattr("app.utils.db.create_engine", mock_create_engine)

    settings = RuntimeSettings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'app.sqlite3'}")
    )
    get_engine(settings)

    args, kwargs = mock_create_engine.call_args
    assert args == (settings.database.url,)
    assert kwargs == {}


def test_engine_cache_key_includes_pool_parameters():
    settings_a = _postgres_settings(pool_size=3)
    settings_b = _postgres_settings(pool_size=8)

    engine_a = get_engine(settings_a)
    engine_b = get_engine(settings_b)

    assert engine_a is not engine_b
    assert engine_a.pool.size() == 3
    assert engine_b.pool.size() == 8
