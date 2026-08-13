"""Security boundaries for the operator restore-drill wrapper."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.models.canonical_game_ledger import CanonicalGameLedgerGame
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
)
from app.services.database_first_drills import (
    FailureDrillRunner,
    PBPRepairIdentitySnapshot,
    verify_new_pbp_repair_identities,
)
from scripts.database_first_drills import _redact, _safe_command_result
from scripts import database_first_drills as drill_script
from app.services.database_first_drills import DatabaseIdentity


def test_restore_command_redaction_handles_urls_and_paired_secrets():
    command = _redact([
        "pg_restore",
        "--dbname",
        "postgresql://operator:db-password@example.invalid/stats?token=url-token",
        "--password",
        "paired-password",
        "--token=inline-token",
        "PGPASSWORD=environment-password",
        "--schema=public",
    ])

    rendered = " ".join(command)
    for secret in (
        "db-password",
        "url-token",
        "paired-password",
        "inline-token",
        "environment-password",
    ):
        assert secret not in rendered
    assert "postgresql://operator:***@example.invalid/stats" in rendered
    assert "--password <redacted>" in rendered
    assert "--token=<redacted>" in rendered
    assert "PGPASSWORD=<redacted>" in rendered


def test_command_result_projection_drops_untrusted_output_fields():
    result = _safe_command_result({
        "complete": True,
        "status": "complete",
        "secret": "must-not-be-persisted",
        "stdout": "contains-secret",
    })

    assert result == {"complete": True, "status": "complete"}


def test_pbp_repair_evidence_does_not_persist_subprocess_output(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    run_migrations(engine)
    monkeypatch.setattr(
        drill_script.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout='{"complete": false, "secret": "stdout-secret"}',
            stderr="stderr-secret",
        ),
    )
    callback = drill_script._build_pbp_repair_callback(
        [
            "ledger-refresh",
            "--database-url",
            "{database_url}",
            "--historical-repair",
        ],
        database_url="postgresql://operator:db-password@example.invalid/stats",
        spec={
            "season": "2025-26",
            "manifest_id": "manifest",
            "game_id": "game-1",
            "checksum": "c" * 64,
        },
    )

    result = callback(engine, "game-1")

    assert result["command_result"] == {"complete": False}
    rendered = repr(result)
    for secret in ("db-password", "stdout-secret", "stderr-secret"):
        assert secret not in rendered


def test_pbp_repair_discovers_new_durable_ids_from_the_restored_database(
    monkeypatch, tmp_path
):
    engine = create_engine(f"sqlite:///{tmp_path / 'restored.sqlite3'}")
    run_migrations(engine)
    now = datetime(2026, 4, 13, 12, tzinfo=timezone.utc)
    cutoff = datetime(2026, 4, 12, 23, 59, tzinfo=timezone.utc)
    game_id = "0022501234"
    manifest_id = "repair-manifest"
    expected_checksum = "e" * 64
    old_observation_id = "old-observation"
    new_observation_id = "random-new-observation"
    new_job_ids = {
        stream: f"random-{index}-job"
        for index, stream in enumerate(
            (
                "player_game_logs",
                "traditional_opponent_season",
                "traditional_opponent_l15",
                "assist_locations_season",
                "assist_locations_l15",
                "player_per36",
            ),
            start=1,
        )
    }
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id,
            season="2025-26",
            cutoff=cutoff,
            collect_before=now + timedelta(days=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="manifest-checksum",
            status="active",
            created_at=now,
        ))
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id=old_observation_id,
            client_observation_id="old-client-observation",
            collector_id="railway-ledger",
            manifest_id=manifest_id,
            environment="server",
            provider="pbp",
            observation_type="canonical_game_ledger",
            scope=json.dumps({"game_id": game_id, "surface": "canonical_game_ledger"}),
            season="2025-26",
            cutoff=cutoff,
            schema_version=1,
            checksum="o" * 64,
            payload="[]",
            payload_bytes=2,
            retrieved_at=now,
            accepted_at=now,
        ))
        connection.execute(CanonicalGameLedgerGame.__table__.insert().values(
            game_id=game_id,
            season="2025-26",
            season_type="Regular Season",
            game_date=date(2026, 4, 12),
            home_team_id=1,
            home_team_tricode="AAA",
            away_team_id=2,
            away_team_tricode="BBB",
            status="final",
            source_observation_id=old_observation_id,
            checksum="a" * 64,
            retrieved_at=now,
            updated_at=now,
        ))
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="old-job",
            stream_key="player_game_logs",
            manifest_id=manifest_id,
            season="2025-26",
            cutoff=cutoff - timedelta(days=1),
            status="queued",
            attempts=0,
            created_at=now,
            updated_at=now,
        ))

    def run_repair(*args, **kwargs):
        with engine.begin() as connection:
            connection.execute(CollectionObservation.__table__.insert().values(
                observation_id=new_observation_id,
                client_observation_id="new-client-observation",
                collector_id="railway-ledger",
                manifest_id=manifest_id,
                environment="server",
                provider="pbp",
                observation_type="canonical_game_ledger",
                scope=json.dumps({
                    "game_id": game_id,
                    "surface": "canonical_game_ledger",
                }),
                season="2025-26",
                cutoff=cutoff,
                schema_version=1,
                checksum="n" * 64,
                payload="[]",
                payload_bytes=2,
                retrieved_at=now + timedelta(minutes=1),
                accepted_at=now + timedelta(minutes=1),
            ))
            connection.execute(
                CanonicalGameLedgerGame.__table__.update()
                .where(CanonicalGameLedgerGame.game_id == game_id)
                .values(
                    source_observation_id=new_observation_id,
                    checksum=expected_checksum,
                    updated_at=now + timedelta(minutes=1),
                )
            )
            connection.execute(CompositionJob.__table__.insert(), [
                {
                    "job_id": job_id,
                    "stream_key": stream,
                    "manifest_id": manifest_id,
                    "season": "2025-26",
                    "cutoff": cutoff,
                    "status": "queued",
                    "attempts": 0,
                    "created_at": now + timedelta(minutes=1),
                    "updated_at": now + timedelta(minutes=1),
                    "last_error": None,
                }
                for stream, job_id in new_job_ids.items()
            ])
        return SimpleNamespace(returncode=0, stdout='{"complete": true}', stderr="")

    monkeypatch.setattr(drill_script.subprocess, "run", run_repair)
    expectations = {
        "season": "2025-26",
        "manifest_id": manifest_id,
        "game_id": game_id,
        "checksum": expected_checksum,
    }
    callback = drill_script._build_pbp_repair_callback(
        ["ledger-refresh", "{season}", "--database-url", "{database_url}"],
        database_url=str(engine.url),
        spec=expectations,
    )
    runner = FailureDrillRunner(
        engine=engine,
        restore_expectations={"pbp_repair": expectations},
        pbp_repair=callback,
    )

    evidence = runner._restore_pbp_repair(engine)

    assert evidence["verified"] is True
    assert evidence["observation_id"] == new_observation_id
    assert set(evidence["composition_job_ids"]) == set(new_job_ids.values())
    assert old_observation_id not in repr(evidence)
    assert "old-job" not in repr(evidence)

    monkeypatch.setattr(
        drill_script.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"complete": true}',
            stderr="",
        ),
    )
    replay_callback = drill_script._build_pbp_repair_callback(
        ["ledger-refresh", "{season}", "--database-url", "{database_url}"],
        database_url=str(engine.url),
        spec=expectations,
    )
    replay_evidence = FailureDrillRunner(
        engine=engine,
        restore_expectations={"pbp_repair": expectations},
        pbp_repair=replay_callback,
    )._restore_pbp_repair(engine)

    assert replay_evidence["verified"] is False
    assert replay_evidence["observation_id"] == ""
    assert replay_evidence["composition_job_ids"] == ()


@pytest.mark.parametrize(
    ("observation_changes", "manifest_changes", "expected_verified"),
    (
        pytest.param({}, {}, True, id="accepted-control"),
        pytest.param({"accepted_at": None}, {}, False, id="unaccepted"),
        pytest.param(
            {"retrieved_at": "2026-04-14 00:00:00.000000"},
            {},
            False,
            id="retrieved-at-deadline",
        ),
        pytest.param(
            {"accepted_at": "2026-04-14 00:00:00.000000"},
            {},
            False,
            id="accepted-at-deadline",
        ),
        pytest.param({"schema_version": 2}, {}, False, id="unauthorized-schema"),
        pytest.param(
            {"scope": '{"surface":"canonical_game_ledger","game_id":"other"}'},
            {},
            False,
            id="wrong-game-scope",
        ),
        pytest.param({"environment": "operator"}, {}, False, id="wrong-environment"),
        pytest.param({}, {"scopes": '["other"]'}, False, id="manifest-scope-missing"),
    ),
)
def test_pbp_repair_rejects_observation_without_manifest_acceptance(
    tmp_path, observation_changes, manifest_changes, expected_verified
):
    engine = create_engine(f"sqlite:///{tmp_path / 'acceptance.sqlite3'}")
    cutoff = "2026-04-12 23:59:00.000000"
    collect_before = "2026-04-14 00:00:00.000000"
    observation = {
        "observation_id": "new-observation",
        "manifest_id": "repair-manifest",
        "environment": "server",
        "provider": "pbp",
        "observation_type": "canonical_game_ledger",
        "scope": '{"surface":"canonical_game_ledger","game_id":"0022501234"}',
        "season": "2025-26",
        "cutoff": cutoff,
        "schema_version": 1,
        "retrieved_at": "2026-04-13 12:00:00.000000",
        "accepted_at": "2026-04-13 12:00:00.000000",
    }
    observation.update(observation_changes)
    manifest = {
        "manifest_id": "repair-manifest",
        "season": "2025-26",
        "cutoff": cutoff,
        "collect_before": collect_before,
        "accepted_versions": "[1]",
        "scopes": '["canonical_game_ledger"]',
        "status": "active",
    }
    manifest.update(manifest_changes)
    streams = (
        "player_game_logs",
        "traditional_opponent_season",
        "traditional_opponent_l15",
        "assist_locations_season",
        "assist_locations_l15",
        "player_per36",
    )
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE collection_manifests ("
            "manifest_id TEXT, season TEXT, cutoff DATETIME, collect_before DATETIME, "
            "accepted_versions TEXT, scopes TEXT, status TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE collection_observations ("
            "observation_id TEXT, manifest_id TEXT, environment TEXT, provider TEXT, "
            "observation_type TEXT, scope TEXT, season TEXT, cutoff DATETIME, "
            "schema_version INTEGER, retrieved_at DATETIME, accepted_at DATETIME)"
        ))
        connection.execute(text(
            "CREATE TABLE canonical_game_ledger_games ("
            "game_id TEXT, season TEXT, source_observation_id TEXT, checksum TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE composition_jobs ("
            "job_id TEXT, stream_key TEXT, manifest_id TEXT, season TEXT, "
            "cutoff DATETIME, status TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO collection_manifests VALUES ("
            ":manifest_id, :season, :cutoff, :collect_before, :accepted_versions, "
            ":scopes, :status)"
        ), manifest)
        connection.execute(text(
            "INSERT INTO collection_observations VALUES ("
            ":observation_id, :manifest_id, :environment, :provider, "
            ":observation_type, :scope, :season, :cutoff, :schema_version, "
            ":retrieved_at, :accepted_at)"
        ), observation)
        connection.execute(text(
            "INSERT INTO canonical_game_ledger_games VALUES ("
            "'0022501234', '2025-26', 'new-observation', :checksum)"
        ), {"checksum": "e" * 64})
        connection.execute(text(
            "INSERT INTO composition_jobs VALUES ("
            ":job_id, :stream_key, 'repair-manifest', '2025-26', :cutoff, 'queued')"
        ), [
            {"job_id": f"new-{index}", "stream_key": stream, "cutoff": cutoff}
            for index, stream in enumerate(streams)
        ])

    evidence = verify_new_pbp_repair_identities(
        engine,
        season="2025-26",
        manifest_id="repair-manifest",
        game_id="0022501234",
        checksum="e" * 64,
        before=PBPRepairIdentitySnapshot(frozenset(), frozenset()),
    )

    assert evidence["verified"] is expected_verified, evidence


def test_restore_report_omits_raw_subprocess_output(monkeypatch, tmp_path):
    backup = tmp_path / "restore.backup"
    backup.write_bytes(b"backup")
    expectations = tmp_path / "expectations.json"
    expectations.write_text(
        json.dumps({
            "pbp_repair": {
                "season": "2025-26",
                "manifest_id": "m",
                "game_id": "game-1",
                "checksum": "c" * 64,
            }
        }),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    captured: dict[str, object] = {}
    identity = DatabaseIdentity("postgresql", "server", 5432, "db", "public")
    monkeypatch.setattr(drill_script, "preflight_disposable_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(drill_script, "connected_database_identity", lambda *args, **kwargs: identity)
    monkeypatch.setattr(drill_script, "same_database_identity", lambda *args: False)
    monkeypatch.setattr(
        drill_script.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"complete": true, "secret": "restore-output-secret"}',
            stderr="restore-error-secret",
        ),
    )

    def fake_run_failure_drills(**kwargs):
        captured.update(kwargs)
        return {"status": "passed"}

    monkeypatch.setattr(drill_script, "run_failure_drills", fake_run_failure_drills)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "database_first_drills.py",
            "--database-url",
            "postgresql://operator:drill-password@server/db",
            "--production-database-url",
            "postgresql://operator:production-password@server/source",
            "--restored-database-url",
            "postgresql://operator:restore-password@server/restore",
            "--marker-nonce",
            "drill-marker",
            "--schema",
            "public",
            "--restored-marker-nonce",
            "restore-marker",
            "--restored-schema",
            "public",
            "--restore-expectations",
            str(expectations),
            "--backup-artifact",
            str(backup),
            "--restore-command",
            json.dumps(["pg_restore", "--dbname={database_url}", "{backup_artifact}"]),
            "--pbp-repair-command",
            json.dumps(["python", "--database-url", "{database_url}", "--historical-repair"]),
            "--report",
            str(report_path),
        ],
    )

    assert drill_script.main() == 0
    evidence = captured["restore_command_evidence"]
    assert isinstance(evidence, dict)
    assert "stdout_tail" not in evidence
    assert "stderr_tail" not in evidence
    rendered = json.dumps(evidence)
    for secret in (
        "drill-password",
        "production-password",
        "restore-password",
        "restore-output-secret",
        "restore-error-secret",
    ):
        assert secret not in rendered


def test_operator_cli_requires_an_explicit_production_snapshot_url(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "database_first_drills.py",
            "--database-url",
            "postgresql://operator:drill-password@example.invalid/drill",
            "--marker-nonce",
            "drill-marker",
            "--report",
            str(tmp_path / "report.json"),
        ],
    )

    with pytest.raises(SystemExit):
        drill_script.main()

    assert "--production-database-url" in capsys.readouterr().err
