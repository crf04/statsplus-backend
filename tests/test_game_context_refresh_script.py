"""The game-context refresh entry point migrates, refreshes one season and prints its summary."""

import json
import subprocess
import sys

from sqlalchemy import create_engine

from app.services.game_context import GameContextRepository
from scripts import game_context_refresh
from tests.support.game_context_world import final, migrated_engine, schedule

NOP = (1610612740, "NOP")
LAC = (1610612746, "LAC")


def test_a_run_stores_the_season_and_a_rerun_changes_nothing(tmp_path, capsys):
    engine = migrated_engine(tmp_path)
    schedule(engine, "0022600001", "2026-10-21T23:30:00+00:00", LAC, NOP, season="2026-27")
    schedule(engine, "0022600002", "2026-10-23T23:30:00+00:00", NOP, LAC, season="2026-27")
    final(engine, "0022600001", "2026-10-21", (*LAC, 100, 98.0), (*NOP, 110, 100.0), season="2026-27")
    url = str(engine.url)
    engine.dispose()

    first = game_context_refresh.main(["2026-27", "--database-url", url])
    first_summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    second = game_context_refresh.main(["2026-27", "--database-url", url])
    second_summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert (first, first_summary) == (
        0,
        {
            "season": "2026-27",
            "games": 2,
            "ledger_games": 1,
            "inserted": 2,
            "updated": 0,
            "removed": 0,
            "unchanged": 0,
        },
    )
    assert (second, second_summary) == (
        0,
        {
            "season": "2026-27",
            "games": 2,
            "ledger_games": 1,
            "inserted": 0,
            "updated": 0,
            "removed": 0,
            "unchanged": 2,
        },
    )
    stored = GameContextRepository(create_engine(url)).read("0022600002")
    assert [(team["tricode"], team["rest"]["rest_days"]) for team in stored["teams"]] == [
        ("NOP", 1),
        ("LAC", 1),
    ]


def test_help_describes_the_nightly_step():
    output = subprocess.run(
        [sys.executable, "scripts/game_context_refresh.py", "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "Store each game's team context for the Matchup" in output
