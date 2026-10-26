"""Offline contract for the misdated ledger game withdrawal command."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine

import scripts.ledger_withdraw_misdated_games as withdraw_script
from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_ledger_backfill import activate_season, store_dated_game


def _database(tmp_path, *, active=True):
    path = tmp_path / "withdraw.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    if active:
        activate_season(engine, "2024-25")
    store_dated_game(
        engine,
        "0022400001",
        season="2024-25",
        scheduled_at=datetime(2024, 11, 15, tzinfo=UTC),
        stored_date="2024-11-15",
        provider="nba_live_data",
    )
    return CanonicalGameLedgerRepository(engine), f"sqlite:///{path}"


def test_a_dry_run_lists_the_misdated_game_and_writes_nothing(tmp_path, capsys):
    repository, url = _database(tmp_path)

    code = withdraw_script.main(["2024-25", "--database-url", url, "--dry-run"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "season": "2024-25",
        "dry_run": True,
        "games": [
            {
                "game_id": "0022400001",
                "stored_game_date": "2024-11-15",
                "eastern_game_date": "2024-11-14",
                "verdict": "withdraw",
                "publication_ids": [],
            }
        ],
    }
    assert repository.get_game("0022400001") is not None


def test_the_command_withdraws_the_game_and_a_rerun_finds_nothing(tmp_path, capsys):
    repository, url = _database(tmp_path)

    first_code = withdraw_script.main(["2024-25", "--database-url", url])
    first = json.loads(capsys.readouterr().out)
    second_code = withdraw_script.main(["2024-25", "--database-url", url])
    second = json.loads(capsys.readouterr().out)

    assert (first_code, second_code) == (0, 0)
    assert [(game["game_id"], game["verdict"]) for game in first["games"]] == [("0022400001", "withdraw")]
    assert second["games"] == []
    assert repository.get_game("0022400001") is None


def test_the_command_refuses_to_write_a_season_that_is_not_active(tmp_path, capsys):
    repository, url = _database(tmp_path, active=False)

    code = withdraw_script.main(["2024-25", "--database-url", url])

    assert code == 2
    assert (
        capsys.readouterr().err == "refused: 2024-25 is not the active season; only a dry run may read it\n"
    )
    assert repository.get_game("0022400001") is not None
