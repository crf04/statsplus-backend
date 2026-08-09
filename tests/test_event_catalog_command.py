"""Offline tests for the one-shot event catalog command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from scripts import refresh_event_catalog


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nba_stats" / "schedule.valid.json"


def test_command_refreshes_writable_database_from_recorded_fixture(
    tmp_path, monkeypatch, capsys
):
    database_url = f"sqlite:///{tmp_path / 'command.sqlite3'}"
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")

    assert (
        refresh_event_catalog.main(
            [
                "--database-url",
                database_url,
                "--season",
                "2025-26",
                "--fixture",
                str(FIXTURE_PATH),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["results"] == [{"event_count": 2, "refreshed_at": output["results"][0]["refreshed_at"], "season": "2025-26"}]
    assert output["failures"] == {}
    assert inspect(create_engine(database_url)).has_table("event_catalog")


def test_command_requires_explicit_database_target_and_season(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as error:
        refresh_event_catalog.main([])
    assert error.value.code == 2


def test_command_rejects_demo_database(monkeypatch, capsys):
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    with pytest.raises(SystemExit) as error:
        refresh_event_catalog.main(
            ["--database-url", "sqlite:///nba_play_types.db", "--season", "2025-26"]
        )
    assert error.value.code == 2
    assert "read-only demo" in capsys.readouterr().err


def test_command_rejects_noncanonical_season(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    with pytest.raises(SystemExit) as error:
        refresh_event_catalog.main(
            [
                "--database-url",
                f"sqlite:///{tmp_path / 'invalid.sqlite3'}",
                "--season",
                "current",
            ]
        )
    assert error.value.code == 2
    assert "canonical NBA season" in capsys.readouterr().err


def test_command_accepts_repeatable_seasons_and_reports_each_result(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path / 'multi.sqlite3'}"
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    assert refresh_event_catalog.main([
        "--database-url", database_url, "--season", "2025-26", "--season", "2024-25",
        "--season", "2025-26", "--fixture", str(FIXTURE_PATH)
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert [item["season"] for item in output["results"]] == ["2025-26"]
    assert set(output["failures"]) == {"2024-25"}
