"""Activation/read-side contracts for backend issue #87."""

from datetime import date, datetime, timedelta, timezone
import json
import hashlib

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion
from app.services.collection_control import PublicationService
from app.services.database_first_activation import (
    DatabaseFirstActivationService,
    DatabaseFirstPublicationReader,
    DatabaseOnlyProviderGuard,
    LegacyWriteFence,
    PublicationPayloadError,
    decode_player_game_logs,
)
from app.services.database_first_benchmark import benchmark_matchup_reads
from app.services.database_first_drills import DrillResult, FailureDrillReport, FailureDrillRunner
from app.services.database_first_drills import (
    connected_database_identity,
    same_database_identity,
)
from app.services.database_first_rehearsal import HistoricalRehearsalRunner


UTC = timezone.utc
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'activation.sqlite3'}")
    run_migrations(engine)
    return engine


def test_activation_facade_uses_default_clock_when_none_is_supplied(tmp_path):
    engine = _db(tmp_path)
    facade = DatabaseFirstActivationService(engine)

    stream = facade.publications.register_stream(
        "default_clock_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
    )

    assert stream.created_at is not None


def test_reader_serves_active_last_good_and_marks_it_stale(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "database_first_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    publication = service.compose(
        "database_first_test",
        season="2025-26",
        cutoff=NOW,
        payload={"value": 1},
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW + timedelta(hours=2))
    result = reader.read("database_first_test", season="2025-26")
    assert result.payload == {"value": 1}
    assert result.publication_id == publication.publication_id
    assert result.freshness == "stale"


def test_reader_keeps_rollback_publication_available(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "rollback_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
    )
    first = service.compose(
        "rollback_test", season="2025-26", cutoff=NOW, payload={"value": 1}
    )
    service.compose(
        "rollback_test",
        season="2025-26",
        cutoff=NOW,
        payload={"value": 2},
        expected_fence=1,
    )
    service.rollback("rollback_test", reason="restore last good")
    result = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).read(
        "rollback_test", season="2025-26"
    )
    assert result.status == "rollback"
    assert result.available
    assert result.publication_id != first.publication_id
    assert result.payload == {"value": 1}


def test_reader_reports_independent_missing_and_mixed_streams(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    for key in ("one", "two"):
        service.register_stream(
            key,
            provider="ledger",
            owner="railway",
            required_observations=(),
            publication_strategy="replace",
            enabled=True,
        )
    service.compose("one", season="2025-26", cutoff=NOW, payload={"v": 1})
    metadata = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).metadata(
        ("one", "two"), season="2025-26"
    )
    assert metadata["streams"]["one"]["status"] == "active"
    assert metadata["streams"]["two"]["status"] == "missing"
    assert metadata["mixed_cutoff"] is True
    assert metadata["mixed_freshness"] is True


def test_reader_metadata_marks_fresh_plus_invalid_publication_as_mixed(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "fresh", provider="ledger", owner="railway", required_observations=(),
        publication_strategy="replace", enabled=True,
    )
    service.register_stream(
        "player_per36", provider="ledger", owner="railway",
        required_observations=(), publication_strategy="replace", enabled=True,
    )
    service.compose("fresh", season="2025-26", cutoff=NOW, payload={"value": 1})
    service.compose(
        "player_per36", season="2025-26", cutoff=NOW, payload={"rows": []}
    )

    metadata = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).metadata(
        ("fresh", "player_per36"), season="2025-26"
    )
    invalid_read = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).read(
        "player_per36", season="2025-26"
    )

    assert metadata["streams"]["player_per36"]["status"] == "unavailable"
    assert metadata["streams"]["player_per36"]["unavailable_reason"] == (
        "publication_payload_invalid"
    )
    assert invalid_read.retrieved_at is None
    assert metadata["mixed_cutoff"] is True
    assert metadata["mixed_freshness"] is True


def test_reader_marks_disabled_stream_as_the_only_legacy_fallback(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "legacy_fallback_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=False,
    )
    result = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).read(
        "legacy_fallback_test", season="2025-26"
    )
    assert result.legacy_fallback_allowed
    assert result.source == "legacy_database"
    assert result.status == "inactive"
    assert result.retrieved_at is None


def test_player_log_publication_decoder_is_strict():
    row = {
        "season": "2025-26",
        "season_type": "Regular Season",
        "player_id": 1,
        "game_id": "game-1",
        "player_name": "Player One",
        "game_date": "2026-01-01",
        "team_id": 1,
        "team_tricode": "AAA",
        "opponent_team_id": 2,
        "opponent_team_tricode": "BBB",
        "is_home": True,
        "minutes": 30.0,
        "points": 10,
        "rebounds": 5,
        "assists": 2,
        "field_goals_made": 4,
        "field_goals_attempted": 8,
        "three_pointers_made": 1,
        "three_pointers_attempted": 3,
        "free_throws_made": 1,
        "free_throws_attempted": 1,
        "offensive_rebounds": 1,
        "defensive_rebounds": 4,
        "turnovers": 1,
        "steals": 1,
        "blocks": 0,
        "personal_fouls": 1,
    }
    decoded = decode_player_game_logs([row], season="2025-26")
    assert decoded[0].game_id == "game-1"
    with pytest.raises(PublicationPayloadError):
        decode_player_game_logs([{**row, "is_home": "true"}], season="2025-26")


def test_legacy_write_fence_fails_only_after_stream_activation(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "synergy_play_types",
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="replace",
        enabled=False,
    )
    fence = LegacyWriteFence(engine)
    fence.assert_writable("synergy_play_types")
    service.activate_stream("synergy_play_types", reason="approved activation")
    try:
        fence.assert_writable("synergy_play_types")
    except Exception as error:
        assert str(error) == "legacy_write_fenced"
    else:  # pragma: no cover - assertion makes the contract explicit
        raise AssertionError("activated stream accepted a legacy write")


def test_split_season_stream_fences_both_legacy_opponent_writers(tmp_path, monkeypatch):
    import pandas as pd

    from app.config.settings import load_settings
    from app.services.data_service import DataService

    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "traditional_opponent_season",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
    )
    service.register_stream(
        "traditional_opponent",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
    )
    data_service = DataService(
        engine,
        settings=load_settings(),
    )
    frame = pd.DataFrame([{"TEAM_NAME": "LAL", "OPP_PTS": 1}])
    monkeypatch.setattr(data_service, "_fetch_opponent_data", lambda *args, **kwargs: frame)
    monkeypatch.setattr(data_service, "_collect_all_frames", lambda: {
        "general_opponent_stats": frame,
    })

    assert data_service.update_all_data() is False
    with pytest.raises(Exception, match="legacy_write_fenced"):
        data_service.process_opponent_scoring()

    from app.services.team_matchup_repository import (
        TeamMatchupFact, TeamMatchupObservation, TeamMatchupRepository,
        TeamMatchupSnapshotScope,
    )
    repository = TeamMatchupRepository(engine)
    with pytest.raises(Exception, match="legacy_write_fenced"):
        repository.replace_snapshots((
            (
                TeamMatchupSnapshotScope("2025-26", NOW.date()),
                (TeamMatchupFact(
                    team_id=1610612747, base="traditional", slice_key="OPP_REB",
                    stat_key="OPP_REB", raw_value=1, denominator_value=48,
                    denominator_unit="minutes", provider="nba_stats",
                ),),
                (TeamMatchupObservation("traditional", "available"),),
            ),
        ), retrieved_at=NOW)


def test_provider_guard_is_fail_closed():
    guard = DatabaseOnlyProviderGuard("nba")
    try:
        guard.fetch_game_logs()
    except AssertionError as error:
        assert "database-only" in str(error)
    else:  # pragma: no cover
        raise AssertionError("provider call guard did not fail closed")


def test_historical_rehearsal_runs_seven_dates_without_pointer_mutation(tmp_path):
    engine = _db(tmp_path)
    dates = tuple(date(2026, 4, day) for day in range(6, 13))
    player_log_payload = json.dumps({
        "rows": [{
            "season": "2025-26", "season_type": "Regular Season",
            "player_id": 1, "game_id": "game-1", "player_name": "Player One",
            "game_date": "2026-04-06", "team_id": 1, "team_tricode": "AAA",
            "opponent_team_id": 2, "opponent_team_tricode": "BBB",
            "is_home": True, "minutes": 30.0, "points": 10, "rebounds": 5,
            "assists": 2, "field_goals_made": 4, "field_goals_attempted": 8,
            "three_pointers_made": 1, "three_pointers_attempted": 3,
            "free_throws_made": 1, "free_throws_attempted": 1,
            "offensive_rebounds": 1, "defensive_rebounds": 4,
            "turnovers": 1, "steals": 1, "blocks": 0, "personal_fouls": 1,
        }]
    })
    synergy_payload = json.dumps({
        "base": "play_types",
        "rows": [{
            "player_id": 1, "slice_key": "Transition", "share": 0.1,
            "volume": 1.0, "games_played": 1, "volume_unit": "possessions",
            "provider": "nba_synergy",
        }],
    })
    with engine.begin() as connection:
        for cutoff in dates:
            connection.execute(PublicationVersion.__table__.insert().values(
                publication_id=f"rehearsal-{cutoff.isoformat()}",
                stream_key="player_game_logs",
                season="2025-26",
                cutoff=datetime.combine(
                    cutoff, datetime.min.time(), tzinfo=UTC
                ) + timedelta(hours=12),
                version=1,
                status="candidate",
                checksum=hashlib.sha256(player_log_payload.encode()).hexdigest(),
                payload=player_log_payload,
                created_at=NOW,
                fence=0,
            ))
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id="rehearsal-synergy",
            stream_key="synergy_play_types",
            season="2025-26",
            cutoff=datetime.combine(
                dates[-1], datetime.min.time(), tzinfo=UTC
            ) + timedelta(hours=12),
            version=1,
            status="candidate",
            checksum=hashlib.sha256(synergy_payload.encode()).hexdigest(),
            payload=synergy_payload,
            created_at=NOW,
            fence=0,
        ))
    report = HistoricalRehearsalRunner(engine, environment="unit").run(
        "2025-26",
        cutoffs=dates,
        collect=lambda cutoff: {
            "publication_ids": {
                "player_game_logs": f"rehearsal-{cutoff.isoformat()}"
            },
            "expected_facts": {"player_game_logs": json.loads(player_log_payload)},
        },
        synergy_check=lambda cutoff: {
            "candidate_publication_id": "rehearsal-synergy",
            "expected_facts": {"synergy_play_types": json.loads(synergy_payload)},
        },
    )
    assert report.status == "passed"
    assert len(report.records) == 7
    assert not report.production_pointers_unchanged
    assert not report.production_immutability_checked
    assert report.synergy_season_status == "passed"


def test_historical_rehearsal_rejects_unordered_window(tmp_path):
    engine = _db(tmp_path)
    dates = tuple(date(2026, 4, day) for day in range(6, 12))
    report = HistoricalRehearsalRunner(engine, environment="unit").run("2025-26", cutoffs=dates)
    assert report.status == "failed"
    assert "exactly 7" in (report.error or "")


def test_failure_drills_are_deterministic_and_named():
    report = FailureDrillRunner(clock=lambda: NOW).run()
    assert report.status == "passed"
    assert {item.name for item in report.drills} == set(FailureDrillRunner.NAMES)
    assert report.drills[0].attempts == 2


def test_failure_drill_rejects_tampered_historical_publication():
    runner = FailureDrillRunner(clock=lambda: NOW)
    original_read = runner.publications.get_historical_payload

    def tamper_before_read(publication_id):
        with runner.engine.begin() as connection:
            connection.execute(
                PublicationVersion.__table__.update().where(
                    PublicationVersion.publication_id == publication_id
                ).values(payload='{"value":8}')
            )
        return original_read(publication_id)

    runner.publications.get_historical_payload = tamper_before_read
    hooks = {
        name: (lambda: {"verified": True})
        for name in FailureDrillRunner.NAMES
        if name != "provider_failure_last_good_retention"
    }
    report = runner.run(hooks=hooks)
    result = next(
        item
        for item in report.drills
        if item.name == "provider_failure_last_good_retention"
    )
    assert result.status == "failed"
    assert result.details["error"] == "ControlPlaneError"


def test_failure_drill_report_serializes_database_timestamps():
    report = FailureDrillRunner(clock=lambda: NOW).run(
        hooks={
            "isolated_restore_replay": lambda: {
                "verified": True,
                "latest_governed_cutoff": NOW,
            }
        }
    )

    encoded = json.dumps(report.to_dict())

    assert NOW.isoformat() in encoded


def test_failure_drill_database_ids_fit_postgres_uuid_columns():
    runner = FailureDrillRunner(clock=lambda: NOW)

    values = {
        runner._id("ingestion-manifest"),
        runner._id("ingestion-collector"),
        runner._id("receipt-1"),
    }

    assert len(values) == 3
    assert all(len(value) <= 36 for value in values)


def test_production_failure_drill_report_satisfies_its_required_fields():
    report = FailureDrillReport(
        status="passed",
        started_at=NOW.isoformat(),
        completed_at=(NOW + timedelta(seconds=1)).isoformat(),
        drills=(
            DrillResult(
                name="isolated_restore_replay",
                status="passed",
                attempts=1,
                details={
                    "restore_command_evidence": {"status": "succeeded"},
                    "restore_duration_ms": 123.0,
                    "recovery_time_ms": 123.0,
                    "pbp_repair_observation_id": "observation-id",
                    "pbp_repair_job_id": "job-id",
                    "recovery_data_point": {"query_duration_ms": 123.0},
                },
            ),
        ),
        environment="operator",
        production_evidence=True,
    )

    artifact = report.to_dict()

    required = artifact["artifact_schema"]["required_fields"]
    assert all(field in artifact for field in required)
    assert artifact["recovery_time_ms"] == artifact["restore_duration_ms"]


def test_url_drill_requires_out_of_band_marker_not_isolated_assertion(tmp_path):
    runner = FailureDrillRunner(
        database_url=f"sqlite:///{tmp_path / 'unsafe.sqlite3'}",
        isolated=True,
    )
    assert runner.configuration_error == "out_of_band_disposable_marker_nonce_required"


def test_marked_railway_named_disposable_target_is_allowed(tmp_path):
    from sqlalchemy import text

    database_path = tmp_path / "railway-disposable.sqlite3"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE statsplus_disposable_control ("
            "marker_nonce VARCHAR(128) NOT NULL, "
            "purpose VARCHAR(128) NOT NULL, schema_name VARCHAR(128)"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO statsplus_disposable_control "
            "(marker_nonce, purpose, schema_name) "
            "VALUES ('drill-marker', 'database_first_drill', NULL)"
        ))

    runner = FailureDrillRunner(
        database_url=database_url,
        disposable_marker_nonce="drill-marker",
        isolated=True,
    )

    assert runner.configuration_error is None


def test_database_identity_canonicalizes_sqlite_aliases(tmp_path):
    database_path = tmp_path / "identity.sqlite3"
    absolute_url = f"sqlite:///{database_path.resolve()}"
    relative_url = (
        "sqlite:///"
        + str(database_path.parent / ".." / database_path.parent.name / database_path.name)
    )

    left = connected_database_identity(absolute_url, schema=None)
    right = connected_database_identity(relative_url, schema=None)

    assert same_database_identity(left, right)


def test_runner_rejects_same_connected_drill_and_restore_target(tmp_path):
    from sqlalchemy import text

    database_path = tmp_path / "same-target.sqlite3"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE statsplus_disposable_control ("
            "marker_nonce VARCHAR(128) NOT NULL, "
            "purpose VARCHAR(128) NOT NULL, schema_name VARCHAR(128)"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO statsplus_disposable_control "
            "(marker_nonce, purpose, schema_name) "
            "VALUES ('drill-marker', 'database_first_drill', NULL)"
        ))

    runner = FailureDrillRunner(
        database_url=database_url,
        restored_database_url=(
            "sqlite:///"
            + str(database_path.parent / ".." / database_path.parent.name / database_path.name)
        ),
        disposable_marker_nonce="drill-marker",
    )

    assert runner.configuration_error == "restored_database_must_be_separate"


def test_operator_runner_requires_production_snapshot_url():
    runner = FailureDrillRunner(environment="operator")

    assert runner.configuration_error == "production_database_url_required"
    report = runner.run(require_production_evidence=True)
    assert report.status == "failed"
    assert report.production_evidence is False


def test_runner_rejects_production_alias_of_drill_target(tmp_path):
    from sqlalchemy import text

    database_path = tmp_path / "same-production.sqlite3"
    database_url = f"sqlite:///{database_path}"
    alias_url = (
        "sqlite:///"
        + str(database_path.parent / ".." / database_path.parent.name / database_path.name)
    )
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE statsplus_disposable_control ("
            "marker_nonce VARCHAR(128) NOT NULL, "
            "purpose VARCHAR(128) NOT NULL, schema_name VARCHAR(128)"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO statsplus_disposable_control "
            "(marker_nonce, purpose, schema_name) "
            "VALUES ('drill-marker', 'database_first_drill', NULL)"
        ))

    runner = FailureDrillRunner(
        database_url=database_url,
        production_database_url=alias_url,
        disposable_marker_nonce="drill-marker",
    )

    assert runner.configuration_error == (
        "drill_database_must_be_separate_from_production"
    )


def test_runner_rejects_production_alias_of_restored_target(tmp_path):
    from sqlalchemy import text

    drill_path = tmp_path / "drill.sqlite3"
    restored_path = tmp_path / "restored.sqlite3"
    drill_url = f"sqlite:///{drill_path}"
    restored_url = f"sqlite:///{restored_path}"
    production_alias = (
        "sqlite:///"
        + str(restored_path.parent / ".." / restored_path.parent.name / restored_path.name)
    )
    engine = create_engine(drill_url)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE statsplus_disposable_control ("
            "marker_nonce VARCHAR(128) NOT NULL, "
            "purpose VARCHAR(128) NOT NULL, schema_name VARCHAR(128)"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO statsplus_disposable_control "
            "(marker_nonce, purpose, schema_name) "
            "VALUES ('drill-marker', 'database_first_drill', NULL)"
        ))

    runner = FailureDrillRunner(
        database_url=drill_url,
        production_database_url=production_alias,
        restored_database_url=restored_url,
        disposable_marker_nonce="drill-marker",
    )

    assert runner.configuration_error == "restored_database_must_be_separate"


def test_unit_restore_drill_replays_outbox_and_repairs_ledger():
    report = FailureDrillRunner(clock=lambda: NOW).run()
    details = next(item.details for item in report.drills if item.name == "isolated_restore_replay")
    assert details["outbox_replayed_twice"]
    assert details["outbox_duplicate_item_idempotent"]
    assert details["pbp_repair_validated"]
    assert details["exact_checksums_validated"]
    assert details["recovery_data_point"]["latest_observation"]


def test_operator_restore_rejects_missing_governed_repair_seam(tmp_path):
    runner = FailureDrillRunner(
        engine=_db(tmp_path),
        environment="operator",
        restore_expectations={
            "pbp_repair": {
                "season": "2025-26",
                "manifest_id": "repair-manifest",
                "game_id": "known-game",
                "checksum": "c" * 64,
            }
        },
    )
    evidence = runner._restore_pbp_repair(runner.engine)
    assert evidence == {
        "verified": False,
        "reason": "governed_pbp_repair_adapter_required",
        "game_id": "known-game",
    }


def test_benchmark_emits_query_plan_and_passes_local_gate(tmp_path):
    engine = _db(tmp_path)
    report = benchmark_matchup_reads(
        engine,
        baseline=lambda: None,
        database_first=lambda: None,
        iterations=2,
    )
    assert report.passed
    assert report.query_plans


def test_benchmark_rejects_one_callable_for_both_paths(tmp_path):
    engine = _db(tmp_path)

    def read():
        return None

    with pytest.raises(ValueError, match="distinct"):
        benchmark_matchup_reads(
            engine, baseline=read, database_first=read, iterations=1
        )


def test_service_benchmark_retains_emitted_sql_and_query_ceiling(tmp_path):
    from sqlalchemy import text
    from app.services.database_first_benchmark import benchmark_matchup_services

    engine = _db(tmp_path)

    def route():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).all()
        return {"status": "ok"}

    report = benchmark_matchup_services(
        engine,
        baseline_route=lambda: route(),
        database_first_route=lambda: route(),
        season="2025-26",
        game_id="benchmark-game",
        iterations=2,
        provider_call_count=lambda: 0,
        fixture_validated=True,
        fixture_profile={"fixture_kind": "representative_fixture", "production_claim": False},
    )
    assert report.query_count == 1
    assert report.query_count_within_ceiling
    assert report.measured_query_shapes
    assert report.query_plans


def test_benchmark_rejects_unplanned_governed_full_scan():
    from app.services.database_first_benchmark import _plans_are_indexed

    statements = ((
        "SELECT payload FROM publication_versions WHERE stream_key = ?",
        ("logs",),
    ),)
    assert not _plans_are_indexed(
        (
            "SELECT payload FROM publication_versions WHERE stream_key = ?"
            " => (0, 0, 'SCAN publication_versions')",
        ),
        measured_statements=statements,
    )
