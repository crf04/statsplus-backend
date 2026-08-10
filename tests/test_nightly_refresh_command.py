"""Offline behavior of the Railway Nightly Refresh process command."""

import json

import pytest

from scripts import nightly_refresh
from scripts.nightly_refresh import run_nightly_refresh


def test_nightly_refresh_runs_stats_schedule_catalog_then_player_logs_once_on_success():
    calls = []

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 0
    )
    assert calls == ["stats", "schedule", "athlete_catalog", "player_game_logs"]


def test_nightly_refresh_retries_the_whole_unit_exactly_once(capsys):
    calls = []
    stats_results = iter([False, True])

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or next(stats_results),
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
    ]
    assert "attempt 1 failed during stats refresh; retrying" in capsys.readouterr().err


def test_nightly_refresh_keeps_schedule_before_retrying_athlete_catalog(capsys):
    calls = []
    athlete_results = iter([False, True])

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog")
            or next(athlete_results),
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
    ]
    assert "attempt 1 failed during athlete catalog refresh; retrying" in (
        capsys.readouterr().err
    )


def test_nightly_refresh_reports_athlete_failure_without_running_player_logs(capsys):
    calls = []

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or False,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 1
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "stats",
        "schedule",
        "athlete_catalog",
    ]
    diagnostics = capsys.readouterr().err
    assert "attempt 1 failed during athlete catalog refresh; retrying" in diagnostics
    assert (
        "attempt 2 failed during athlete catalog refresh; no retries remain"
        in diagnostics
    )


def test_nightly_refresh_returns_failure_after_two_attempts(capsys):
    calls = []

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule")
            or (_ for _ in ()).throw(RuntimeError("offline failure")),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 1
    )
    assert calls == [
        "stats",
        "schedule",
        "stats",
        "schedule",
    ]
    diagnostics = capsys.readouterr().err
    assert "attempt 1 failed during schedule refresh; retrying" in diagnostics
    assert "attempt 2 failed during schedule refresh; no retries remain" in diagnostics
    assert "offline failure" not in diagnostics


def test_nightly_refresh_retries_whole_unit_when_player_logs_fail(capsys):
    calls = []
    log_attempts = iter([RuntimeError("offline failure"), None])

    def refresh_logs():
        calls.append("player_game_logs")
        error = next(log_attempts)
        if error is not None:
            raise error

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=refresh_logs,
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
    ]
    assert "failed during player game logs refresh" in capsys.readouterr().err


def test_main_reports_success_without_live_calls(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path / 'nightly.sqlite3'}"
    monkeypatch.setattr(nightly_refresh, "_run", lambda value: 0)

    assert nightly_refresh.main(["--database-url", database_url]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "succeeded"}


def test_main_reports_failure_without_live_calls(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path / 'nightly.sqlite3'}"
    monkeypatch.setattr(nightly_refresh, "_run", lambda value: 1)

    assert nightly_refresh.main(["--database-url", database_url]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}


def test_main_requires_database_target(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as raised:
        nightly_refresh.main([])

    assert raised.value.code == 2


def test_main_rejects_demo_database(capsys):
    with pytest.raises(SystemExit) as raised:
        nightly_refresh.main(["--database-url", "sqlite:///nba_play_types.db"])

    assert raised.value.code == 2
    assert "read-only demo" in capsys.readouterr().err
