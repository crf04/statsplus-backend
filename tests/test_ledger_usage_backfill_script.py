"""Offline contract for the ``usage_pct`` ledger backfill command (#450)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text

import scripts.ledger_usage_backfill as backfill_script
from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
)
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_correction_propagation import _boundary_observation_values
from tests.services.test_usage_ledger import _game, _null_usage, _stored


def _database(tmp_path, *, null_usage=True):
    path = tmp_path / "usage-backfill.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    CanonicalGameLedgerRepository(engine).replace_game(_game())
    if null_usage:
        _null_usage(engine)
    return engine, f"sqlite:///{path}"


def test_the_command_fills_usage_and_a_rerun_reports_zero_updates(tmp_path, capsys):
    engine, url = _database(tmp_path)

    first_code = backfill_script.main(["2024-25", "--database-url", url])
    first = json.loads(capsys.readouterr().out)
    after_first = _stored(engine)
    second_code = backfill_script.main(["2024-25", "--database-url", url])
    second = json.loads(capsys.readouterr().out)

    assert (first_code, second_code) == (0, 0)
    assert (first["examined"], first["updated"], first["rejected"]) == (1, 1, [])
    assert (second["examined"], second["updated"], second["unchanged"]) == (1, 0, 1)
    assert after_first == {101: 28.5714, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}
    assert _stored(engine) == after_first


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    engine, url = _database(tmp_path)

    code = backfill_script.main(["2024-25", "--database-url", url, "--dry-run"])
    report = json.loads(capsys.readouterr().out)

    assert (code, report["updated"], report["dry_run"]) == (0, 1, True)
    assert set(_stored(engine).values()) == {None}


def test_the_command_refuses_a_database_without_migration_071(tmp_path, capsys):
    engine, url = _database(tmp_path, null_usage=False)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE canonical_game_ledger_player_facts DROP COLUMN usage_pct"))

    code = backfill_script.main(["2024-25", "--database-url", url])
    error = json.loads(capsys.readouterr().err)

    assert code == 2
    assert error["error"] == "migration_071_required"
    assert error["missing_columns"] == ["canonical_game_ledger_player_facts.usage_pct"]


def _manifest(engine):
    """A manifest past its collection deadline that governs the stored game."""

    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    game = _game()
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="m",
                season="2024-25",
                cutoff=cutoff,
                collect_before=cutoff + timedelta(days=1),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="m",
                status="superseded",
                created_at=cutoff,
            )
        )
        connection.execute(
            CollectionObservation.__table__.insert().values(
                **_boundary_observation_values(game, cutoff=cutoff, manifest_id="m")
            )
        )


def _jobs(engine):
    with engine.connect() as connection:
        return [
            (row.stream_key, row.status, row.manifest_id)
            for row in connection.execute(select(CompositionJob.__table__))
        ]


def test_requeue_flag_queues_only_player_game_logs_under_the_governing_manifest(tmp_path, capsys):
    engine, url = _database(tmp_path)
    _manifest(engine)

    code = backfill_script.main(
        [
            "2024-25",
            "--database-url",
            url,
            "--requeue-player-game-logs",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert (report["updated"], report["requeued_games"]) == (1, 1)
    assert _jobs(engine) == [("player_game_logs", "queued", "m")]


def test_requeue_flag_refuses_without_a_governing_manifest(tmp_path, capsys):
    engine, url = _database(tmp_path)

    code = backfill_script.main(
        [
            "2024-25",
            "--database-url",
            url,
            "--requeue-player-game-logs",
        ]
    )
    error = json.loads(capsys.readouterr().err)

    assert code == 2
    assert error["error"] == "governing_manifest_required"
    assert _jobs(engine) == []


def test_requeue_flag_cannot_be_combined_with_a_dry_run(tmp_path):
    _engine, url = _database(tmp_path)

    with pytest.raises(SystemExit) as stopped:
        backfill_script.main(
            [
                "2024-25",
                "--database-url",
                url,
                "--dry-run",
                "--requeue-player-game-logs",
            ]
        )

    assert stopped.value.code == 2


def test_requeue_flag_does_not_queue_when_a_game_was_rejected(tmp_path, capsys):
    engine, url = _database(tmp_path)
    _manifest(engine)
    with engine.begin() as connection:
        # The archive no longer matches its checksum, so the game is rejected.
        connection.execute(
            text("UPDATE canonical_game_ledger_raw_rows SET payload = replace(payload, '28.5714', '150')")
        )

    code = backfill_script.main(
        [
            "2024-25",
            "--database-url",
            url,
            "--requeue-player-game-logs",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert len(report["rejected"]) == 1 and "requeued_games" not in report
    assert _jobs(engine) == []
