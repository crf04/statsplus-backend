"""Offline contract for the ``offensive_possessions`` backfill command (#433)."""

from __future__ import annotations

import json

from sqlalchemy import create_engine, text

import scripts.ledger_possessions_backfill as backfill_script
from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_offensive_possessions_ledger import (
    FILLED,
    LAL,
    SAS,
    _game,
    _null_possessions,
    _stored,
)


def _database(tmp_path):
    path = tmp_path / "possessions-backfill.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    CanonicalGameLedgerRepository(engine).replace_game(_game())
    _null_possessions(engine)
    return engine, f"sqlite:///{path}"


def test_the_command_fills_possessions_and_a_rerun_reports_zero_updates(tmp_path, capsys):
    engine, url = _database(tmp_path)

    first_code = backfill_script.main(["2024-25", "--database-url", url])
    first = json.loads(capsys.readouterr().out)
    second_code = backfill_script.main(["2024-25", "--database-url", url])
    second = json.loads(capsys.readouterr().out)

    assert (first_code, second_code) == (0, 0)
    assert (first["updated"], first["rejected"], first["checksums_changed"]) == (1, [], 0)
    assert (second["updated"], second["unchanged"], second["checksums_changed"]) == (0, 1, 0)
    assert _stored(engine) == FILLED


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    engine, url = _database(tmp_path)

    code = backfill_script.main(["2024-25", "--database-url", url, "--dry-run"])
    report = json.loads(capsys.readouterr().out)

    assert (code, report["updated"], report["dry_run"]) == (0, 1, True)
    assert _stored(engine)[0] == {LAL: None, SAS: None}


def test_the_command_refuses_a_database_without_migration_085(tmp_path, capsys):
    engine, url = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE canonical_game_ledger_team_facts DROP COLUMN offensive_possessions")
        )

    code = backfill_script.main(["2024-25", "--database-url", url])
    error = json.loads(capsys.readouterr().err)

    assert code == 2
    assert error == {
        "error": "migration_085_required",
        "missing_columns": ["canonical_game_ledger_team_facts.offensive_possessions"],
    }
