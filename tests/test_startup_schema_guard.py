"""Boot-time schema-drift guard behavior.

The guard must fail closed for a real deployment database that is behind the
code's migration head, while staying completely inert for the offline test
suite (the testing environment, the demo fixture, and app-construction paths
whose engine records no migration head).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.config.settings import DatabaseSettings, RuntimeSettings
from app.migrations import MIGRATIONS, expected_schema_version, run_migrations
from app.startup_schema_guard import (
    ALLOW_SCHEMA_DRIFT_ENV,
    SchemaBehindError,
    verify_schema_is_current,
)


def _production_settings(url: str) -> RuntimeSettings:
    return RuntimeSettings(
        environment="production", database=DatabaseSettings(url=url)
    )


def _migrate_to(url: str, *, versions_below: int = 0) -> None:
    """Migrate ``url`` to head, optionally leaving it ``versions_below`` behind."""
    engine = create_engine(url)
    truncated = (
        MIGRATIONS[: len(MIGRATIONS) - versions_below]
        if versions_below
        else MIGRATIONS
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.migrations.MIGRATIONS", truncated)
        run_migrations(engine)
    engine.dispose()


def test_guard_raises_when_real_database_is_behind_code(tmp_path):
    url = f"sqlite:///{tmp_path / 'behind.sqlite3'}"
    _migrate_to(url, versions_below=1)
    engine = create_engine(url)

    with pytest.raises(SchemaBehindError) as error:
        verify_schema_is_current(engine, _production_settings(url), env={})

    message = str(error.value)
    assert str(expected_schema_version()) in message
    assert str(expected_schema_version() - 1) in message
    assert "scripts/migrate.py" in message
    engine.dispose()


def test_guard_passes_when_head_matches_expected(tmp_path):
    url = f"sqlite:///{tmp_path / 'current.sqlite3'}"
    _migrate_to(url)
    engine = create_engine(url)

    verify_schema_is_current(engine, _production_settings(url), env={})
    engine.dispose()


def test_guard_passes_when_database_is_ahead_of_code(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'ahead.sqlite3'}"
    _migrate_to(url)
    engine = create_engine(url)

    # An old worker whose code expects a lower head than the migrated database
    # must still boot: only a database strictly behind the code is fatal.
    monkeypatch.setattr(
        "app.startup_schema_guard.expected_schema_version",
        lambda: expected_schema_version() - 1,
    )
    verify_schema_is_current(engine, _production_settings(url), env={})
    engine.dispose()


def test_guard_is_inert_in_testing_environment(tmp_path):
    url = f"sqlite:///{tmp_path / 'behind.sqlite3'}"
    _migrate_to(url, versions_below=1)
    engine = create_engine(url)

    settings = RuntimeSettings(
        environment="testing", database=DatabaseSettings(url=url)
    )
    verify_schema_is_current(engine, settings, env={})
    engine.dispose()


def test_guard_is_inert_for_demo_database():
    settings = RuntimeSettings(
        environment="production",
        database=DatabaseSettings(url="sqlite:///nba_play_types.db"),
    )
    # The demo fixture is exempt before any inspection, so a Mock engine that
    # would raise on inspection proves the guard short-circuits on it.
    verify_schema_is_current(Mock(name="engine"), settings, env={})


def test_guard_is_inert_when_engine_records_no_migration_head():
    # The offline suite builds production apps with a Mock engine; an engine
    # that cannot be inspected records no head, so the guard must not engage.
    settings = _production_settings("postgresql://statsplus@example.invalid/db")
    verify_schema_is_current(Mock(name="engine"), settings, env={})


def test_guard_is_inert_when_migration_table_absent(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite3'}"
    engine = create_engine(url)  # never migrated: no schema_migrations table

    verify_schema_is_current(engine, _production_settings(url), env={})
    engine.dispose()


def test_create_app_fails_closed_when_production_schema_is_behind(
    tmp_path, monkeypatch
):
    """The boot wiring runs the guard: a behind-schema production DB cannot boot."""
    from types import SimpleNamespace

    from app import create_app

    monkeypatch.setenv("FLASK_ENV", "production")
    url = f"sqlite:///{tmp_path / 'behind.sqlite3'}"
    _migrate_to(url, versions_below=1)
    engine = create_engine(url)
    settings = _production_settings(url)
    dependencies = SimpleNamespace(settings=settings, engine=engine)

    with pytest.raises(SchemaBehindError):
        create_app(
            {
                "RUNTIME_SETTINGS": settings,
                "DEPENDENCIES": dependencies,
                "SKIP_FIREBASE_INIT": True,
                "SKIP_TABLE_CREATE": True,
            }
        )
    engine.dispose()


def test_override_downgrades_behind_schema_to_a_warning(tmp_path):
    url = f"sqlite:///{tmp_path / 'behind.sqlite3'}"
    _migrate_to(url, versions_below=1)
    engine = create_engine(url)

    verify_schema_is_current(
        engine,
        _production_settings(url),
        env={ALLOW_SCHEMA_DRIFT_ENV: "true"},
    )
    engine.dispose()
