"""The odds cron stores NBA.com's daily lineups after its game-line step, each guarded (crf04/statsplus#492)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select

from app.config.settings import OddsApiSettings
from app.migrations import run_migrations
from app.models.team_lineup import TeamLineup
from app.providers import nba_daily_lineups
from app.services import game_lines, team_lineups
from scripts import collect_odds

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / "tests" / "fixtures" / "nba_daily_lineups" / "00_daily_lineups_20261110.json"
DISABLED = OddsApiSettings()
WITHOUT_KEY = OddsApiSettings(enabled=True)


def run_script(monkeypatch, engine, odds_api=DISABLED):
    settings = SimpleNamespace(odds_api=odds_api, log_level="INFO")
    monkeypatch.setattr(collect_odds, "load_settings", lambda **_kw: settings)
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: SimpleNamespace(engine=engine))
    return collect_odds.main(["--database-url", "postgresql://odds.example/statsplus"])


def printed(capsys):
    # The JSON log lines go to stdout too.
    return [line for line in capsys.readouterr().out.splitlines() if not line.startswith('{"level"')]


def lineups_line(line):
    assert line.startswith("lineups: ")
    reported = json.loads(line.removeprefix("lineups: "))
    reported.pop("date")
    return reported


@pytest.fixture
def no_game_lines(monkeypatch):
    monkeypatch.setattr(game_lines, "collect", lambda _engine: {"stubbed": True})


def test_the_beat_stores_todays_lineups_even_with_odds_disabled(monkeypatch, capsys, tmp_path, no_game_lines):
    engine = create_engine(f"sqlite:///{tmp_path / 'lineups.sqlite3'}")
    run_migrations(engine)
    monkeypatch.setattr(
        nba_daily_lineups, "fetch_daily_lineups", lambda _day: json.loads(RECORDED.read_text())
    )

    assert run_script(monkeypatch, engine) == 0

    out = printed(capsys)
    assert out[:2] == ["no_work: ODDS_API_ENABLED is false", 'game_lines: {"stubbed": true}']
    assert lineups_line(out[2]) == {"state": "stored", "teams": 12}
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(TeamLineup.__table__)).scalar() == 12


def test_a_lineups_fetch_failure_is_reported_and_leaves_the_exit_status_alone(
    monkeypatch, capsys, no_game_lines
):
    def unreachable(_day):
        raise ConnectionError("stats.nba.com unreachable")

    monkeypatch.setattr(nba_daily_lineups, "fetch_daily_lineups", unreachable)

    assert run_script(monkeypatch, None) == 0

    out = printed(capsys)
    assert lineups_line(out[2]) == {"state": "failed", "error": "ConnectionError"}


@pytest.mark.parametrize("odds_api, code", [(DISABLED, 0), (WITHOUT_KEY, 1)])
@pytest.mark.parametrize("failing", ["game_lines", "lineups"])
def test_one_side_step_raising_still_runs_the_other_in_order(monkeypatch, capsys, failing, odds_api, code):
    calls = []

    def step(name, result):
        def run(_engine):
            calls.append(name)
            if name == failing:
                raise RuntimeError(f"{name} broke")
            return result

        return run

    monkeypatch.setattr(game_lines, "collect", step("game_lines", {"inserted": 0}))
    monkeypatch.setattr(
        team_lineups, "collect_team_lineups", step("lineups", {"state": "stored", "teams": 0})
    )

    assert run_script(monkeypatch, None, odds_api) == code

    assert calls == ["game_lines", "lineups"]
    out = printed(capsys)
    assert out[1:] == [
        "game_lines failed: RuntimeError: game_lines broke"
        if failing == "game_lines"
        else 'game_lines: {"inserted": 0}',
        "lineups failed: RuntimeError: lineups broke"
        if failing == "lineups"
        else 'lineups: {"state": "stored", "teams": 0}',
    ]
