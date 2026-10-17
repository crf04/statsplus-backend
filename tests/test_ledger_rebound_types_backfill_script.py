"""Offline contract for the Rebound Types ledger backfill command (#446)."""

from __future__ import annotations

import json

from sqlalchemy import create_engine, select, text, update

import scripts.ledger_rebound_types_backfill as backfill_script
from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
)
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_rebound_types_ledger import SIX, _game


def _database(tmp_path, *, null_the_counts=True):
    path = tmp_path / "backfill.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    CanonicalGameLedgerRepository(engine).replace_game(_game())
    if null_the_counts:
        nulls = {name: None for name in SIX}
        with engine.begin() as connection:
            connection.execute(update(CanonicalGameLedgerPlayerFact.__table__).values(nulls))
            connection.execute(update(CanonicalGameLedgerTeamFact.__table__).values(nulls))
    return engine, f"sqlite:///{path}"


def _players(engine):
    table = CanonicalGameLedgerPlayerFact.__table__
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                select(table.c.player_id, *[getattr(table.c, n) for n in SIX])
                .order_by(table.c.player_id)
            )
        ]


def test_the_command_fills_the_counts_and_a_rerun_reports_zero_updates(tmp_path, capsys):
    engine, url = _database(tmp_path)

    first_code = backfill_script.main(["2024-25", "--database-url", url])
    first = json.loads(capsys.readouterr().out)
    after_first = _players(engine)
    second_code = backfill_script.main(["2024-25", "--database-url", url])
    second = json.loads(capsys.readouterr().out)

    assert (first_code, second_code) == (0, 0)
    assert (first["examined"], first["updated"], first["rejected"]) == (1, 1, [])
    assert (second["examined"], second["updated"], second["unchanged"]) == (1, 0, 1)
    assert after_first == [
        (101, 2, 0, 0, 0, 1, 0),
        (102, 0, 1, 2, 0, 0, 0),
        (103, 0, 0, 0, 0, 0, 0),
        (201, 1, 0, 0, 1, 0, 1),
        (202, 0, 0, 0, 0, 0, 0),
    ]
    assert _players(engine) == after_first


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    engine, url = _database(tmp_path)

    code = backfill_script.main(["2024-25", "--database-url", url, "--dry-run"])
    report = json.loads(capsys.readouterr().out)

    assert (code, report["updated"], report["dry_run"]) == (0, 1, True)
    assert _players(engine)[0] == (101, None, None, None, None, None, None)


def test_the_command_can_be_restricted_to_named_games(tmp_path, capsys):
    engine, url = _database(tmp_path)

    code = backfill_script.main([
        "2024-25", "--database-url", url, "--game-id", "no-such-game",
    ])
    report = json.loads(capsys.readouterr().out)

    assert (code, report["examined"], report["updated"]) == (0, 0, 0)
    assert _players(engine)[0] == (101, None, None, None, None, None, None)


def test_the_command_refuses_a_database_without_migration_069(tmp_path, capsys):
    engine, url = _database(tmp_path, null_the_counts=False)
    with engine.begin() as connection:
        for table in (
            "canonical_game_ledger_player_facts", "canonical_game_ledger_team_facts",
        ):
            for name in SIX:
                connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))

    code = backfill_script.main(["2024-25", "--database-url", url])
    error = json.loads(capsys.readouterr().err)

    assert code == 2
    assert error["error"] == "migration_069_required"
    assert "canonical_game_ledger_player_facts.def_two_pt_rebounds" in error["missing_columns"]
