"""Tests for the repeatable application-schema migration workflow."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from scripts import migrate
from scripts.validate_demo_db import validate_demo_database


def test_run_migrations_creates_current_schema_from_empty_database(tmp_path):
    database_path = tmp_path / "fresh.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.applied == ("001_create_users",)
    assert second.applied == ()
    assert inspect(engine).get_table_names() == ["schema_migrations", "users"]
    assert {
        column["name"] for column in inspect(engine).get_columns("users")
    } == {
        "firebase_uid",
        "email",
        "display_name",
        "photo_url",
        "created_at",
        "last_login",
        "is_active",
    }

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version, name FROM schema_migrations")
        ).all() == [(1, "001_create_users")]


def test_run_migrations_upgrades_existing_app_database(tmp_path):
    database_path = tmp_path / "existing.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")

    from app.models import Base

    Base.metadata.create_all(engine)

    result = run_migrations(engine)

    assert result.applied == ("001_create_users",)
    assert inspect(engine).has_table("users")


def test_demo_database_validation_is_read_only():
    database_path = "nba_play_types.db"
    with open(database_path, "rb") as database_file:
        before = hashlib.sha256(database_file.read()).digest()

    result = validate_demo_database(database_path)

    with open(database_path, "rb") as database_file:
        after = hashlib.sha256(database_file.read()).digest()

    assert result.valid
    assert result.user_count == 0
    assert before == after


def test_demo_database_validation_reports_missing_tables(tmp_path):
    database_path = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE users (firebase_uid TEXT)")

    result = validate_demo_database(database_path)

    assert not result.valid
    assert any("missing required table" in issue for issue in result.issues)


def test_migration_cli_requires_database_target(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as error:
        migrate.main([])

    assert error.value.code == 2
    error_output = capsys.readouterr().err
    assert "--database-url" in error_output
    assert "DATABASE_URL" in error_output


def test_migration_cli_rejects_demo_database(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        migrate,
        "_run",
        lambda _: pytest.fail("the demo fixture must be rejected before migration"),
    )

    with pytest.raises(SystemExit) as error:
        migrate.main(["--database-url", "sqlite:///nba_play_types.db"])

    assert error.value.code == 2
    assert "read-only demo database" in capsys.readouterr().err


def test_migration_cli_accepts_database_url_environment_variable(monkeypatch):
    database_url = "sqlite:////tmp/statsplus-migrations.sqlite3"
    monkeypatch.setenv("DATABASE_URL", database_url)
    observed_urls = []
    monkeypatch.setattr(
        migrate,
        "_run",
        lambda url: (
            observed_urls.append(url),
            migrate.MigrationResult(applied=(), current_version=1),
        )[1],
    )

    assert migrate.main([]) == 0
    assert observed_urls == [database_url]


def test_migration_cli_redacts_database_password(monkeypatch, capsys):
    database_url = "postgresql://migration_user:super-secret@example.invalid/stats"
    monkeypatch.setattr(
        migrate,
        "_run",
        lambda _: migrate.MigrationResult(
            applied=("001_create_users",), current_version=1
        ),
    )

    assert migrate.main(["--database-url", database_url]) == 0

    output = capsys.readouterr().out
    assert "super-secret" not in output
    assert "postgresql://migration_user:***@example.invalid/stats" in output
