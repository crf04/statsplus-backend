"""Offline contract for the misdated ledger game withdrawal command."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine

import scripts.ledger_withdraw_misdated_games as withdraw_script
from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository, canonical_game_from_pbp
from tests.services.test_ledger_backfill import _event, _Participants, _payload, _seed_participant_event


def _database(tmp_path):
    path = tmp_path / "withdraw.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    _seed_participant_event(engine, datetime(2024, 11, 16, tzinfo=UTC))
    payload = _payload()
    payload["date"] = "2024-11-15"
    CanonicalGameLedgerRepository(engine).replace_game(
        canonical_game_from_pbp(
            payload,
            event=_event(),
            season="2024-25",
            source_observation_id="seed",
            retrieved_at=datetime(2024, 11, 15, 6, tzinfo=UTC),
            participant_ids_by_team=_Participants().get_participants("seed", game_id="0022400001"),
        )
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
            {"game_id": "0022400001", "stored_game_date": "2024-11-15", "eastern_game_date": "2024-11-14"}
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
    assert [game["game_id"] for game in first["games"]] == ["0022400001"]
    assert second["games"] == []
    assert repository.get_game("0022400001") is None
