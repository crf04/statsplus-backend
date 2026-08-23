"""Operator entry-point contracts for scheduled projection collection."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import collect_projections, projection_collection_service


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_railway_service_script_imports_when_executed_by_file_path(tmp_path):
    script = REPOSITORY_ROOT / "scripts" / "projection_collection_service.py"
    scripts_directory = script.parent
    command = (
        "import runpy, sys; "
        f"root={str(REPOSITORY_ROOT)!r}; "
        f"sys.path=[{str(scripts_directory)!r}] + [p for p in sys.path if p != root]; "
        f"runpy.run_path({str(script)!r}, run_name='projection_collection_service_entry')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_one_shot_cli_and_railway_loop_share_one_coordinator_path(monkeypatch, capsys):
    dependency_builds = []
    coordinator_runs = []

    class Coordinator:
        def run(self):
            coordinator_runs.append("run")
            return SimpleNamespace(status="complete", reason="collected")

    settings = SimpleNamespace(nba=SimpleNamespace(current_season="2025-26"))
    monkeypatch.setattr(collect_projections, "load_settings", lambda **_kwargs: settings)

    def build_dependencies(candidate_settings):
        dependency_builds.append(candidate_settings)
        return SimpleNamespace(projection_collection_coordinator=Coordinator())

    monkeypatch.setattr(collect_projections, "build_dependencies", build_dependencies)

    assert (
        collect_projections.main(
            ["--database-url", "postgresql://collector.example/statsplus"]
        )
        == 0
    )
    assert capsys.readouterr().out == "complete: collected\n"

    class TwoLoopsComplete(Exception):
        pass

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://collector.example/statsplus"
    )
    sleeps = []

    def sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 2:
            raise TwoLoopsComplete

    monkeypatch.setattr(projection_collection_service.time, "sleep", sleep)

    with pytest.raises(TwoLoopsComplete):
        projection_collection_service.run()

    assert dependency_builds == [settings, settings]
    assert coordinator_runs == ["run", "run", "run"]
    assert sleeps == [300, 300]


def test_cli_rejects_a_non_current_season_before_dependency_or_provider_work(
    monkeypatch,
):
    settings = SimpleNamespace(nba=SimpleNamespace(current_season="2025-26"))
    monkeypatch.setattr(collect_projections, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(
        collect_projections,
        "build_dependencies",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("dependencies must not be built for an invalid season")
        ),
    )

    with pytest.raises(SystemExit) as error:
        collect_projections.main(
            [
                "--database-url",
                "postgresql://collector.example/statsplus",
                "--season",
                "2024-25",
            ]
        )

    assert error.value.code == 2


def test_one_shot_cli_exits_nonzero_when_every_provider_collection_fails(
    monkeypatch,
):
    coordinator = SimpleNamespace(
        run=lambda: SimpleNamespace(
            status="partial",
            reason="provider_collection_failed",
        )
    )
    settings = SimpleNamespace(nba=SimpleNamespace(current_season="2025-26"))
    monkeypatch.setattr(collect_projections, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(
        collect_projections,
        "build_dependencies",
        lambda _settings: SimpleNamespace(
            projection_collection_coordinator=coordinator
        ),
    )

    exit_code = collect_projections.main(
        ["--database-url", "postgresql://collector.example/statsplus"]
    )

    assert exit_code == 1
