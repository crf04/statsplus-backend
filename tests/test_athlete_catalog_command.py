"""Operator command contract for explicit, writable athlete catalog refreshes."""

from __future__ import annotations

import pytest

from scripts import refresh_athlete_catalog
from app.services.athlete_catalog_service import AthleteCatalogBatchResult, AthleteCatalogSeasonResult


def test_command_requires_at_least_one_explicit_season(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as error:
        refresh_athlete_catalog.main(["--database-url", "sqlite:////tmp/catalog.db"])
    assert error.value.code == 2


def test_command_rejects_demo_database_before_running_refresh(monkeypatch, capsys):
    monkeypatch.setattr(
        refresh_athlete_catalog,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("demo fixture must never be opened for writes"),
    )

    with pytest.raises(SystemExit) as error:
        refresh_athlete_catalog.main(
            ["--database-url", "sqlite:///nba_play_types.db", "--season", "2024-25"]
        )

    assert error.value.code == 2
    assert "read-only demo database" in capsys.readouterr().err


def test_command_passes_every_explicit_season_to_runner(monkeypatch):
    observed: dict[str, object] = {}

    def fake_run(database_url, seasons):
        observed["database_url"] = database_url
        observed["seasons"] = seasons
        return AthleteCatalogBatchResult(tuple(
            AthleteCatalogSeasonResult(season, "succeeded", 1) for season in seasons
        ))

    monkeypatch.setattr(refresh_athlete_catalog, "_run", fake_run)

    assert refresh_athlete_catalog.main(
        [
            "--database-url",
            "sqlite:////tmp/catalog.db",
            "--season",
            "2023-24",
            "--season",
            "2024-25",
        ]
    ) == 0
    assert observed == {
        "database_url": "sqlite:////tmp/catalog.db",
        "seasons": ("2023-24", "2024-25"),
    }


def test_command_reports_mixed_outcomes_and_returns_nonzero(monkeypatch, capsys):
    def fake_run(_database_url, seasons):
        return AthleteCatalogBatchResult((
            AthleteCatalogSeasonResult(seasons[0], "succeeded", 2),
            AthleteCatalogSeasonResult(
                seasons[1], "failed", 0, failure_summary="The athlete catalog refresh could not complete."
            ),
        ))

    monkeypatch.setattr(refresh_athlete_catalog, "_run", fake_run)
    assert refresh_athlete_catalog.main([
        "--database-url", "sqlite:////tmp/catalog.db",
        "--season", "2023-24", "--season", "2024-25",
    ]) == 1
    output = capsys.readouterr().out
    assert "2023-24: succeeded" in output
    assert "2024-25: failed" in output
