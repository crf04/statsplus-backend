import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from scripts import player_shooting_refresh


@pytest.fixture(autouse=True)
def _no_backtest_precompute(monkeypatch):
    # The post-tick step has its own tests (test_target_backtest_precompute).
    monkeypatch.setattr(player_shooting_refresh, "_precompute_target_backtests", lambda: None)


@pytest.mark.parametrize("url", [None, "sqlite:///nba_play_types.db"])
def test_cli_refuses_missing_or_demo_database(monkeypatch, url):
    if url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(SystemExit) as error:
        player_shooting_refresh.main(["2025-26"])
    assert error.value.code == 2


def test_cli_idle_real_database_never_constructs_provider_dependencies(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'cli.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)

    def forbidden(*args):
        raise AssertionError("idle CLI constructed provider dependencies")

    monkeypatch.setattr(player_shooting_refresh, "_composer", forbidden)
    assert player_shooting_refresh.main(["2025-26"]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "season_inactive", "composed_jobs": 0}


def _migrated_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'cli.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _insert_manifest(url, manifest_id, season, status, created_at):
    from app.models.collection_control import CollectionManifest

    stamp = datetime.fromisoformat(created_at)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id=manifest_id,
                season=season,
                cutoff=stamp,
                collect_before=stamp,
                accepted_versions="[]",
                scopes="[]",
                checksum=manifest_id * 2,
                status=status,
                created_at=stamp,
            )
        )
    engine.dispose()


def _activate_season(url, season, status="active"):
    from app.models.collection_control import ActiveSeason

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            ActiveSeason.__table__.insert().values(
                season=season,
                status=status,
                phase="Regular Season",
                activated_at=datetime(2026, 10, 1),
                activated_by="test",
            )
        )
    engine.dispose()


def test_cli_without_season_and_without_active_season_exits_cleanly(tmp_path, monkeypatch, capsys):
    url = _migrated_url(tmp_path, monkeypatch)
    _insert_manifest(url, "m1", "2025-26", "active", "2026-01-01T00:00:00")
    _activate_season(url, "2025-26", status="inactive")
    assert player_shooting_refresh.main([]) == 0
    assert capsys.readouterr().out == '{"composed_jobs": 0, "state": "no_active_season"}\n'


def test_cli_without_season_ticks_the_active_season_not_the_newest_manifest(tmp_path, monkeypatch, capsys):
    """At the 2026-27 rollover 2025-26 held the newest active manifest but was
    an inactive season; the tick must follow ``active_seasons``."""
    url = _migrated_url(tmp_path, monkeypatch)
    _insert_manifest(url, "m1", "2025-26", "active", "2026-04-01T00:00:00")
    _activate_season(url, "2025-26", status="inactive")
    _activate_season(url, "2026-27")
    monkeypatch.setattr(player_shooting_refresh, "_composer", lambda *a: pytest.fail("idle"))
    # "disabled" is only returned for an active season, so 2026-27 was ticked;
    # 2025-26 would have returned "season_inactive".
    assert player_shooting_refresh.main([]) == 0
    assert capsys.readouterr().out == '{"composed_jobs": 0, "state": "disabled"}\n'


def test_cli_explicit_season_overrides_the_active_season(tmp_path, monkeypatch, capsys):
    url = _migrated_url(tmp_path, monkeypatch)
    _activate_season(url, "2025-26", status="inactive")
    _activate_season(url, "2026-27")
    assert player_shooting_refresh.main(["2025-26"]) == 0
    assert capsys.readouterr().out == '{"composed_jobs": 0, "state": "season_inactive"}\n'


def test_cli_without_season_refuses_multiple_active_seasons(tmp_path, monkeypatch, capsys):
    url = _migrated_url(tmp_path, monkeypatch)
    _activate_season(url, "2025-26")
    _activate_season(url, "2026-27")
    assert player_shooting_refresh.main([]) == 1
    assert (
        capsys.readouterr().out
        == '{"composed_jobs": 0, "seasons": ["2025-26", "2026-27"], "state": "ambiguous_active_season"}\n'
    )
