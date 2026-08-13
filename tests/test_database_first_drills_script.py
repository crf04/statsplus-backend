"""Security boundaries for the operator restore-drill wrapper."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

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
            "composition_job_id": "job",
        },
    )

    result = callback(None, "game-1")

    assert result["command_result"] == {"complete": False}
    rendered = repr(result)
    for secret in ("db-password", "stdout-secret", "stderr-secret"):
        assert secret not in rendered


def test_restore_report_omits_raw_subprocess_output(monkeypatch, tmp_path):
    backup = tmp_path / "restore.backup"
    backup.write_bytes(b"backup")
    expectations = tmp_path / "expectations.json"
    expectations.write_text(
        json.dumps({"pbp_repair": {"season": "2025-26", "manifest_id": "m", "composition_job_id": "j"}}),
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
