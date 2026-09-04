"""Tests for the repeatable application-schema migration workflow."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.migrations import MIGRATIONS, run_migrations
from app.models.projection_archive import (
    LatestPlayerProjection,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.projection_archive import ProjectionArchive
from app.services.dfs_snapshot_cache import serialize_provider_snapshot
from app.services.statistic_catalog import StatisticCatalog
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


def _create_projection_v40_fixture(engine) -> None:
    """Create the migration-40 projection cluster, including one row."""

    statements = (
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE projection_archive_scope_locks (provider VARCHAR(64) NOT NULL, season VARCHAR(7) NOT NULL, query_key VARCHAR(72) NOT NULL, PRIMARY KEY (provider, season, query_key))",
        "CREATE TABLE projection_provider_snapshots (snapshot_id VARCHAR(72) PRIMARY KEY, provider VARCHAR(64) NOT NULL, season VARCHAR(7) NOT NULL, query_key VARCHAR(72) NOT NULL, contract_version VARCHAR(32) NOT NULL, snapshot_status VARCHAR(16) NOT NULL, retrieved_at DATETIME NOT NULL, accepted_at DATETIME NOT NULL, checksum VARCHAR(64) NOT NULL UNIQUE, content_checksum VARCHAR(64) NOT NULL, evidence_document TEXT NOT NULL)",
        "CREATE TABLE projection_provider_polls (poll_id VARCHAR(72) PRIMARY KEY, provider VARCHAR(64) NOT NULL, season VARCHAR(7) NOT NULL, query_key VARCHAR(72) NOT NULL, started_at DATETIME, completed_at DATETIME NOT NULL, retrieved_at DATETIME NOT NULL, outcome VARCHAR(24) NOT NULL, snapshot_id VARCHAR(72) REFERENCES projection_provider_snapshots(snapshot_id) ON DELETE RESTRICT, generation_id VARCHAR(72) NOT NULL, observation_count INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE projection_materialization_generations (generation_id VARCHAR(72) PRIMARY KEY, provider VARCHAR(64) NOT NULL, season VARCHAR(7) NOT NULL, query_key VARCHAR(72) NOT NULL, snapshot_id VARCHAR(72) NOT NULL REFERENCES projection_provider_snapshots(snapshot_id) ON DELETE RESTRICT, source_poll_id VARCHAR(72) NOT NULL UNIQUE REFERENCES projection_provider_polls(poll_id) ON DELETE RESTRICT, created_at DATETIME NOT NULL, retrieved_at DATETIME NOT NULL, materialization_checksum VARCHAR(64) NOT NULL, outcome VARCHAR(32) NOT NULL, CONSTRAINT uq_projection_materialization_generation_identity UNIQUE (snapshot_id, materialization_checksum, retrieved_at))",
        "CREATE TABLE projection_observations (observation_id VARCHAR(72) PRIMARY KEY, snapshot_id VARCHAR(72) NOT NULL REFERENCES projection_provider_snapshots(snapshot_id) ON DELETE RESTRICT, generation_id VARCHAR(72) NOT NULL REFERENCES projection_materialization_generations(generation_id) ON DELETE RESTRICT, source_poll_id VARCHAR(72) NOT NULL REFERENCES projection_provider_polls(poll_id) ON DELETE RESTRICT, ordinal INTEGER NOT NULL, provider VARCHAR(64) NOT NULL, provider_market_id VARCHAR(255), market_reference VARCHAR(72) NOT NULL, canonical_game_id VARCHAR(32), canonical_player_id INTEGER, canonical_player_name VARCHAR(255), canonical_team_id INTEGER, canonical_statistic_id VARCHAR(128), market_category VARCHAR(32), market_status VARCHAR(32) NOT NULL, market_variant VARCHAR(32) NOT NULL, scoring_period VARCHAR(32) NOT NULL, targetable BOOLEAN NOT NULL DEFAULT 0, observed_at DATETIME NOT NULL, CONSTRAINT uq_projection_observations_generation_ordinal UNIQUE (generation_id, ordinal))",
        "CREATE TABLE latest_player_projections (provider VARCHAR(64) NOT NULL, season VARCHAR(7) NOT NULL, query_key VARCHAR(72) NOT NULL, canonical_game_id VARCHAR(32) NOT NULL, canonical_player_id INTEGER NOT NULL, market_reference VARCHAR(72) NOT NULL, observation_id VARCHAR(72) NOT NULL REFERENCES projection_observations(observation_id) ON DELETE RESTRICT, generation_id VARCHAR(72) NOT NULL REFERENCES projection_materialization_generations(generation_id) ON DELETE RESTRICT, canonical_team_id INTEGER NOT NULL, canonical_player_name VARCHAR(255) NOT NULL, canonical_statistic_id VARCHAR(128) NOT NULL, market_category VARCHAR(32) NOT NULL, observed_at DATETIME NOT NULL, PRIMARY KEY (provider, season, query_key, canonical_game_id, canonical_player_id, market_reference))",
    )
    observed_at = "2026-01-02 12:30:00"
    unchanged_at = "2026-01-02 12:31:00"
    evidence_document = serialize_provider_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(
                PlayerProjectionMarket(
                    provider="dabble",
                    market_id="market",
                    athlete=AthleteEvidence(
                        provider_id="athlete-v40",
                        canonical_id=7,
                        name="Player 7",
                        team=TeamEvidence(canonical_id=10),
                    ),
                    event=EventEvidence(
                        provider_id="event-v40",
                        canonical_id="game",
                    ),
                    team=TeamEvidence(canonical_id=10),
                    statistic=StatisticEvidence(
                        provider_id="stat-v40",
                        canonical_id="points",
                        label="Points",
                    ),
                    status=MarketStatus.AVAILABLE,
                    variant=MarketVariant.STANDARD,
                    scoring_period=ScoringPeriod.FULL_GAME,
                ),
            ),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                expected_total=1,
            ),
            retrieved_at=datetime.fromisoformat(observed_at).replace(
                tzinfo=timezone.utc
            ),
        ),
        NBAMarketQuery(season="2025-26"),
        allow_partial=True,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
        for migration in MIGRATIONS[:40]:
            connection.execute(
                text("INSERT INTO schema_migrations (version, name) VALUES (:version, :name)"),
                {"version": migration.version, "name": migration.name},
            )
        for identity, retrieved_at in (
            ("winner", observed_at),
            ("older", "2026-01-02 12:29:00"),
            ("same", observed_at),
        ):
            connection.execute(
                text(
                    "INSERT INTO projection_provider_snapshots VALUES "
                    "(:snapshot,'dabble','2025-26','query','1','complete',:at,:at,"
                    ":checksum,:content,:document)"
                ),
                {
                    "snapshot": f"{identity}_snapshot",
                    "at": retrieved_at,
                    "checksum": f"{identity}_checksum",
                    "content": f"{identity}_content",
                    "document": evidence_document if identity == "winner" else "{}",
                },
            )
        for poll_id, completed_at, retrieved_at, outcome, snapshot_id, generation_id in (
            (
                "winner_poll",
                observed_at,
                observed_at,
                "changed",
                "winner_snapshot",
                "winner_generation",
            ),
            (
                "older_poll",
                unchanged_at,
                "2026-01-02 12:29:00",
                "changed",
                "older_snapshot",
                "older_generation",
            ),
            (
                "same_poll",
                "2026-01-02 12:32:00",
                observed_at,
                "changed",
                "same_snapshot",
                "same_generation",
            ),
            (
                "unchanged_poll",
                "2026-01-02 12:33:00",
                unchanged_at,
                "unchanged",
                "winner_snapshot",
                "winner_generation",
            ),
            (
                "older_unchanged_poll",
                "2026-01-02 12:34:00",
                "2026-01-02 12:29:00",
                "unchanged",
                "winner_snapshot",
                "winner_generation",
            ),
            (
                "late_unchanged_poll",
                "2026-01-02 12:35:00",
                "2026-01-02 12:30:30",
                "unchanged",
                "winner_snapshot",
                "winner_generation",
            ),
        ):
            connection.execute(
                text(
                    "INSERT INTO projection_provider_polls VALUES "
                    "(:poll,'dabble','2025-26','query',NULL,:completed,:retrieved,"
                    ":outcome,:snapshot,:generation,1)"
                ),
                {
                    "poll": poll_id,
                    "completed": completed_at,
                    "retrieved": retrieved_at,
                    "outcome": outcome,
                    "snapshot": snapshot_id,
                    "generation": generation_id,
                },
            )
        for identity, retrieved_at, outcome in (
            ("winner", observed_at, "advanced"),
            ("older", "2026-01-02 12:29:00", "older_not_promoted"),
            ("same", observed_at, "same_time_not_promoted"),
        ):
            connection.execute(
                text(
                    "INSERT INTO projection_materialization_generations VALUES "
                    "(:generation,'dabble','2025-26','query',:snapshot,:poll,:created,"
                    ":retrieved,:materialized,:outcome)"
                ),
                {
                    "generation": f"{identity}_generation",
                    "snapshot": f"{identity}_snapshot",
                    "poll": f"{identity}_poll",
                    "created": observed_at,
                    "retrieved": retrieved_at,
                    "materialized": f"{identity}_materialized",
                    "outcome": outcome,
                },
            )
        connection.execute(text(
            "INSERT INTO projection_observations VALUES "
            "('observation','winner_snapshot','winner_generation','winner_poll',0,'dabble','market','reference','game',7,'Player 7',10,'points','PTS','available','standard','full_game',1,:at)"
        ), {"at": observed_at})
        connection.execute(text(
            "INSERT INTO latest_player_projections VALUES "
            "('dabble','2025-26','query','game',7,'reference','observation','winner_generation',10,'Player 7','points','PTS',:at)"
        ), {"at": observed_at})


def test_projection_transition_migration_upgrades_authentic_v40_sqlite(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-v40.sqlite3'}")
    _create_projection_v40_fixture(engine)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations._upgrade_correction_propagation",
            lambda _connection: None,
        )
        upgraded = run_migrations(engine)
        repeated = run_migrations(engine)

    assert upgraded.applied == (
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert upgraded.current_version == 48
    assert repeated.applied == ()
    inspector = inspect(engine)
    poll_columns = {
        column["name"]: column
        for column in inspector.get_columns("projection_provider_polls")
    }
    assert poll_columns["retrieved_at"]["nullable"] is True
    assert poll_columns["generation_id"]["nullable"] is True
    assert {"failure_reason", "promoted"} <= set(poll_columns)
    latest_columns = {
        column["name"]: column
        for column in inspector.get_columns("latest_player_projections")
    }
    assert latest_columns["confirmed_at"]["nullable"] is False
    observation_columns = {
        column["name"]
        for column in inspector.get_columns("projection_observations")
    }
    assert {
        "source_observation_id",
        "source_ordinal",
        "athlete_provider_id",
        "event_provider_id",
        "statistic_provider_id",
        "statistic_provider_label",
        "resolution_state",
        "unresolved_identities",
    } <= observation_columns
    generation_columns = {
        column["name"]
        for column in inspector.get_columns(
            "projection_materialization_generations"
        )
    }
    assert "is_replay" in generation_columns
    lock_columns = {
        column["name"]
        for column in inspector.get_columns("projection_archive_scope_locks")
    }
    assert {
        "active_generation_id",
        "mapping_replayed_at",
        "mapping_replayed_retrieved_at",
    } <= lock_columns
    observation_indexes = {
        index["name"]
        for index in inspector.get_indexes("projection_observations")
    }
    assert {
        "ix_projection_observations_provider_athlete",
        "ix_projection_observations_provider_event",
        "ix_projection_observations_provider_statistic_id",
        "ix_projection_observations_provider_statistic_label",
        "ix_projection_observations_snapshot_id",
    } <= observation_indexes
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name = "
                "'ix_projection_observations_provider_statistic_label_lower'"
            )
        ).scalar_one() == "ix_projection_observations_provider_statistic_label_lower"
    assert ("source_poll_id",) not in {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "projection_materialization_generations"
        )
    }
    with engine.connect() as connection:
        promoted = dict(connection.execute(text(
            "SELECT poll_id, promoted FROM projection_provider_polls ORDER BY poll_id"
        )).all())
        assert promoted == {
            "late_unchanged_poll": 0,
            "older_poll": 0,
            "older_unchanged_poll": 0,
            "same_poll": 0,
            "unchanged_poll": 1,
            "winner_poll": 1,
        }
        latest = connection.execute(text(
            "SELECT generation_id, confirmed_at, observed_at "
            "FROM latest_player_projections"
        )).one()
        assert latest[0] == "winner_generation"
        assert latest[1] == "2026-01-02 12:31:00"
        assert latest[2] == "2026-01-02 12:30:00"
        assert dict(connection.execute(text(
            "SELECT generation_id, outcome FROM projection_materialization_generations"
        )).all()) == {
            "winner_generation": "advanced",
            "older_generation": "older_not_promoted",
            "same_generation": "same_time_not_promoted",
        }
        assert connection.execute(text(
            "SELECT COUNT(*) FROM projection_materialization_generations "
            "WHERE is_replay = 1"
        )).scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        backfilled = connection.execute(text(
            "SELECT athlete_provider_id, event_provider_id, "
            "statistic_provider_id, statistic_provider_label, resolution_state "
            "FROM projection_observations WHERE observation_id = 'observation'"
        )).one()
        assert backfilled == (
            "athlete-v40",
            "event-v40",
            "stat-v40",
            "Points",
            "resolved",
        )
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projection_provider_polls "
            "(poll_id, provider, season, query_key, completed_at, retrieved_at, "
            "outcome, promoted, failure_reason, snapshot_id, generation_id, observation_count) "
            "VALUES ('failed','dabble','2025-26','query','2026-01-02 13:00:00',NULL,"
            "'failed',0,'access_denied',NULL,NULL,0)"
        ))


def test_v40_snapshot_replay_keeps_its_historical_poll_identity_after_upgrade(
    tmp_path,
):
    staging = create_engine(f"sqlite:///{tmp_path / 'projection-current.sqlite3'}")
    run_migrations(staging)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="prizepicks",
        market_id="legacy-market",
        athlete=AthleteEvidence(
            canonical_id=77,
            name="Legacy Player",
            team=TeamEvidence(canonical_id=10),
        ),
        event=EventEvidence(canonical_id="legacy-game"),
        team=TeamEvidence(canonical_id=10),
        statistic=evidence,
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=statistic,
            provider="prizepicks",
        ),
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    retrieved_at = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)
    snapshot = ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=1,
        ),
        retrieved_at=retrieved_at,
    )
    query = NBAMarketQuery(season="2025-26")
    first = ProjectionArchive(staging, catalog).ingest_snapshot(
        snapshot,
        query=query,
        accepted_at=retrieved_at,
    )

    upgraded_engine = create_engine(
        f"sqlite:///{tmp_path / 'projection-v40-replay.sqlite3'}"
    )
    _create_projection_v40_fixture(upgraded_engine)
    common_columns = {
        ProjectionProviderSnapshot: (
            "snapshot_id", "provider", "season", "query_key", "contract_version",
            "snapshot_status", "retrieved_at", "accepted_at", "checksum",
            "content_checksum", "evidence_document",
        ),
        ProviderPoll: (
            "poll_id", "provider", "season", "query_key", "started_at",
            "completed_at", "retrieved_at", "outcome", "snapshot_id",
            "generation_id", "observation_count",
        ),
        ProjectionMaterializationGeneration: (
            "generation_id", "provider", "season", "query_key", "snapshot_id",
            "source_poll_id", "created_at", "retrieved_at",
            "materialization_checksum", "outcome",
        ),
        ProjectionObservation: (
            "observation_id", "snapshot_id", "generation_id", "source_poll_id",
            "ordinal", "provider", "provider_market_id", "market_reference",
            "canonical_game_id", "canonical_player_id", "canonical_player_name",
            "canonical_team_id", "canonical_statistic_id", "market_category",
            "market_status", "market_variant", "scoring_period", "targetable",
            "observed_at",
        ),
        LatestPlayerProjection: (
            "provider", "season", "query_key", "canonical_game_id",
            "canonical_player_id", "market_reference", "observation_id",
            "generation_id", "canonical_team_id", "canonical_player_name",
            "canonical_statistic_id", "market_category", "observed_at",
        ),
    }
    historical_poll_id = "v40_historical_poll"
    with staging.connect() as source, upgraded_engine.begin() as target:
        for model, columns in common_columns.items():
            rows = source.execute(select(model.__table__)).mappings().all()
            for source_row in rows:
                row = {column: source_row[column] for column in columns}
                if model is ProviderPoll:
                    row["poll_id"] = historical_poll_id
                elif model in (
                    ProjectionMaterializationGeneration,
                    ProjectionObservation,
                ):
                    row["source_poll_id"] = historical_poll_id
                target.execute(
                    text(
                        f"INSERT INTO {model.__tablename__} "
                        f"({', '.join(columns)}) VALUES "
                        f"({', '.join(':' + column for column in columns)})"
                    ),
                    row,
                )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations._upgrade_correction_propagation",
            lambda _connection: None,
        )
        upgraded = run_migrations(upgraded_engine)
    with upgraded_engine.connect() as connection:
        before = tuple(
            connection.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (
                ProviderPoll,
                ProjectionProviderSnapshot,
                ProjectionMaterializationGeneration,
                ProjectionObservation,
                LatestPlayerProjection,
            )
        )
    replay = ProjectionArchive(upgraded_engine, catalog).ingest_snapshot(
        snapshot,
        query=query,
        accepted_at=retrieved_at + timedelta(minutes=10),
        poll_started_at=retrieved_at + timedelta(minutes=9),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations._upgrade_correction_propagation",
            lambda _connection: None,
        )
        repeated_migration = run_migrations(upgraded_engine)

    assert upgraded.applied == (
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert replay == first
    assert repeated_migration.applied == ()
    with upgraded_engine.connect() as connection:
        after = tuple(
            connection.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (
                ProviderPoll,
                ProjectionProviderSnapshot,
                ProjectionMaterializationGeneration,
                ProjectionObservation,
                LatestPlayerProjection,
            )
        )
        assert connection.execute(
            select(ProviderPoll.poll_id).where(
                ProviderPoll.provider == "prizepicks"
            )
        ).scalar_one() == historical_poll_id
    assert after == before


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
        "021_collector_surface_authorization",
        "022_publication_provenance_reconciliation",
        "023_collector_release_status",
        "024_canonical_game_ledger",
        "025_ledger_parity_artifacts",
        "026_repair_publication_provenance_foreign_keys",
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert second.applied == ()
    assert sorted(inspect(engine).get_table_names()) == sorted(
        [
            "schema_migrations",
            "users",
            "saved_filter_sets",
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
            "projection_archive_scope_locks",
            "projection_closing_sets",
            "projection_closing_memberships",
            "projection_provider_snapshots",
            "projection_provider_polls",
            "projection_observations",
            "projection_materialization_generations",
            "latest_player_projections",
            "projection_collection_leases",
            "projection_collection_provider_states",
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
            "publication_observations",
            "publication_pointers",
            "publication_player_game_logs",
            "publication_activations",
            "composition_jobs",
            "collector_token_replays",
            "collector_status_transitions",
            "collector_ingestion_leases",
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
            "canonical_game_ledger_games",
            "canonical_game_ledger_team_facts",
            "canonical_game_ledger_player_facts",
            "canonical_game_ledger_backfill",
            "canonical_game_ledger_publications",
            "canonical_game_ledger_parity_artifacts",
            "canonical_game_ledger_raw_rows",
            "canonical_game_ledger_observation_evidence",
        ]
    )
    generation_columns = {
        column["name"]
        for column in inspect(engine).get_columns(
            "projection_materialization_generations"
        )
    }
    assert {
        "retrieved_at",
        "materialization_checksum",
        "source_poll_id",
    } <= generation_columns
    poll_columns = {
        column["name"]
        for column in inspect(engine).get_columns("projection_provider_polls")
    }
    assert {"generation_id", "failure_reason"} <= poll_columns
    observation_columns = {
        column["name"]
        for column in inspect(engine).get_columns("projection_observations")
    }
    assert {"generation_id", "source_poll_id"} <= observation_columns
    latest_columns = {
        column["name"]
        for column in inspect(engine).get_columns("latest_player_projections")
    }
    assert "confirmed_at" in latest_columns


def test_publication_authority_migration_backfills_only_unambiguous_manifest(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-038.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 38),
        )
        assert run_migrations(engine).current_version == 38

    cutoff = datetime(2025, 11, 1, tzinfo=timezone.utc)
    ambiguous_cutoff = cutoff + timedelta(days=1)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO publication_streams (
                stream_key, provider, owner, required_observations,
                publication_strategy, supported_windows, schema_versions,
                completeness_rule, freshness_rule, enabled, created_at
            ) VALUES (
                'exact_shot_zones_opponent_season', 'nba', 'collector', '[]',
                'snapshot_replace', '[\"season\"]', '[1, 2]',
                'base_complete', 'cutoff_current', 0, :cutoff
            )
        """), {"cutoff": cutoff})
        for catalog_id, catalog_cutoff in (
            ("catalog-only", cutoff),
            ("catalog-a", ambiguous_cutoff),
        ):
            connection.execute(text("""
                INSERT INTO collection_catalog_publications (
                    publication_id, season, catalog_type, cutoff, version,
                    checksum, payload, complete, published_at
                ) VALUES (
                    :catalog_id, '2025-26', 'event', :cutoff, 'event-v1',
                    :checksum, '{}', 1, :published_at
                )
            """), {
                "catalog_id": catalog_id,
                "cutoff": catalog_cutoff,
                "checksum": catalog_id,
                "published_at": catalog_cutoff - timedelta(minutes=1),
            })
        for manifest_id, manifest_cutoff, catalog_id in (
            ("manifest-only", cutoff, "catalog-only"),
            ("manifest-a", ambiguous_cutoff, "catalog-a"),
            ("manifest-b-unbound", ambiguous_cutoff, None),
        ):
            connection.execute(text("""
                INSERT INTO collection_manifests (
                    manifest_id, season, cutoff, collect_before,
                    accepted_versions, scopes, checksum,
                    event_catalog_publication_id, event_catalog_checksum,
                    status, created_at
                ) VALUES (
                    :manifest_id, '2025-26', :cutoff, :collect_before,
                    '[1]', '[\"canonical_game_ledger\"]', :manifest_id,
                    :catalog_id, :catalog_checksum, 'superseded', :cutoff
                )
            """), {
                "manifest_id": manifest_id,
                "cutoff": manifest_cutoff,
                "collect_before": manifest_cutoff + timedelta(hours=1),
                "catalog_id": catalog_id,
                "catalog_checksum": catalog_id,
            })
        for publication_id, publication_cutoff, version in (
            ("version-only", cutoff, 1),
            ("version-ambiguous", ambiguous_cutoff, 2),
        ):
            connection.execute(text("""
                INSERT INTO publication_versions (
                    publication_id, stream_key, season, cutoff, version,
                    status, checksum, payload, created_at, fence
                ) VALUES (
                    :publication_id, 'exact_shot_zones_opponent_season',
                    '2025-26', :cutoff,
                    :version, 'candidate', :publication_id, '{}', :cutoff, 0
                )
            """), {
                "publication_id": publication_id,
                "cutoff": publication_cutoff,
                "version": version,
            })

    upgraded = run_migrations(engine)
    assert upgraded.applied == (
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    with engine.connect() as connection:
        rows = {
            row.publication_id: row
            for row in connection.execute(text("""
                SELECT publication_id, manifest_id,
                       event_catalog_publication_id, event_catalog_checksum
                FROM publication_versions
            """))
        }
    assert rows["version-only"].manifest_id == "manifest-only"
    assert rows["version-only"].event_catalog_publication_id == "catalog-only"
    assert rows["version-only"].event_catalog_checksum == "catalog-only"
    assert rows["version-ambiguous"].manifest_id is None
    collector_columns = {
        column["name"] for column in inspect(engine).get_columns("collector_identities")
    }
    assert {"release_version", "release_checksum"} <= collector_columns
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
            (21, "021_collector_surface_authorization"),
            (22, "022_publication_provenance_reconciliation"),
            (23, "023_collector_release_status"),
            (24, "024_canonical_game_ledger"),
            (25, "025_ledger_parity_artifacts"),
            (26, "026_repair_publication_provenance_foreign_keys"),
            (27, "027_bind_ledger_parity_to_publications"),
            (28, "028_collector_status_transitions"),
            (29, "029_publication_activations"),
            (30, "030_bind_publication_activation_candidates"),
            (31, "031_repair_canonical_game_ledger_tables"),
            (32, "032_ledger_raw_row_evidence"),
            (33, "033_ledger_observation_evidence"),
            (34, "034_team_matchup_ledger_lineage"),
            (35, "035_governed_catalog_freshness"),
            (36, "036_publication_player_game_log_projection"),
            (37, "037_team_matchup_publication_lineage"),
            (38, "038_bind_manifests_to_event_catalog_publications"),
            (39, "039_bind_publication_versions_to_manifest_authority"),
            (40, "040_projection_archive"),
            (41, "041_projection_archive_transitions"),
            (42, "042_team_matchup_provider_provenance"),
            (43, "043_projection_collection_control"),
            (44, "044_projection_closing_sets"),
            (45, "045_projection_mapping_replay"),
            (46, "046_projection_observation_prices"),
            (47, "047_create_saved_filter_sets"),
            (48, "048_drop_legacy_ranking_tables"),
        ]


def test_governed_catalog_freshness_migration_backfills_complete_publications(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-034.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 34),
        )
        assert run_migrations(engine).current_version == 34

    published_at = datetime(2026, 8, 14, 1, 21, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO event_catalog ("
                "nba_game_id, season, home_team_id, home_team_name, "
                "home_team_tricode, away_team_id, away_team_name, "
                "away_team_tricode, scheduled_at, status_text, classification, "
                "first_seen_at, last_seen_at"
                ") VALUES ("
                "'game-1', '2025-26', 1, 'Home', 'HOM', 2, 'Away', 'AWY', "
                ":published_at, 'Final', 'Regular Season', :published_at, :published_at"
                ")"
            ),
            {"published_at": published_at},
        )
        connection.execute(
            text(
                "INSERT INTO collection_catalog_publications ("
                "publication_id, season, catalog_type, cutoff, version, checksum, "
                "payload, complete, published_at"
                ") VALUES ("
                "'publication-1', '2025-26', 'event', :published_at, 'v1', "
                ":checksum, '{}', 1, :published_at"
                ")"
            ),
            {"published_at": published_at, "checksum": "a" * 64},
        )

    result = run_migrations(engine)

    assert result.applied == (
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    with engine.connect() as connection:
        freshness = connection.execute(
            text(
                "SELECT last_attempt_at, last_success_at, event_count, failure_summary "
                "FROM event_catalog_refreshes WHERE season = '2025-26'"
            )
        ).one()
    assert freshness.event_count == 1
    assert freshness.failure_summary is None
    assert freshness.last_attempt_at == freshness.last_success_at


def test_player_log_projection_migration_backfills_immutable_publications(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-035.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 35),
        )
        assert run_migrations(engine).current_version == 35

    row = {
        "season": "2025-26",
        "season_type": "Regular Season",
        "player_id": 2544,
        "game_id": "0022500001",
        "player_name": "LeBron James",
        "game_date": "2026-01-02",
        "team_id": 1,
        "team_tricode": "LAL",
        "opponent_team_id": 2,
        "opponent_team_tricode": "SAS",
        "is_home": True,
        "minutes": 35.0,
        "points": 25,
        "rebounds": 8,
        "assists": 7,
        "field_goals_made": 9,
        "field_goals_attempted": 18,
        "three_pointers_made": 3,
        "three_pointers_attempted": 7,
        "free_throws_made": 4,
        "free_throws_attempted": 5,
        "offensive_rebounds": 1,
        "defensive_rebounds": 7,
        "turnovers": 3,
        "steals": 1,
        "blocks": 1,
        "personal_fouls": 2,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO publication_versions "
                "(publication_id, stream_key, season, cutoff, version, status, "
                "checksum, payload, created_at, reason, fence) VALUES "
                "('legacy-player-logs', 'player_game_logs', '2025-26', "
                "'2026-01-02 00:00:00', 1, 'active', :checksum, :payload, "
                "'2026-01-02 00:00:00', 'legacy publication', 1)"
            ),
            {
                "checksum": "a" * 64,
                "payload": json.dumps({"rows": [row]}),
            },
        )

    result = run_migrations(engine)

    assert result.applied == (
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    with engine.connect() as connection:
        projected = connection.execute(
            text(
                "SELECT publication_id, player_id, game_id, row_payload "
                "FROM publication_player_game_logs"
            )
        ).one()
    assert projected.publication_id == "legacy-player-logs"
    assert projected.player_id == 2544
    assert projected.game_id == "0022500001"
    assert json.loads(projected.row_payload) == row


def test_old_036_correction_columns_backfill_legacy_lineage_before_coalescing(tmp_path):
    """A true pre-change 036 row gains empty lineage without fabricated evidence."""
    from app.migrations import MIGRATIONS
    from app.models.player_game_log import PublicationPlayerGameLog
    from app.services.ledger_materialization import LedgerCorrectionQueue
    from tests.services.test_ledger_derivations import _league_games

    engine = create_engine(f"sqlite:///{tmp_path / 'old-036.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 35),
        )
        assert run_migrations(engine).current_version == 35

    with engine.begin() as connection:
        # Migration 036 predates correction propagation entirely. Rebuild the
        # current model-created queue table to the exact deployed old shape,
        # and create only the old 036 projection before recording version 36.
        connection.execute(text("ALTER TABLE composition_jobs RENAME TO composition_jobs_pre_correction"))
        connection.execute(text(
            "CREATE TABLE composition_jobs ("
            "job_id VARCHAR(36) NOT NULL PRIMARY KEY, "
            "stream_key VARCHAR(96) NOT NULL, manifest_id VARCHAR(36), "
            "season VARCHAR(7) NOT NULL, cutoff DATETIME NOT NULL, "
            "status VARCHAR(16) NOT NULL, attempts INTEGER NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "last_error VARCHAR(64))"
        ))
        PublicationPlayerGameLog.__table__.create(connection, checkfirst=True)
        connection.execute(text(
            "INSERT INTO schema_migrations (version, name) "
            "VALUES (36, '036_publication_player_game_log_projection')"
        ))
        connection.execute(text(
            "INSERT INTO composition_jobs "
            "(job_id, stream_key, season, cutoff, status, attempts, created_at, updated_at) VALUES "
            "('legacy-job', 'player_game_logs', '2025-26', "
            ":cutoff, 'queued', 0, :cutoff, :cutoff)"
        ), {"cutoff": datetime(2025, 10, 15)})

    result = run_migrations(engine)
    assert result.applied == (
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT trigger_game_ids, ledger_evidence, game_set_checksum, generation "
            "FROM composition_jobs WHERE job_id = 'legacy-job'"
        )).one()
    assert json.loads(row.trigger_game_ids) == []
    assert json.loads(row.ledger_evidence) == {}
    assert row.game_set_checksum is None
    assert row.generation == 1

    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE composition_jobs SET status = 'running', "
            "claimed_generation = 1 WHERE job_id = 'legacy-job'"
        ))

    assert run_migrations(engine).applied == ()
    with engine.connect() as connection:
        running = connection.execute(text(
            "SELECT status, claimed_generation FROM composition_jobs "
            "WHERE job_id = 'legacy-job'"
        )).one()
    assert running == ("running", 1)

    # With no pre-change trigger/checksum columns, the first post-upgrade
    # acceptance creates only its own keyed evidence; no legacy lineage is
    # invented from unrelated defaults.
    game = _league_games()[0]
    queue = LedgerCorrectionQueue(clock=lambda: datetime(2025, 10, 15, tzinfo=timezone.utc))
    with engine.begin() as connection:
        queue(connection, game)
        row = connection.execute(text(
            "SELECT trigger_game_ids, ledger_evidence FROM composition_jobs "
            "WHERE job_id = 'legacy-job'"
        )).one()
    assert json.loads(row.trigger_game_ids) == [game.game_id]
    evidence = json.loads(row.ledger_evidence)
    assert evidence[game.game_id] == game.checksum


def test_repair_migration_recreates_ledger_tables_when_024_is_recorded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger-drift.sqlite3'}")
    ledger_tables = (
        "canonical_game_ledger_team_facts",
        "canonical_game_ledger_player_facts",
        "canonical_game_ledger_games",
        "canonical_game_ledger_backfill",
        "canonical_game_ledger_publications",
    )

    run_migrations(engine)
    with engine.begin() as connection:
        for table in ledger_tables:
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 31"))

    repaired = run_migrations(engine)

    assert repaired.applied == ("031_repair_canonical_game_ledger_tables",)
    assert repaired.current_version == 48
    assert all(inspect(engine).has_table(table) for table in ledger_tables)


def test_ledger_raw_row_evidence_migration_preserves_pre_032_games_as_unarchived(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-031.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 31),
        )
        assert run_migrations(engine).current_version == 31
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO canonical_game_ledger_games (
                game_id, season, season_type, game_date,
                home_team_id, home_team_tricode, away_team_id, away_team_tricode,
                status, source_observation_id, checksum, retrieved_at, updated_at
            ) VALUES (
                '0022400001', '2024-25', 'Regular Season', '2024-11-15',
                1610612747, 'LAL', 1610612759, 'SAS',
                'final', 'legacy:0022400001', 'legacy-checksum',
                '2024-11-16 00:00:00', '2024-11-16 00:00:00'
            )
        """))

    upgraded = run_migrations(engine)

    assert upgraded.applied == (
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert upgraded.current_version == 48
    assert inspect(engine).has_table("canonical_game_ledger_raw_rows")
    with engine.connect() as connection:
        raw_checksum = connection.execute(text(
            "SELECT raw_checksum FROM canonical_game_ledger_games "
            "WHERE game_id = '0022400001'"
        )).scalar_one()
        raw_count = connection.execute(text(
            "SELECT COUNT(*) FROM canonical_game_ledger_raw_rows WHERE game_id = '0022400001'"
        )).scalar_one()
    assert raw_checksum is None
    assert raw_count == 0


def test_ledger_observation_evidence_migration_backfills_existing_accepted_games(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-032.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 32),
        )
        run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO collection_observations (
                observation_id, client_observation_id, collector_id, manifest_id,
                environment, provider, observation_type, scope, season, cutoff,
                schema_version, checksum, payload, payload_bytes, retrieved_at, accepted_at
            ) VALUES (
                'obs-existing-1', 'client:obs-existing-1', 'railway-ledger', 'm',
                'server', 'pbp', 'canonical_game_ledger', '{"game_id":"0022400001"}',
                '2024-25', '2024-11-16 00:00:00', 1, 'c', '{}', 2,
                '2024-11-16 00:00:00', '2024-11-16 00:00:00'
            )
        """))
        connection.execute(text("""
            INSERT INTO canonical_game_ledger_games (
                game_id, season, season_type, game_date,
                home_team_id, home_team_tricode, away_team_id, away_team_tricode,
                status, source_observation_id, checksum, retrieved_at, updated_at
            ) VALUES (
                '0022400001', '2024-25', 'Regular Season', '2024-11-15',
                1610612747, 'LAL', 1610612759, 'SAS',
                'final', 'obs-existing-1', 'typed-checksum',
                '2024-11-16 00:00:00', '2024-11-16 00:00:00'
            )
        """))
        connection.execute(text("""
            INSERT INTO canonical_game_ledger_games (
                game_id, season, season_type, game_date,
                home_team_id, home_team_tricode, away_team_id, away_team_tricode,
                status, source_observation_id, checksum, retrieved_at, updated_at
            ) VALUES (
                '0022400002', '2024-25', 'Regular Season', '2024-11-14',
                1610612747, 'LAL', 1610612759, 'SAS',
                'final', 'legacy:0022400002', 'typed-checksum',
                '2024-11-15 00:00:00', '2024-11-15 00:00:00'
            )
        """))

    upgraded = run_migrations(engine)

    assert upgraded.applied == (
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert upgraded.current_version == 48
    with engine.connect() as connection:
        references = connection.execute(text(
            "SELECT observation_id, game_id FROM canonical_game_ledger_observation_evidence "
            "ORDER BY observation_id"
        )).all()
    assert references == [("obs-existing-1", "0022400001")]


def test_postgres_migrations_take_one_transaction_advisory_lock():
    from unittest.mock import Mock

    from app.migrations import _acquire_migration_lock

    connection = Mock()
    connection.dialect.name = "postgresql"

    _acquire_migration_lock(connection)

    statement = connection.execute.call_args.args[0]
    assert "pg_advisory_xact_lock" in str(statement)


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
        "021_collector_surface_authorization",
        "022_publication_provenance_reconciliation",
        "023_collector_release_status",
        "024_canonical_game_ledger",
        "025_ledger_parity_artifacts",
        "026_repair_publication_provenance_foreign_keys",
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
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
    assert inspect(engine).has_table("canonical_game_ledger_games")
    assert inspect(engine).has_table("canonical_game_ledger_team_facts")
    assert inspect(engine).has_table("canonical_game_ledger_player_facts")
    assert inspect(engine).has_table("canonical_game_ledger_backfill")
    assert inspect(engine).has_table("canonical_game_ledger_publications")


def test_collector_release_status_migration_upgrades_database_stopped_at_022(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-022.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 22),
        )
        assert run_migrations(engine).current_version == 22
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE collector_identities DROP COLUMN release_checksum"))

    upgraded = run_migrations(engine)

    assert upgraded.applied == (
        "023_collector_release_status",
        "024_canonical_game_ledger",
        "025_ledger_parity_artifacts",
        "026_repair_publication_provenance_foreign_keys",
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert upgraded.current_version == 48
    columns = {column["name"] for column in inspect(engine).get_columns("collector_identities")}
    assert {"release_version", "release_checksum"} <= columns


def test_publication_provenance_foreign_keys_have_no_version_self_reference(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'provenance-fks.sqlite3'}")
    run_migrations(engine)
    inspector = inspect(engine)

    assert {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            item["options"].get("ondelete"),
        )
        for item in inspector.get_foreign_keys("publication_versions")
    } == {
        (("manifest_id",), "collection_manifests", "RESTRICT"),
        (
            ("event_catalog_publication_id",),
            "collection_catalog_publications",
            "RESTRICT",
        ),
    }
    assert {
        (tuple(item["constrained_columns"]), item["referred_table"], item["options"].get("ondelete"))
        for item in inspector.get_foreign_keys("publication_observations")
    } == {
        (("publication_id",), "publication_versions", "CASCADE"),
        (("observation_id",), "collection_observations", "RESTRICT"),
    }
    assert {
        (tuple(item["constrained_columns"]), item["referred_table"], item["options"].get("ondelete"))
        for item in inspector.get_foreign_keys("canonical_game_ledger_observation_evidence")
    } == {
        (("observation_id",), "collection_observations", "RESTRICT"),
    }
    parity_columns = {
        column["name"]
        for column in inspector.get_columns("canonical_game_ledger_parity_artifacts")
    }
    assert {"publication_id", "payload_checksum"} <= parity_columns
    assert any(
        item["constrained_columns"] == ["publication_id"]
        and item["referred_table"] == "publication_versions"
        and item["options"].get("ondelete") == "CASCADE"
        for item in inspector.get_foreign_keys("canonical_game_ledger_parity_artifacts")
    )


def test_parity_binding_migration_retires_unbound_legacy_evidence(tmp_path):
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'parity-026.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 26),
        )
        run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE canonical_game_ledger_parity_artifacts"))
        connection.execute(text(
            "CREATE TABLE canonical_game_ledger_parity_artifacts ("
            "artifact_id VARCHAR(36) PRIMARY KEY, stream_key VARCHAR(96) NOT NULL, "
            "season VARCHAR(7) NOT NULL, cutoff TIMESTAMP NOT NULL, status VARCHAR(32) NOT NULL, "
            "report TEXT NOT NULL, created_at TIMESTAMP NOT NULL, decision VARCHAR(16), "
            "adjudicated_by VARCHAR(128), adjudicated_at TIMESTAMP, adjudication_reason VARCHAR(255))"
        ))
        connection.execute(text(
            "INSERT INTO canonical_game_ledger_parity_artifacts "
            "(artifact_id, stream_key, season, cutoff, status, report, created_at) VALUES "
            "('legacy', 'player_per36', '2025-26', '2026-08-12', 'exact', '{}', '2026-08-12')"
        ))

    result = run_migrations(engine)

    assert result.applied == (
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )


def test_publication_activation_030_rebuild_preserves_sqlite_fk_enforcement(tmp_path):
    """A real 029-era table upgrades without toggling PRAGMA in a transaction."""

    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'activation-029.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 29),
        )
        run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE publication_activations"))
        connection.execute(text(
            "INSERT INTO publication_versions "
            "(publication_id, stream_key, season, cutoff, version, status, checksum, payload, created_at, reason, fence) "
            "VALUES ('legacy-publication', 'player_game_logs', '2025-26', "
            "'2026-08-13 00:00:00', 1, 'active', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '{}', "
            "'2026-08-13 00:00:00', 'legacy', 1)"
        ))
        connection.execute(text(
            "CREATE TABLE publication_activations ("
            "activation_id VARCHAR(36) NOT NULL PRIMARY KEY, "
            "stream_key VARCHAR(96) NOT NULL, publication_id VARCHAR(36) NOT NULL, "
            "actor VARCHAR(128) NOT NULL, reason VARCHAR(255) NOT NULL, "
            "fence INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO publication_activations "
            "(activation_id, stream_key, publication_id, actor, reason, fence, created_at) "
            "VALUES ('legacy-activation', 'player_game_logs', 'legacy-publication', "
            "'operator', 'legacy evidence', 1, '2026-08-13 00:00:00')"
        ))
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1

    result = run_migrations(engine)

    assert result.current_version == 48
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        foreign_keys = inspect(engine).get_foreign_keys("publication_activations")
        assert any(
            item["referred_table"] == "publication_versions"
            and item["constrained_columns"] == ["publication_id"]
            for item in foreign_keys
        )
        assert connection.execute(text(
            "SELECT actor FROM publication_activations WHERE activation_id = 'legacy-activation'"
        )).scalar() == "operator"
    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM canonical_game_ledger_parity_artifacts"
        )) == 0


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
            "saved_filter_sets",
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
            "projection_archive_scope_locks",
            "projection_closing_sets",
            "projection_closing_memberships",
            "projection_provider_snapshots",
            "projection_provider_polls",
            "projection_observations",
            "projection_materialization_generations",
            "latest_player_projections",
            "projection_collection_leases",
            "projection_collection_provider_states",
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
            "publication_observations",
            "publication_pointers",
            "publication_player_game_logs",
            "publication_activations",
            "composition_jobs",
            "collector_token_replays",
            "collector_status_transitions",
            "collector_ingestion_leases",
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
            "canonical_game_ledger_games",
            "canonical_game_ledger_team_facts",
            "canonical_game_ledger_player_facts",
            "canonical_game_ledger_backfill",
            "canonical_game_ledger_publications",
            "canonical_game_ledger_parity_artifacts",
            "canonical_game_ledger_raw_rows",
            "canonical_game_ledger_observation_evidence",
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


def test_migration_cli_propagates_failures_as_a_nonzero_exit(monkeypatch):
    """A failing migration must not return a success status.

    ``scripts/migrate.py`` runs as the Railway pre-deploy command; the deploy is
    fail-closed only if a failed migration exits non-zero.  ``main`` returns 0
    solely on success, so an exception from ``run_migrations`` propagates out of
    ``main`` and, run as a script (``raise SystemExit(main())``), aborts the
    process with a non-zero status and a traceback, never a zero status.
    """

    def _boom(_url):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrate, "_run", _boom)

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate.main(["--database-url", "sqlite:////tmp/statsplus-fail.sqlite3"])


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
        "021_collector_surface_authorization",
        "022_publication_provenance_reconciliation",
        "023_collector_release_status",
        "024_canonical_game_ledger",
        "025_ledger_parity_artifacts",
        "026_repair_publication_provenance_foreign_keys",
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
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
        "021_collector_surface_authorization",
        "022_publication_provenance_reconciliation",
        "023_collector_release_status",
        "024_canonical_game_ledger",
        "025_ledger_parity_artifacts",
        "026_repair_publication_provenance_foreign_keys",
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert upgraded.current_version == 48
    assert repeated.applied == ()
    assert repeated.current_version == 48
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
        "021_collector_surface_authorization",
        "022_publication_provenance_reconciliation",
        "023_collector_release_status",
        "024_canonical_game_ledger",
        "025_ledger_parity_artifacts",
        "026_repair_publication_provenance_foreign_keys",
        "027_bind_ledger_parity_to_publications",
        "028_collector_status_transitions",
        "029_publication_activations",
        "030_bind_publication_activation_candidates",
        "031_repair_canonical_game_ledger_tables",
        "032_ledger_raw_row_evidence",
        "033_ledger_observation_evidence",
        "034_team_matchup_ledger_lineage",
        "035_governed_catalog_freshness",
        "036_publication_player_game_log_projection",
        "037_team_matchup_publication_lineage",
        "038_bind_manifests_to_event_catalog_publications",
        "039_bind_publication_versions_to_manifest_authority",
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert stored is not None
    assert stored.unresolved_team_entry_count == 0
    evidence = InjurySnapshotRepository(engine).get_evidence(
        InjurySnapshotScope("2025-26", "legacy")
    )
    assert evidence.raw_payload == [{"ID": "1"}]
    assert evidence.source_entries == ()


def test_provider_provenance_migration_adds_columns_without_backfilling_rows(tmp_path):
    """Migration 042 adds evidence columns but cannot invent old authority."""
    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'provider-provenance.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 39),
        )
        assert run_migrations(engine).current_version == 39

    provenance_columns = (
        "manifest_id",
        "event_catalog_publication_id",
        "event_catalog_checksum",
        "provider_window_identity",
    )
    with engine.begin() as connection:
        for table_name in (
            "team_matchup_facts",
            "team_matchup_surface_observations",
        ):
            for column_name in provenance_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"DROP COLUMN {column_name}"
                    )
                )
        connection.execute(text(
            "INSERT INTO team_matchup_facts ("
            "season, as_of_date, window_kind, window_games, team_id, base, "
            "slice_key, stat_key, raw_value, denominator_value, denominator_unit, "
            "provider, window_end_date, retrieved_at"
            ") VALUES ("
            "'2025-26', '2026-08-15', 'season', 0, 1, 'traditional', 'all', "
            "'points', 1.0, 1.0, 'minutes', 'nba', '2026-08-15', "
            "'2026-08-15 12:00:00'"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO team_matchup_surface_observations ("
            "season, as_of_date, window_kind, window_games, surface, status, "
            "retrieved_at"
            ") VALUES ("
            "'2025-26', '2026-08-15', 'season', 0, 'traditional', 'available', "
            "'2026-08-15 12:00:00'"
            ")"
        ))

    upgraded = run_migrations(engine)
    assert upgraded.applied == (
        "040_projection_archive",
        "041_projection_archive_transitions",
        "042_team_matchup_provider_provenance",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert upgraded.current_version == 48

    for table_name in (
        "team_matchup_facts",
        "team_matchup_surface_observations",
    ):
        columns = {
            column["name"] for column in inspect(engine).get_columns(table_name)
        }
        assert set(provenance_columns) <= columns

    with engine.connect() as connection:
        fact = connection.execute(text(
            "SELECT manifest_id, event_catalog_publication_id, "
            "event_catalog_checksum, provider_window_identity "
            "FROM team_matchup_facts"
        )).one()
        observation = connection.execute(text(
            "SELECT manifest_id, event_catalog_publication_id, "
            "event_catalog_checksum, provider_window_identity "
            "FROM team_matchup_surface_observations"
        )).one()

    assert tuple(fact) == (None, None, None, None)
    assert tuple(observation) == (None, None, None, None)


def test_migrations_repair_former_provider_version_040_history_idempotently(tmp_path):
    """Former branch history is relocated before current 040 is selected."""
    from app.migrations import Migration, _add_team_matchup_provider_provenance

    engine = create_engine(f"sqlite:///{tmp_path / 'former-provider-040.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            (
                *MIGRATIONS[:39],
                Migration(
                    40,
                    "040_team_matchup_provider_provenance",
                    _add_team_matchup_provider_provenance,
                ),
            ),
        )
        first = run_migrations(engine)
    assert first.current_version == 40

    upgraded = run_migrations(engine)
    repeated = run_migrations(engine)

    assert upgraded.applied == (
        "040_projection_archive",
        "041_projection_archive_transitions",
        "043_projection_collection_control",
        "044_projection_closing_sets",
        "045_projection_mapping_replay",
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert repeated.applied == ()
    with engine.connect() as connection:
        history = connection.execute(
            text("SELECT version, name FROM schema_migrations ORDER BY version")
        ).all()
    assert history[-9:] == [
        (40, "040_projection_archive"),
        (41, "041_projection_archive_transitions"),
        (42, "042_team_matchup_provider_provenance"),
        (43, "043_projection_collection_control"),
        (44, "044_projection_closing_sets"),
        (45, "045_projection_mapping_replay"),
        (46, "046_projection_observation_prices"),
        (47, "047_create_saved_filter_sets"),
        (48, "048_drop_legacy_ranking_tables"),
    ]
    assert inspect(engine).has_table("projection_provider_snapshots")


def test_projection_collection_migration_omits_derived_next_poll_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-collection-schema.sqlite3'}")

    result = run_migrations(engine)

    assert result.current_version == 48
    inspector = inspect(engine)
    columns = {
        column["name"]
        for column in inspector.get_columns("projection_collection_provider_states")
    }
    indexes = {
        index["name"]
        for index in inspector.get_indexes("projection_collection_provider_states")
    }
    assert "next_poll_at" not in columns
    assert "ix_projection_collection_provider_state_next_poll" not in indexes


def test_projection_closing_migration_adds_durable_event_start_fence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-closing-schema.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE projection_closing_memberships")
        connection.exec_driver_sql("DROP TABLE projection_closing_sets")
        connection.exec_driver_sql(
            "ALTER TABLE event_catalog DROP COLUMN first_observed_started_at"
        )
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = 44")
        )

    result = run_migrations(engine)

    assert result.applied == ("044_projection_closing_sets",)
    inspector = inspect(engine)
    assert "first_observed_started_at" in {
        column["name"] for column in inspector.get_columns("event_catalog")
    }
    assert inspector.has_table("projection_closing_sets")
    assert inspector.has_table("projection_closing_memberships")


def test_projection_price_migration_is_additive_and_idempotent(tmp_path):
    from app.migrations import MIGRATIONS, _add_projection_observation_prices

    engine = create_engine(f"sqlite:///{tmp_path / 'projection-prices.sqlite3'}")
    at = "2026-01-02 12:30:00"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.migrations.MIGRATIONS", MIGRATIONS[:45])
        run_migrations(engine)
        # A database upgraded before this change carries the version-045
        # observation table, which the current model no longer matches.
        with engine.begin() as connection:
            for column in ("price_kind", "price_value", "price_scope"):
                connection.exec_driver_sql(
                    f"ALTER TABLE projection_observations DROP COLUMN {column}"
                )
        with engine.connect() as connection:
            before = {
                column["name"]
                for column in inspect(connection).get_columns("projection_observations")
            }
        assert not before & {"price_kind", "price_value", "price_scope"}
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projection_provider_snapshots VALUES "
                    f"('snap','dabble','2025-26','query','1','complete','{at}',"
                    f"'{at}','sum','content','{{}}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO projection_provider_polls "
                    "(poll_id, provider, season, query_key, completed_at, outcome, "
                    "retrieved_at, snapshot_id, generation_id, observation_count) "
                    f"VALUES ('poll','dabble','2025-26','query','{at}','changed',"
                    f"'{at}','snap','gen',1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO projection_materialization_generations "
                    "(generation_id, provider, season, query_key, snapshot_id, "
                    "source_poll_id, created_at, retrieved_at, "
                    "materialization_checksum, outcome, is_replay) VALUES "
                    f"('gen','dabble','2025-26','query','snap','poll','{at}',"
                    f"'{at}','checksum','advanced',0)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO projection_observations "
                    "(observation_id, snapshot_id, generation_id, source_poll_id, "
                    "source_observation_id, source_ordinal, ordinal, provider, "
                    "market_reference, market_status, market_variant, scoring_period, "
                    "targetable, resolution_state, unresolved_identities, observed_at) "
                    "VALUES ('obs','snap','gen','poll','obs',0,0,'dabble','mkt',"
                    f"'available','standard','full_game',1,'resolved','','{at}')"
                )
            )

    applied = run_migrations(engine)
    repeated = run_migrations(engine)

    assert applied.applied == (
        "046_projection_observation_prices",
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert applied.current_version == 48
    assert repeated.applied == ()
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT price_kind, price_value, price_scope, targetable "
                "FROM projection_observations"
            )
        ).mappings().one()
        # Running the upgrade a second time by hand changes nothing either.
        _add_projection_observation_prices(connection)

    # A row archived before the price triple existed says no price was
    # recorded, and the targetable decision it was written with is untouched.
    assert row["price_kind"] == "unpriced"
    assert row["price_value"] is None
    assert row["price_scope"] == "selection"
    assert row["targetable"] == 1


def test_saved_filter_set_migration_upgrades_a_database_stopped_at_046(tmp_path):
    """An existing application database gains the table and its indexes."""

    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-046.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 46),
        )
        assert run_migrations(engine).current_version == 46
    assert not inspect(engine).has_table("saved_filter_sets")

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, "
                "is_active) VALUES ('kept-uid', 'kept@example.com', "
                "'2026-08-01 00:00:00', '2026-08-01 00:00:00', 1)"
            )
        )

    upgraded = run_migrations(engine)
    repeated = run_migrations(engine)

    assert upgraded.applied == (
        "047_create_saved_filter_sets",
        "048_drop_legacy_ranking_tables",
    )
    assert repeated.applied == ()
    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns("saved_filter_sets")} == {
        "id",
        "firebase_uid",
        "name",
        "query_string",
        "created_at",
        "updated_at",
    }
    # The case-insensitive uniqueness index is expression-based, which
    # SQLAlchemy reflection skips, so read the recorded DDL instead.
    with engine.connect() as connection:
        indexes = dict(
            connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'saved_filter_sets'"
                )
            ).all()
        )
    assert set(indexes) == {
        "idx_saved_filter_sets_owner_created",
        "uq_saved_filter_sets_owner_name",
    }
    assert "UNIQUE INDEX uq_saved_filter_sets_owner_name" in (
        indexes["uq_saved_filter_sets_owner_name"]
    )
    assert "lower(name)" in indexes["uq_saved_filter_sets_owner_name"]
    # The account that predates the migration keeps its row and can own one.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO saved_filter_sets (firebase_uid, name, query_string, "
                "created_at, updated_at) VALUES ('kept-uid', 'Kept', 'player=X', "
                "'2026-08-02 00:00:00', '2026-08-02 00:00:00')"
            )
        )
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT name FROM saved_filter_sets")
        ).scalars().all() == ["Kept"]


#: The six nightly ranking tables #199 retires.  ``opp_shooting_zone`` is
#: deliberately absent: its stream is activated but the table is not retired.
_DROPPED_LEGACY_RANKING_TABLES = (
    "general_opponent_stats",
    "catch_and_shoot",
    "pullups",
    "less_than_10_ft",
    "team_play_types",
    "processed_team_assists",
)


def test_legacy_ranking_drop_migration_removes_the_six_tables(tmp_path):
    """A database carrying the pre-cutover tables loses exactly those six."""

    from app.migrations import MIGRATIONS

    engine = create_engine(f"sqlite:///{tmp_path / 'at-047.sqlite3'}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.migrations.MIGRATIONS",
            tuple(migration for migration in MIGRATIONS if migration.version <= 47),
        )
        assert run_migrations(engine).current_version == 47

    survivors = ("opp_shooting_zone", "player_per36_stats")
    with engine.begin() as connection:
        for table in _DROPPED_LEGACY_RANKING_TABLES + survivors:
            connection.execute(text(f"CREATE TABLE {table} (value TEXT)"))
            connection.execute(text(f"INSERT INTO {table} VALUES ('old')"))

    upgraded = run_migrations(engine)

    assert upgraded.applied == ("048_drop_legacy_ranking_tables",)
    inspector = inspect(engine)
    for table in _DROPPED_LEGACY_RANKING_TABLES:
        assert not inspector.has_table(table)
    # opp_shooting_zone is not on #199's list and must survive the drop.
    for table in survivors:
        assert inspector.has_table(table)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT value FROM opp_shooting_zone")
        ).scalar_one() == "old"


def test_legacy_ranking_drop_migration_is_idempotent_on_a_fresh_database(tmp_path):
    """The gate applies migrations twice, and no database ever held them."""

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite3'}")

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert "048_drop_legacy_ranking_tables" in first.applied
    assert second.applied == ()
    inspector = inspect(engine)
    for table in _DROPPED_LEGACY_RANKING_TABLES:
        assert not inspector.has_table(table)
