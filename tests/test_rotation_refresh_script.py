"""The rotation-refresh entry point prints its summary and sets the exit status."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import rotation_refresh


class _Engine:
    def dispose(self):
        pass


def _run(monkeypatch, capsys, tmp_path, result, *argv):
    import app.dependencies as dependencies

    calls = []

    def collect(season, **options):
        calls.append((season, options))
        return result

    monkeypatch.setattr(
        dependencies,
        "build_dependencies",
        lambda settings: SimpleNamespace(
            rotation_collection_service=SimpleNamespace(collect=collect), engine=_Engine()
        ),
    )
    code = rotation_refresh.main(
        [
            "2025-26",
            "--database-url",
            f"sqlite:///{tmp_path / 'r.sqlite3'}",
            *argv,
        ]
    )
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1]), calls


def test_a_clean_run_prints_the_summary_and_exits_zero(monkeypatch, capsys, tmp_path):
    summary = {"stored": 3, "rejected": [{"game_id": "0022500777", "reason": "x"}], "failed": []}

    code, printed, calls = _run(
        monkeypatch,
        capsys,
        tmp_path,
        summary,
        "--max-games",
        "10",
        "--throttle",
        "2",
    )

    assert (code, printed) == (0, summary)
    assert calls == [("2025-26", {"max_games": 10, "throttle_seconds": 2.0, "game_ids": None})]


def test_a_run_with_unfetchable_games_exits_one_so_the_scheduler_flags_it(monkeypatch, capsys, tmp_path):
    summary = {"stored": 0, "rejected": [], "failed": [{"game_id": "0022500666", "error": "x"}]}

    code, printed, _ = _run(monkeypatch, capsys, tmp_path, summary, "--game-id", "0022500666")

    assert code == 1
    assert printed == summary


def test_the_script_runs_as_a_file_from_the_repository_root():
    completed = subprocess.run(
        [sys.executable, "scripts/rotation_refresh.py", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0
    assert "--max-games" in completed.stdout


@pytest.mark.parametrize("argv", [[], ["2025-26"]])
def test_season_and_database_url_are_required(argv):
    with pytest.raises(SystemExit) as exit_info:
        rotation_refresh.main(argv)

    assert exit_info.value.code == 2
