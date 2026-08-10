"""Tests for the repeatable application-schema migration workflow."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from scripts import migrate
from scripts.validate_demo_db import validate_demo_database


def _sqlite_schema_snapshot(database_path: str | Path) -> tuple[bytes, tuple[tuple[str, str | None], ...]]:
    path = Path(database_path)
    with path.open("rb") as database_file:
        file_digest = hashlib.sha256(database_file.read()).digest()

    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        schema = tuple(
            connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' ORDER BY name"
            ).fetchall()
        )
    return file_digest, schema


def test_run_migrations_creates_current_schema_from_empty_database(tmp_path):
    database_path = tmp_path / "fresh.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.applied == (
        "001_create_users",
        "002_create_data_refresh_jobs",
        "003_durable_data_refresh_queue",
        "004_create_athlete_catalog",
        "005_create_event_catalog",
        "006_create_athlete_mappings",
        "007_create_athlete_mapping_contradictions",
        "008_create_event_mappings",
    )
    assert second.applied == ()
    assert sorted(inspect(engine).get_table_names()) == sorted(
        [
            "schema_migrations",
            "users",
            "data_refresh_jobs",
            "athlete_catalog",
            "athlete_catalog_freshness",
            "event_catalog",
            "event_catalog_refreshes",
            "provider_athlete_mappings",
            "athlete_mapping_decisions",
            "athlete_mapping_decision_candidates",
            "athlete_mapping_decision_contradictions",
            "athlete_mapping_rejections",
            "athlete_mapping_locks",
            "provider_event_mappings",
            "event_mapping_decision_contradictions",
            "event_mapping_decisions",
            "event_mapping_decision_candidates",
            "event_mapping_rejections",
            "event_mapping_locks",
        ]
    )
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
    assert {
        column["name"] for column in inspect(engine).get_columns("data_refresh_jobs")
    } == {
        "job_id",
        "operation",
        "status",
        "progress",
        "progress_note",
        "created_at",
        "started_at",
        "finished_at",
        "error_summary",
        "request_id",
        "lease_owner",
        "lease_expires_at",
        "heartbeat_at",
        "attempt_count",
    }

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version, name FROM schema_migrations ORDER BY version")
        ).all() == [
            (1, "001_create_users"),
            (2, "002_create_data_refresh_jobs"),
            (3, "003_durable_data_refresh_queue"),
            (4, "004_create_athlete_catalog"),
            (5, "005_create_event_catalog"),
            (6, "006_create_athlete_mappings"),
            (7, "007_create_athlete_mapping_contradictions"),
            (8, "008_create_event_mappings"),
        ]


def test_run_migrations_upgrades_existing_app_database(tmp_path):
    database_path = tmp_path / "existing.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")

    from app.models import Base

    Base.metadata.create_all(engine)

    result = run_migrations(engine)

    assert result.applied == (
        "001_create_users",
        "002_create_data_refresh_jobs",
        "003_durable_data_refresh_queue",
        "004_create_athlete_catalog",
        "005_create_event_catalog",
        "006_create_athlete_mappings",
        "007_create_athlete_mapping_contradictions",
        "008_create_event_mappings",
    )
    assert inspect(engine).has_table("users")
    assert inspect(engine).has_table("data_refresh_jobs")
    assert inspect(engine).has_table("athlete_catalog")
    assert inspect(engine).has_table("event_catalog")


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


def test_run_migrations_rejects_demo_database_without_mutating_it():
    database_path = Path("nba_play_types.db")
    before = _sqlite_schema_snapshot(database_path)
    with pytest.raises(ValueError, match="read-only demo database"):
        run_migrations(create_engine("sqlite:///nba_play_types.db"))
    assert _sqlite_schema_snapshot(database_path) == before


def test_app_factory_does_not_migrate_demo_database(monkeypatch):
    from app import create_app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    database_path = Path("nba_play_types.db")
    before = _sqlite_schema_snapshot(database_path)

    create_app({"TESTING": True, "SKIP_FIREBASE_INIT": True})

    assert _sqlite_schema_snapshot(database_path) == before


def test_app_factory_migrates_configured_application_database(tmp_path, monkeypatch):
    from app import create_app

    database_url = f"sqlite:///{tmp_path / 'application.sqlite3'}"
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")

    application = create_app(
        {
            "DATABASE_URL": database_url,
            "TESTING": True,
            "SKIP_FIREBASE_INIT": True,
        }
    )

    engine = create_engine(database_url)
    assert sorted(inspect(engine).get_table_names()) == sorted(
        [
            "schema_migrations",
            "users",
            "data_refresh_jobs",
            "athlete_catalog",
            "athlete_catalog_freshness",
            "event_catalog",
            "event_catalog_refreshes",
            "provider_athlete_mappings",
            "athlete_mapping_decisions",
            "athlete_mapping_decision_candidates",
            "athlete_mapping_decision_contradictions",
            "athlete_mapping_rejections",
            "athlete_mapping_locks",
            "provider_event_mappings",
            "event_mapping_decision_contradictions",
            "event_mapping_decisions",
            "event_mapping_decision_candidates",
            "event_mapping_rejections",
            "event_mapping_locks",
        ]
    )
    assert application.extensions["dependencies"].athlete_catalog_service is not None
    assert application.extensions["dependencies"].athlete_mapping_repository is not None
    assert "athlete_catalog" not in application.extensions["request_services"]


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


def test_contradiction_migration_upgrades_a_database_stopped_at_006(tmp_path):
    """An existing mapping database gains the table without losing decisions."""
    from app.migrations import MIGRATIONS
    from app.models.athlete_mapping import AthleteMappingDecision
    from sqlalchemy import insert as sql_insert, select as sql_select

    database_path = tmp_path / "at-006.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 6),
        )
        assert run_migrations(engine).current_version == 6
    with engine.begin() as connection:
        connection.execute(
            sql_insert(AthleteMappingDecision.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-15",
                decision_state="auto",
                created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            )
        )

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.applied == (
        "007_create_athlete_mapping_contradictions",
        "008_create_event_mappings",
    )
    assert second.applied == ()
    assert inspect(engine).has_table("athlete_mapping_decision_contradictions")
    with engine.connect() as connection:
        assert connection.execute(
            sql_select(AthleteMappingDecision.provider_athlete_id)
        ).scalars().all() == ["pp-15"]
