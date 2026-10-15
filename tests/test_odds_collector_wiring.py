"""The odds beat rides the projection collector's loop without coupling (#377)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.settings import OddsApiSettings
from app.services import odds_collection
from scripts import collect_odds, collect_projections, projection_collection_service


class Stop(Exception):
    pass


def _loop(monkeypatch, *, odds_settings, odds_run, build=None):
    events = []
    dfs = SimpleNamespace(run=lambda: events.append("dfs") or SimpleNamespace(status="complete", reason="ok"))
    settings = SimpleNamespace(nba=SimpleNamespace(current_season="2026-27"), odds_api=odds_settings)
    monkeypatch.setenv("DATABASE_URL", "postgresql://collector.example/statsplus")
    monkeypatch.setattr(collect_projections, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(
        collect_projections, "build_dependencies",
        lambda _settings: SimpleNamespace(projection_collection_coordinator=dfs),
    )
    odds = SimpleNamespace(run=lambda: events.append("odds") or odds_run(), settings=odds_settings)
    monkeypatch.setattr(
        odds_collection, "build_odds_collection", build or (lambda deps, cfg: odds if cfg.odds_api.enabled else None)
    )

    def sleep(_seconds):
        if events.count("dfs") == 2:
            raise Stop

    monkeypatch.setattr(projection_collection_service.time, "sleep", sleep)
    with pytest.raises(Stop):
        projection_collection_service.run()
    return events


def test_each_beat_runs_dfs_then_odds_when_enabled(monkeypatch):
    events = _loop(
        monkeypatch,
        odds_settings=OddsApiSettings(enabled=True, api_key="k"),
        odds_run=lambda: SimpleNamespace(status="no_work", reason="nothing_due"),
    )

    assert events == ["dfs", "odds", "dfs", "odds"]


def test_a_disabled_odds_collector_leaves_the_dfs_loop_untouched(monkeypatch):
    events = _loop(monkeypatch, odds_settings=OddsApiSettings(), odds_run=lambda: None)

    assert events == ["dfs", "dfs"]


def test_an_odds_crash_never_stops_dfs_collection(monkeypatch):
    def explode():
        raise RuntimeError("boom")

    events = _loop(monkeypatch, odds_settings=OddsApiSettings(enabled=True, api_key="k"), odds_run=explode)

    assert events == ["dfs", "odds", "dfs", "odds"]


def test_enabled_without_a_key_disables_only_odds_and_says_so(monkeypatch, capsys):
    events = _loop(
        monkeypatch,
        odds_settings=OddsApiSettings(enabled=True),
        odds_run=lambda: None,
        build=odds_collection.build_odds_collection,
    )

    assert events == ["dfs", "dfs"]
    assert "odds collection disabled: ODDS_API_ENABLED is true but ODDS_API_KEY is not set" in capsys.readouterr().out


def test_standalone_script_exits_nonzero_without_a_key(monkeypatch, capsys):
    settings = SimpleNamespace(odds_api=OddsApiSettings(enabled=True))
    monkeypatch.setattr(collect_odds, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _settings: object())

    code = collect_odds.main(["--database-url", "postgresql://collector.example/statsplus"])

    assert code == 1
    assert capsys.readouterr().out == "failed: ODDS_API_ENABLED is true but ODDS_API_KEY is not set\n"
