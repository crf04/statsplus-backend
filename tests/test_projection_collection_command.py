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
    calls: list[tuple[str, ...]] = []

    class Coordinator:
        def run(self):
            calls.append(("coordinator",))
            return SimpleNamespace(status="complete", reason="collected")

    settings = SimpleNamespace(nba=SimpleNamespace(current_season="2025-26"))
    monkeypatch.setattr(collect_projections, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(
        collect_projections,
        "build_dependencies",
        lambda _settings: SimpleNamespace(
            projection_collection_coordinator=Coordinator()
        ),
    )

    assert (
        collect_projections.main(
            ["--database-url", "postgresql://collector.example/statsplus"]
        )
        == 0
    )
    assert capsys.readouterr().out == "complete: collected\n"

    class LoopComplete(Exception):
        pass

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://collector.example/statsplus"
    )
    monkeypatch.setattr(
        projection_collection_service,
        "main",
        lambda argv: calls.append(tuple(argv)) or 0,
    )
    monkeypatch.setattr(
        projection_collection_service.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(LoopComplete),
    )

    with pytest.raises(LoopComplete):
        projection_collection_service.run()

    assert calls == [
        ("coordinator",),
        ("--database-url", "postgresql://collector.example/statsplus"),
    ]


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
