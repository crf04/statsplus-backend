"""The migrated-template shortcut in tests/conftest.py is indistinguishable from
running every migration.

If a migration ever produces state the backup copy cannot reproduce, these
tests fail rather than letting the rest of the suite run against a schema that
production would never have.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

import app.migrations as migrations
from app.migrations import expected_schema_version
from tests.support import migration_template

_TIMESTAMP = re.compile(r"'\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(\.\d+)?([+-]\d\d:\d\d)?'")
_PRAGMAS = ("user_version", "page_size", "journal_mode", "auto_vacuum", "encoding")


def _snapshot(path):
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        # Only schema_migrations.applied_at records when the copy was migrated.
        dump = [
            _TIMESTAMP.sub("'<applied_at>'", line)
            if line.startswith('INSERT INTO "schema_migrations"')
            else line
            for line in connection.iterdump()
        ]
        pragmas = {
            pragma: connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            for pragma in _PRAGMAS
        }
    finally:
        connection.close()
    return schema, dump, pragmas


def _restored(engine):
    before = migration_template.calls["template"]
    result = migrations.run_migrations(engine)
    assert migration_template.calls["template"] == before + 1, "template was not used"
    return result


def test_restored_database_matches_a_freshly_migrated_database(tmp_path):
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    restored = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    try:
        fresh_result = migration_template.REAL_RUN_MIGRATIONS(fresh)
        restored_result = _restored(restored)

        assert restored_result == fresh_result
        assert restored_result.current_version == expected_schema_version()
        assert len(restored_result.applied) == len(migrations.MIGRATIONS)

        fresh_schema, fresh_dump, fresh_pragmas = _snapshot(tmp_path / "fresh.db")
        schema, dump, pragmas = _snapshot(tmp_path / "restored.db")
        assert len(fresh_schema) > len(migrations.MIGRATIONS)
        assert schema == fresh_schema
        assert dump == fresh_dump
        assert pragmas == fresh_pragmas

        # Re-running the real migrations on the copy finds nothing to apply.
        rerun = migration_template.REAL_RUN_MIGRATIONS(restored)
        assert rerun.applied == ()
        assert rerun.current_version == fresh_result.current_version
        assert _snapshot(tmp_path / "restored.db")[:2] == (schema, dump)
    finally:
        fresh.dispose()
        restored.dispose()


def test_in_memory_database_is_restored_from_the_template():
    engine = create_engine("sqlite://")
    try:
        result = _restored(engine)
        assert result.current_version == expected_schema_version()
        with engine.connect() as connection:
            head = connection.execute(
                text("SELECT max(version) FROM schema_migrations")
            ).scalar_one()
        assert head == expected_schema_version()
    finally:
        engine.dispose()


def test_populated_database_runs_real_migrations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'populated.db'}")
    try:
        _restored(engine)
        before = migration_template.calls["real"]
        assert migrations.run_migrations(engine).applied == ()
        assert migration_template.calls["real"] == before + 1
    finally:
        engine.dispose()


def test_patched_migrations_module_runs_real_migrations(tmp_path, monkeypatch):
    head = migrations.MIGRATIONS[:1]
    monkeypatch.setattr(migrations, "MIGRATIONS", head)
    engine = create_engine(f"sqlite:///{tmp_path / 'patched.db'}")
    try:
        result = migrations.run_migrations(engine)
        assert result.applied == (head[0].name,)
        assert result.current_version == head[0].version
    finally:
        engine.dispose()


def test_observed_engine_runs_real_migrations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'observed.db'}")
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, *_args: statements.append(statement),
    )
    try:
        before = migration_template.calls["real"]
        migrations.run_migrations(engine)
        assert migration_template.calls["real"] == before + 1
        assert any("schema_migrations" in statement for statement in statements)
    finally:
        engine.dispose()


def test_non_default_page_size_runs_real_migrations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paged.db'}")

    @event.listens_for(engine.pool, "connect")
    def _page_size(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA page_size = 8192")

    try:
        before = migration_template.calls["real"]
        migrations.run_migrations(engine)
        assert migration_template.calls["real"] == before + 1
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA page_size").scalar_one() == 8192
    finally:
        engine.dispose()


def test_locked_target_reports_contention_like_real_migrations(tmp_path):
    path = tmp_path / "locked.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        before = migration_template.calls["real"]
        with pytest.raises(OperationalError, match="locked"):
            migrations.run_migrations(engine)
        assert migration_template.calls["real"] == before + 1
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()


@pytest.mark.real_migrations
def test_real_migrations_marker_bypasses_the_template(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'marked.db'}")
    try:
        before = dict(migration_template.calls)
        migrations.run_migrations(engine)
        assert migration_template.calls["template"] == before["template"]
        assert migration_template.calls["real"] == before["real"] + 1
    finally:
        engine.dispose()
