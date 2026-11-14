"""Migration 084: the on/off lineup segment tables (crf04/statsplus#410)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import downgrade_lineup_segments, run_migrations

pytestmark = pytest.mark.real_migrations

TABLES = ("lineup_roster_slots", "lineup_segment_games", "lineup_segment_players", "lineup_segment_seasons")
_SLOT = (
    "INSERT INTO lineup_roster_slots (season, team_id, player_id, slot, player_name) "
    "VALUES ('2025-26', 1610612738, {player}, {slot}, 'P')"
)


def _forget_084(engine):
    with engine.begin() as connection:
        for table in TABLES:
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 84"))


def test_the_migration_creates_the_tables_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    _forget_084(engine)

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.applied == ("084_lineup_segments",)
    assert second.applied == ()
    inspector = inspect(engine)
    assert [c["name"] for c in inspector.get_columns("lineup_segment_players")] == [
        "game_id",
        "team_id",
        "seg",
        "player_id",
        "season",
        "written_at",
        "lineup_mask",
        "duration_cs",
        "pts",
        "reb",
        "ast",
        "fg3m",
        "fga",
        "fg3a",
        "tov",
        "fta",
        "team_fga",
        "team_fta",
        "team_tov",
        "team_heaves",
        "off_poss",
        "fgm",
        "ftm",
    ]
    assert inspector.get_pk_constraint("lineup_segment_players")["constrained_columns"] == [
        "game_id",
        "team_id",
        "seg",
        "player_id",
    ]
    assert {
        index["name"]: index["column_names"] for index in inspector.get_indexes("lineup_segment_players")
    } == {"ix_lineup_segment_players_team_season": ["team_id", "season"]}
    assert inspector.get_pk_constraint("lineup_roster_slots")["constrained_columns"] == [
        "season",
        "team_id",
        "player_id",
    ]


def test_a_rerun_keeps_the_rows_already_stored(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_SLOT.format(player=1, slot=0)))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 84"))

    assert run_migrations(engine).applied == ("084_lineup_segments",)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM lineup_roster_slots")).scalar() == 1


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (((1, 0), (2, 0)), "two players share a slot"),
        (((1, 64),), "a slot past the 64-bit mask"),
        (((1, -1),), "a negative slot"),
    ],
)
def test_a_team_season_cannot_hold_an_unusable_slot(tmp_path, rows, reason):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        for player, slot in rows:
            connection.execute(text(_SLOT.format(player=player, slot=slot)))


def test_the_downgrade_drops_only_the_tables_and_their_record(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with engine.begin() as connection:
        downgrade_lineup_segments(connection)

    names = inspect(engine).get_table_names()
    assert not set(TABLES) & set(names)
    assert "rotation_stints" in names
    with engine.connect() as connection:
        versions = connection.execute(text("SELECT version FROM schema_migrations")).scalars().all()
    assert 84 not in versions and 83 in versions


def test_089_adds_null_makes_to_stored_rows_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE lineup_segment_players DROP COLUMN fgm"))
        connection.execute(text("ALTER TABLE lineup_segment_players DROP COLUMN ftm"))
        connection.execute(
            text(
                "INSERT INTO lineup_segment_players VALUES "
                "('0022500001', 1, 0, 7, '2025-26', '2026-11-03 12:00:00', 1, 6000,"
                " 5, 1, 2, 1, 3, 1, 0, 0, 10, 2, 1, 0, 9)"
            )
        )
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 89"))

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert (first.applied, second.applied) == (("089_lineup_segment_makes",), ())
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT pts, fg3m, off_poss, fgm, ftm FROM lineup_segment_players")
        ).all() == [(5, 1, 9, None, None)]
