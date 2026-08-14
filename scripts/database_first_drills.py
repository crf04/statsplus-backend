#!/usr/bin/env python3
"""Run deterministic failure and isolated restore/replay drills.

Operator mode deliberately owns the restore boundary.  It preflights an
out-of-band marked, empty Postgres target, invokes a caller-provided argv
restore command with ``shell=False``, and only then asks the application drill
runner to query and repair that target.  A URL that merely points at an
already-restored database cannot produce production evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.engine import make_url

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.database_first_drills import (
    capture_pbp_repair_identity_snapshot,
    connected_database_identity,
    preflight_disposable_database,
    run_failure_drills,
    same_database_identity,
    verify_new_pbp_repair_identities,
)


def _command(value: str, *, label: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} must be a JSON argv array") from error
    if (
        not isinstance(parsed, list)
        or not parsed
        or any(not isinstance(item, str) or not item.strip() for item in parsed)
    ):
        raise ValueError(f"{label} must be a non-empty JSON argv array")
    return parsed


def _render(command: list[str], *, replacements: Mapping[str, str]) -> list[str]:
    return [
        value.format_map({key: str(replacements.get(key, "")) for key in replacements})
        for value in command
    ]


_SENSITIVE_OPTIONS = frozenset(
    {
        "--access-token",
        "--api-key",
        "--apikey",
        "--credential",
        "--database-url",
        "--dbname",
        "--dsn",
        "--password",
        "--production-database-url",
        "--refresh-token",
        "--restored-database-url",
        "--secret",
        "--token",
        "-p",
    }
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "credential",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)
_SENSITIVE_ASSIGNMENT_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "credential",
        "database_url",
        "password",
        "pgpassword",
        "production_database_url",
        "refresh_token",
        "restored_database_url",
        "secret",
        "token",
    }
)
_SAFE_COMMAND_STATUSES = frozenset(
    {"complete", "completed", "error", "failed", "ok", "success", "succeeded"}
)
_URL_OPTIONS = frozenset(
    {
        "--database-url",
        "--dbname",
        "--dsn",
        "--production-database-url",
        "--restored-database-url",
    }
)
_URL_START = re.compile(r"(?i)(?:postgres(?:ql)?|rediss?|https?)://")
_URL_PASSWORD = re.compile(
    r"(?i)(?P<scheme>(?:postgres(?:ql)?|rediss?|https?)://)"
    r"(?P<user>[^/@\s:]+):(?P<password>[^/@\s]*)@"
)


def _redact_url(value: str) -> str:
    """Keep URL routing facts while removing password and secret queries."""

    try:
        rendered = make_url(value).render_as_string(hide_password=True)
    except Exception:
        rendered = _URL_PASSWORD.sub(r"\g<scheme>\g<user>:<redacted>@", value)
    parsed = urlsplit(rendered)
    if not parsed.scheme or not parsed.netloc:
        return rendered
    query = urlencode(
        [
            (
                key,
                "<redacted>"
                if key.casefold() in _SENSITIVE_QUERY_KEYS
                else item,
            )
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit(parsed._replace(query=query))


def _redact_url_fragment(value: str) -> str:
    """Redact a URL embedded in an argv token such as ``--dsn=URL``."""

    match = _URL_START.search(value)
    if match is None:
        return value
    start = match.start()
    return value[:start] + _redact_url(value[start:])


def _option_name(value: str) -> str:
    return value.partition("=")[0].casefold()


def _is_sensitive_option(value: str) -> bool:
    option = _option_name(value)
    if option in _SENSITIVE_OPTIONS:
        return True
    if option.removeprefix("--") in _SENSITIVE_ASSIGNMENT_NAMES:
        return True
    if option.startswith("--"):
        return option.removeprefix("--") in {
            name.removeprefix("--") for name in _SENSITIVE_OPTIONS if name.startswith("--")
        }
    return False


def _safe_command_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Retain only bounded status facts from untrusted command output."""

    safe: dict[str, Any] = {}
    complete = result.get("complete")
    if isinstance(complete, bool):
        safe["complete"] = complete
    status = result.get("status")
    if isinstance(status, str) and status.casefold() in _SAFE_COMMAND_STATUSES:
        safe["status"] = status.casefold()
    return safe


def _redact(command: list[str]) -> list[str]:
    """Redact URL credentials and both inline and paired secret arguments."""

    redacted: list[str] = []
    redact_next: str | None = None
    for value in command:
        if redact_next is not None:
            redacted.append(
                _redact_url_fragment(value) if redact_next == "url" else "<redacted>"
            )
            redact_next = None
            continue
        if _is_sensitive_option(value):
            key, separator, _ = value.partition("=")
            if separator:
                if key.casefold() in _URL_OPTIONS:
                    redacted.append(f"{key}={_redact_url_fragment(value.split('=', 1)[1])}")
                else:
                    redacted.append(f"{key}=<redacted>")
            else:
                redacted.append(value)
                redact_next = "url" if value.casefold() in _URL_OPTIONS else "secret"
            continue
        assignment_key, separator, _ = value.partition("=")
        if separator and _is_sensitive_option(assignment_key):
            redacted.append(f"{assignment_key}=<redacted>")
            continue
        redacted.append(_redact_url_fragment(value))
    return redacted


def _json_output(stdout: str) -> Mapping[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    return {}


def _failed_report(*, reason: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "failed",
        "environment": "postgres_isolated",
        "production_evidence": False,
        "artifact_schema": {
            "version": 1,
            "engine": "postgresql",
            "required_fields": [
                "restore_command_evidence",
                "recovery_time_ms",
                "pbp_repair_observation_id",
                "pbp_repair_job_id",
            ],
        },
        "error": reason,
        "restore_command_evidence": dict(evidence),
    }


def _build_pbp_repair_callback(
    command: list[str],
    *,
    database_url: str,
    spec: Mapping[str, Any],
):
    """Invoke a real historical-repair seam and verify its durable IDs.

    The command is normally ``scripts/ledger_refresh.py`` with provider
    credentials supplied by the operator environment.  The callback performs
    only read-side verification after the command; it never patches a ledger
    row directly.
    """

    season = str(spec.get("season", ""))
    expected_manifest = str(spec.get("manifest_id", ""))
    expected_game_id = str(spec.get("game_id", ""))
    expected_checksum = str(spec.get("checksum", ""))
    if (
        not season
        or not expected_manifest
        or not expected_game_id
        or len(expected_checksum) != 64
    ):
        raise ValueError(
            "pbp_repair season, manifest_id, game_id, and checksum are required"
        )

    def repair(engine, game_id: str) -> Mapping[str, Any]:
        if game_id != expected_game_id:
            return {
                "verified": False,
                "adapter": "ledger_refresh_historical_repair",
                "reason": "governed_repair_game_mismatch",
            }
        before = capture_pbp_repair_identity_snapshot(engine)
        rendered = _render(
            command,
            replacements={
                "database_url": database_url,
                "game_id": game_id,
                "season": season,
            },
        )
        completed = subprocess.run(
            rendered,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        command_result = _json_output(completed.stdout)
        if completed.returncode != 0 or not (
            bool(command_result.get("complete"))
            or str(command_result.get("status", "")) == "complete"
        ):
            return {
                "verified": False,
                "adapter": "ledger_refresh_historical_repair",
                "command": _redact(rendered),
                "returncode": completed.returncode,
                "command_result": _safe_command_result(command_result),
            }
        verified = verify_new_pbp_repair_identities(
            engine,
            season=season,
            manifest_id=expected_manifest,
            game_id=game_id,
            checksum=expected_checksum,
            before=before,
        )
        if not verified.get("verified"):
            return {
                "verified": False,
                "adapter": "ledger_refresh_historical_repair",
                "command": _redact(rendered),
                "returncode": completed.returncode,
                "reason": str(
                    verified.get(
                        "reason",
                        "governed_repair_observation_or_job_missing",
                    )
                ),
            }
        return {
            "verified": True,
            "adapter": "ledger_refresh_historical_repair",
            "command": _redact(rendered),
            "returncode": completed.returncode,
            "game_id": game_id,
            "observation_id": str(verified["observation_id"]),
            "composition_job_id": str(verified["composition_job_id"]),
            "composition_job_ids": tuple(verified["composition_job_ids"]),
            "checksum": expected_checksum,
            "updated_rows": 1,
            "manifest_id": expected_manifest,
            "command_result": _safe_command_result(command_result),
        }

    return repair


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--sqlite-unit",
        action="store_true",
        help="run the explicit local SQLite adapter drill (not production evidence)",
    )
    parser.add_argument("--production-database-url")
    parser.add_argument("--restored-database-url")
    parser.add_argument("--marker-nonce", default=os.environ.get("STATPLUS_DISPOSABLE_MARKER_NONCE"))
    parser.add_argument("--schema", default=os.environ.get("STATPLUS_DISPOSABLE_SCHEMA"))
    parser.add_argument("--restored-marker-nonce", default=os.environ.get("STATPLUS_RESTORED_MARKER_NONCE"))
    parser.add_argument("--restored-schema", default=os.environ.get("STATPLUS_RESTORED_SCHEMA"))
    parser.add_argument("--restore-expectations", type=Path)
    parser.add_argument(
        "--backup-artifact",
        type=Path,
        help="backup artifact consumed by the operator restore command",
    )
    parser.add_argument(
        "--restore-command",
        help="JSON argv array; use {backup_artifact}, {database_url}, and {schema} placeholders",
    )
    parser.add_argument(
        "--pbp-repair-command",
        help="JSON argv array for the governed historical repair seam; use {database_url}, {game_id}, and {season}",
    )
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if args.sqlite_unit and not str(args.database_url).startswith("sqlite"):
        parser.error("--sqlite-unit requires a SQLite drill database")
    if str(args.database_url).startswith("sqlite") and not args.sqlite_unit:
        parser.error("SQLite drills require explicit --sqlite-unit")
    if not args.marker_nonce:
        parser.error("--marker-nonce or STATPLUS_DISPOSABLE_MARKER_NONCE is required")
    if not args.sqlite_unit:
        if not args.production_database_url:
            parser.error("operator drills require --production-database-url")
        if not args.restored_database_url:
            parser.error("operator drills require --restored-database-url")
        if not args.restored_marker_nonce:
            parser.error("--restored-marker-nonce is required for operator restore evidence")
        if not args.restored_schema:
            parser.error("--restored-schema is required for operator restore evidence")
        if not args.backup_artifact:
            parser.error("operator drills require --backup-artifact")
        if not args.restore_command:
            parser.error("operator drills require --restore-command")
        if not args.pbp_repair_command:
            parser.error("operator drills require --pbp-repair-command")

    expectations = None
    if args.restore_expectations:
        try:
            expectations = json.loads(args.restore_expectations.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            parser.error(f"cannot read --restore-expectations: {error}")
        if not isinstance(expectations, dict):
            parser.error("--restore-expectations must contain a JSON object")
    if not args.sqlite_unit and (
        not isinstance(expectations, dict)
        or not isinstance(expectations.get("pbp_repair"), Mapping)
    ):
        parser.error("operator restore expectations require a pbp_repair object")

    restore_evidence: dict[str, Any] = {}
    restore_started: float | None = None
    pbp_repair = None
    if not args.sqlite_unit:
        try:
            backup = args.backup_artifact.resolve(strict=True)
            if not backup.is_file():
                raise ValueError("backup_artifact_must_be_a_file")
            restore_command = _command(args.restore_command, label="--restore-command")
            pbp_command = _command(args.pbp_repair_command, label="--pbp-repair-command")
            if not any("{backup_artifact}" in token for token in restore_command):
                raise ValueError("restore command must consume {backup_artifact}")
            if not any("{database_url}" in token for token in restore_command):
                raise ValueError("restore command must target {database_url}")
            if "--historical-repair" not in pbp_command:
                raise ValueError("pbp repair command must invoke --historical-repair")
            preflight_disposable_database(
                str(args.restored_database_url),
                marker_nonce=str(args.restored_marker_nonce),
                schema=str(args.restored_schema),
                label="restored",
                require_empty=True,
            )
            drill_identity = connected_database_identity(
                str(args.database_url),
                schema=str(args.schema) if args.schema else None,
            )
            restored_identity = connected_database_identity(
                str(args.restored_database_url),
                schema=str(args.restored_schema),
            )
            if same_database_identity(drill_identity, restored_identity):
                raise ValueError("restored target must be separate from drill database")
            if args.production_database_url:
                production_identity = connected_database_identity(
                    str(args.production_database_url),
                    schema=None,
                )
                if same_database_identity(production_identity, restored_identity):
                    raise ValueError("restored target must be separate from production database")
                if same_database_identity(production_identity, drill_identity):
                    raise ValueError("drill target must be separate from production database")
            rendered_restore = _render(
                restore_command,
                replacements={
                    "backup_artifact": str(backup),
                    "database_url": str(args.restored_database_url),
                    "schema": str(args.restored_schema),
                },
            )
            restore_started = perf_counter()
            started_at = datetime.now(timezone.utc).isoformat()
            completed = subprocess.run(
                rendered_restore,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
            )
            completed_at = datetime.now(timezone.utc).isoformat()
            restore_evidence = {
                "status": "succeeded" if completed.returncode == 0 else "failed",
                "backup_artifact": backup.name,
                "command": _redact(rendered_restore),
                "returncode": completed.returncode,
                "started_at": started_at,
                "completed_at": completed_at,
                "output_captured": bool(completed.stdout or completed.stderr),
                "command_result": _safe_command_result(_json_output(completed.stdout)),
            }
            if completed.returncode != 0:
                report = _failed_report(reason="restore_command_failed", evidence=restore_evidence)
                Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print(json.dumps(report, sort_keys=True))
                return 1
            pbp_repair = _build_pbp_repair_callback(
                pbp_command,
                database_url=str(args.restored_database_url),
                spec=expectations["pbp_repair"],
            )
        except (OSError, ValueError) as error:
            report = _failed_report(reason=str(error), evidence=restore_evidence)
            Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(report, sort_keys=True))
            return 1

    report = run_failure_drills(
        database_url=args.database_url,
        environment="unit" if args.sqlite_unit else "operator",
        isolated=False,
        production_database_url=args.production_database_url,
        restored_database_url=args.restored_database_url,
        disposable_marker_nonce=args.marker_nonce,
        disposable_schema=args.schema,
        restored_marker_nonce=args.restored_marker_nonce,
        restored_schema=args.restored_schema,
        restore_expectations=expectations,
        pbp_repair=pbp_repair,
        restore_started=restore_started,
        restore_command_evidence=restore_evidence,
        require_production_evidence=not args.sqlite_unit,
    )
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
