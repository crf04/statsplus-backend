"""The operator command for projection archive retention (#502)."""

from __future__ import annotations

import json
from functools import partial

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from scripts import projection_retention as command
from tests.services.test_projection_retention import (
    EXPECTED_PROTECTED,
    EXPECTED_TABLES,
    NOW,
    PRUNED,
    SEEDED,
    Events,
    observations,
    seed,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'retention-command.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    seed(engine)
    engine.dispose()
    monkeypatch.setattr(command, "_CatalogEvents", lambda repository: Events())
    monkeypatch.setattr(
        command,
        "ProjectionArchiveRetention",
        partial(command.ProjectionArchiveRetention, clock=lambda: NOW),
    )
    return url


def _rows(url):
    engine = create_engine(url)
    try:
        return observations(engine)
    finally:
        engine.dispose()


def test_dry_run_prints_the_report_and_writes_nothing(database, capsys):
    assert command.main(["--database-url", database, "--dry-run"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert (report["mode"], report["outcome"], report["cutoff"]) == (
        "dry_run",
        "succeeded",
        "2026-10-17T12:00:00+00:00",
    )
    assert report["tables"] == EXPECTED_TABLES
    assert report["protected"] == EXPECTED_PROTECTED
    assert report["history"] == {"dabble": {"rows": 2, "unkeyed": 0}, "underdog": {"rows": 6, "unkeyed": 0}}
    assert _rows(database) == SEEDED


def test_execute_prunes(database, capsys):
    assert command.main(["--database-url", database, "--execute"]) == 0

    assert json.loads(capsys.readouterr().out)["mode"] == "run"
    assert _rows(database) == PRUNED


def test_a_mode_is_required(database, capsys):
    with pytest.raises(SystemExit):
        command.main(["--database-url", database])
    assert "one of the arguments --dry-run --execute is required" in capsys.readouterr().err
    assert _rows(database) == SEEDED
