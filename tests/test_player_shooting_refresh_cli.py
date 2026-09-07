import json

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from scripts import player_shooting_refresh


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
