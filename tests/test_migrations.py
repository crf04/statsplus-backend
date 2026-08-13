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
        "009_create_stats_freshness",
        "010_create_player_pool_snapshots",
        "011_create_player_game_logs",
        "012_create_team_matchup_facts",
        "013_create_player_diet_facts",
        "014_create_injury_snapshots",
        "015_share_injury_source_snapshots",
        "016_pbp_game_log_primitives",
        "017_collection_control_plane",
        "018_collection_operations",
        "019_surface_registry_metadata",
        "020_operator_control",
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
            "stats_refreshes",
            "player_pool_snapshots",
            "player_game_logs",
            "player_game_log_refreshes",
            "player_game_log_sync",
            "active_seasons",
            "collection_bootstrap_requests",
            "collection_catalog_publications",
            "collection_manifests",
            "collector_identities",
            "collection_observations",
            "publication_streams",
            "publication_versions",
            "publication_pointers",
            "composition_jobs",
            "collector_token_replays",
            "collection_cycles",
            "collection_audit_events",
            "collection_reconciliation_items",
            "collection_alerts",
            "collector_usage",
            "collection_validation_summaries",
            "governed_not_applicable",
            "collection_operator_jobs",
            "collector_credential_deliveries",
            "team_matchup_facts",
            "team_matchup_surface_observations",
            "player_diet_facts",
            "player_diet_surface_observations",
            "injury_snapshots",
            "injury_source_snapshots",
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
    assert {
        column["name"]
        for column in inspect(engine).get_columns("player_pool_snapshots")
    } == {
        "season",
        "game_ids",
        "payload",
        "retrieved_at",
        "updated_at",
        "lease_owner",
        "lease_expires_at",
        "refresh_version",
        "refresh_outcome",
    }
    assert {
        column["name"] for column in inspect(engine).get_columns("player_game_logs")
    } == {
        "season",
        "season_type",
        "player_id",
        "game_id",
        "player_name",
        "game_date",
        "team_id",
        "team_tricode",
        "opponent_team_id",
        "opponent_team_tricode",
        "is_home",
        "minutes",
        "points",
        "rebounds",
        "assists",
        "field_goals_made",
        "field_goals_attempted",
        "three_pointers_made",
        "three_pointers_attempted",
        "free_throws_made",
        "free_throws_attempted",
        "offensive_rebounds",
        "defensive_rebounds",
        "turnovers",
        "steals",
        "blocks",
        "personal_fouls",
    }
    assert inspect(engine).get_check_constraints("player_game_logs") == [
        {
            "name": "ck_player_game_logs_season_type",
            "sqltext": "season_type IN ('Regular Season', 'Playoffs')",
        }
    ]
    assert {
        column["name"]
        for column in inspect(engine).get_columns("player_game_log_refreshes")
    } == {
        "season",
        "source_provider",
        "retrieved_at",
        "row_count",
        "source_row_count",
        "identity_source_row_count",
        "publication_status",
    }
    assert {
        column["name"]
        for column in inspect(engine).get_columns("player_game_log_sync")
    } == {
        "season",
        "game_id",
        "season_type",
        "status",
        "checksum",
        "row_count",
        "source_provider",
        "retrieved_at",
    }
    assert inspect(engine).get_check_constraints("player_game_log_refreshes") == [
        {
            "name": "ck_player_game_log_refresh_counts",
            "sqltext": (
                "source_row_count >= identity_source_row_count "
                "AND identity_source_row_count >= row_count AND row_count >= 0"
            ),
        }
    ]
    assert {
        column["name"] for column in inspect(engine).get_columns("player_diet_facts")
    } == {
        "id",
        "season",
        "player_id",
        "base",
        "slice_key",
        "share",
        "volume",
        "games_played",
        "volume_unit",
        "provider",
        "retrieved_at",
    }
    assert {
        column["name"]
        for column in inspect(engine).get_columns(
            "player_diet_surface_observations"
        )
    } == {
        "season",
        "base",
        "status",
        "unavailable_reason",
        "retrieved_at",
    }
    assert {
        constraint["name"]
        for constraint in inspect(engine).get_check_constraints(
            "player_diet_facts"
        )
    } == {
        "ck_player_diet_base",
        "ck_player_diet_games_played",
        "ck_player_diet_player_id",
        "ck_player_diet_share",
        "ck_player_diet_volume",
        "ck_player_diet_volume_unit",
    }
    assert {
        column["name"] for column in inspect(engine).get_columns("injury_snapshots")
    } == {
        "season",
        "game_id",
        "source_snapshot_id",
        "raw_payload",
        "normalized_entries",
        "unresolved_team_entry_count",
        "retrieved_at",
        "updated_at",
    }
    assert {
        column["name"]
        for column in inspect(engine).get_columns("injury_source_snapshots")
    } == {"id", "source", "raw_payload", "normalized_entries", "retrieved_at"}

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
            (9, "009_create_stats_freshness"),
            (10, "010_create_player_pool_snapshots"),
            (11, "011_create_player_game_logs"),
            (12, "012_create_team_matchup_facts"),
            (13, "013_create_player_diet_facts"),
            (14, "014_create_injury_snapshots"),
            (15, "015_share_injury_source_snapshots"),
            (16, "016_pbp_game_log_primitives"),
            (17, "017_collection_control_plane"),
            (18, "018_collection_operations"),
            (19, "019_surface_registry_metadata"),
            (20, "020_operator_control"),
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
        "009_create_stats_freshness",
        "010_create_player_pool_snapshots",
        "011_create_player_game_logs",
        "012_create_team_matchup_facts",
        "013_create_player_diet_facts",
        "014_create_injury_snapshots",
        "015_share_injury_source_snapshots",
        "016_pbp_game_log_primitives",
        "017_collection_control_plane",
        "018_collection_operations",
        "019_surface_registry_metadata",
        "020_operator_control",
    )
    assert inspect(engine).has_table("users")
    assert inspect(engine).has_table("data_refresh_jobs")
    assert inspect(engine).has_table("athlete_catalog")
    assert inspect(engine).has_table("event_catalog")
    assert inspect(engine).has_table("player_pool_snapshots")
    assert inspect(engine).has_table("player_game_logs")
    assert inspect(engine).has_table("player_game_log_refreshes")
    assert inspect(engine).has_table("player_game_log_sync")
    assert inspect(engine).has_table("team_matchup_facts")
    assert inspect(engine).has_table("player_diet_facts")
    assert inspect(engine).has_table("player_diet_surface_observations")
    assert inspect(engine).has_table("injury_snapshots")
    assert inspect(engine).has_table("injury_source_snapshots")


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
            "stats_refreshes",
            "player_pool_snapshots",
            "player_game_logs",
            "player_game_log_refreshes",
            "player_game_log_sync",
            "active_seasons",
            "collection_bootstrap_requests",
            "collection_catalog_publications",
            "collection_manifests",
            "collector_identities",
            "collection_observations",
            "publication_streams",
            "publication_versions",
            "publication_pointers",
            "composition_jobs",
            "collector_token_replays",
            "collection_cycles",
            "collection_audit_events",
            "collection_reconciliation_items",
            "collection_alerts",
            "collector_usage",
            "collection_validation_summaries",
            "governed_not_applicable",
            "collection_operator_jobs",
            "collector_credential_deliveries",
            "team_matchup_facts",
            "team_matchup_surface_observations",
            "player_diet_facts",
            "player_diet_surface_observations",
            "injury_snapshots",
            "injury_source_snapshots",
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
        "009_create_stats_freshness",
        "010_create_player_pool_snapshots",
        "011_create_player_game_logs",
        "012_create_team_matchup_facts",
        "013_create_player_diet_facts",
        "014_create_injury_snapshots",
        "015_share_injury_source_snapshots",
        "016_pbp_game_log_primitives",
        "017_collection_control_plane",
        "018_collection_operations",
        "019_surface_registry_metadata",
        "020_operator_control",
    )
    assert second.applied == ()
    assert inspect(engine).has_table("athlete_mapping_decision_contradictions")
    with engine.connect() as connection:
        assert connection.execute(
            sql_select(AthleteMappingDecision.provider_athlete_id)
        ).scalars().all() == ["pp-15"]


def test_player_pool_snapshot_migration_upgrades_database_stopped_at_009(tmp_path):
    """The merged stats-freshness schema advances independently to pool snapshots."""
    from app.migrations import MIGRATIONS

    database_path = tmp_path / "at-009.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 9),
        )
        first = run_migrations(engine)

    assert first.current_version == 9
    assert inspect(engine).has_table("stats_refreshes")
    assert not inspect(engine).has_table("player_pool_snapshots")

    upgraded = run_migrations(engine)
    repeated = run_migrations(engine)

    assert upgraded.applied == (
        "010_create_player_pool_snapshots",
        "011_create_player_game_logs",
        "012_create_team_matchup_facts",
        "013_create_player_diet_facts",
        "014_create_injury_snapshots",
        "015_share_injury_source_snapshots",
        "016_pbp_game_log_primitives",
        "017_collection_control_plane",
        "018_collection_operations",
        "019_surface_registry_metadata",
        "020_operator_control",
    )
    assert upgraded.current_version == 20
    assert repeated.applied == ()
    assert repeated.current_version == 20
    assert inspect(engine).has_table("stats_refreshes")
    assert inspect(engine).has_table("player_pool_snapshots")
    assert inspect(engine).has_table("player_game_logs")
    assert inspect(engine).has_table("player_game_log_sync")
    assert inspect(engine).has_table("team_matchup_facts")
    assert inspect(engine).has_table("player_diet_facts")
    assert inspect(engine).has_table("injury_snapshots")
    assert inspect(engine).has_table("injury_source_snapshots")


def test_shared_injury_source_migration_preserves_legacy_014_rows(tmp_path):
    from app.migrations import MIGRATIONS
    from app.services.injury_snapshot_repository import (
        InjurySnapshotRepository,
        InjurySnapshotScope,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'at-014.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 14),
        )
        run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE injury_snapshots DROP COLUMN source_snapshot_id"))
        connection.execute(
            text("ALTER TABLE injury_snapshots DROP COLUMN unresolved_team_entry_count")
        )
        connection.execute(
            text(
                "INSERT INTO injury_snapshots "
                "(season, game_id, raw_payload, normalized_entries, retrieved_at, updated_at) "
                "VALUES ('2025-26', 'legacy', '[{\"ID\":\"1\"}]', '[]', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )

    upgraded = run_migrations(engine)
    stored = InjurySnapshotRepository(engine).get(
        InjurySnapshotScope("2025-26", "legacy")
    )

    assert upgraded.applied == (
        "015_share_injury_source_snapshots",
        "016_pbp_game_log_primitives",
        "017_collection_control_plane",
        "018_collection_operations",
        "019_surface_registry_metadata",
        "020_operator_control",
    )
    assert stored is not None
    assert stored.unresolved_team_entry_count == 0
    evidence = InjurySnapshotRepository(engine).get_evidence(
        InjurySnapshotScope("2025-26", "legacy")
    )
    assert evidence.raw_payload == [{"ID": "1"}]
    assert evidence.source_entries == ()
