"""Whole-game rotation writes: idempotent, replacing, and the 066 migration."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations
from app.services.rotation_derivation import derive_rotation
from tests.support.rotation_world import COLLECTED_AT, SEASON, game, line, migrated_repository

FIXTURES = Path("tests/fixtures/nba_live_data")


def _recorded(name):
    return derive_rotation(
        json.loads((FIXTURES / f"boxscore_{name}.json").read_text()),
        json.loads((FIXTURES / f"playbyplay_{name}.json").read_text()),
    )


def _dump(repository):
    with repository.engine.connect() as connection:
        return {
            table: sorted(
                tuple(row) for row in connection.execute(text(f"SELECT * FROM {table}"))
            )
            for table in ("rotation_games", "rotation_player_games", "rotation_stints")
        }


def test_storing_the_same_game_twice_leaves_identical_rows(tmp_path):
    repository = migrated_repository(tmp_path)
    rotation = _recorded("overtime")

    repository.replace_game(SEASON, rotation, COLLECTED_AT)
    first = _dump(repository)
    repository.replace_game(SEASON, rotation, COLLECTED_AT)

    assert _dump(repository) == first
    assert len(first["rotation_games"]) == 1
    assert len(first["rotation_player_games"]) == 35
    assert len(first["rotation_stints"]) == 61


def test_replacing_a_game_drops_every_row_of_the_earlier_version(tmp_path):
    repository = migrated_repository(tmp_path)
    repository.replace_game(SEASON, game("0022500001", "2026-01-01", "DET", "BOS", [
        line(1, "Unit One", "DET", minutes=30, started=True,
             stints=((1, "12:00", 1, "6:00", 6), (2, "12:00", 2, "0:00", 12))),
        line(2, "Gone Guy", "DET", minutes=10),
    ]), COLLECTED_AT)

    repository.replace_game(SEASON, game("0022500001", "2026-01-01", "DET", "BOS", [
        line(1, "Unit One", "DET", minutes=31, started=True,
             stints=((1, "12:00", 1, "0:00", 12),)),
    ]), COLLECTED_AT)

    dump = _dump(repository)
    assert [row[1] for row in dump["rotation_player_games"]] == [1]
    assert [row[2] for row in dump["rotation_stints"]] == [0]
    assert repository.stored_game_ids(SEASON) == {"0022500001"}
    assert repository.seasons() == [SEASON]


def test_a_game_that_fails_midway_stores_nothing(tmp_path):
    repository = migrated_repository(tmp_path)
    broken = game("0022500002", "2026-01-02", "DET", "BOS", [
        line(1, "Unit One", "DET", minutes=30, started=True),
        line(1, "Unit One", "DET", minutes=30, started=True),  # duplicate player key
    ])

    with pytest.raises(IntegrityError):
        repository.replace_game(SEASON, broken, COLLECTED_AT)

    assert _dump(repository) == {
        "rotation_games": [], "rotation_player_games": [], "rotation_stints": [],
    }


def test_migration_066_creates_the_tables_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE rotation_stints"))
        connection.execute(text("DROP TABLE rotation_player_games"))
        connection.execute(text("DROP TABLE rotation_games"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 66"))

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.applied == ("066_rotation_tables",)
    assert second.applied == ()
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("rotation_player_games")} == {
        "game_id", "player_id", "team_tricode", "player_name", "started",
        "minutes_cs", "closed", "q1_cs", "q2_cs", "q3_cs", "q4_cs", "ot_cs",
    }
    assert inspector.get_pk_constraint("rotation_stints")["constrained_columns"] == [
        "game_id", "player_id", "seq",
    ]
