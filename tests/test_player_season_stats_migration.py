"""Migration 074: the season player stats table (crf04/statsplus-backend#509)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import downgrade_player_season_stats, run_migrations

pytestmark = pytest.mark.real_migrations

_INSERT = (
    "INSERT INTO player_season_stats (season, season_type, player_id, player_name, team_abbreviation, age, "
    "gp, w, l, min, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf, pts, "
    "plus_minus, fetched_at) VALUES ('2023-24', 'Regular Season', {player}, 'P', 'DEN', 28, 79, 55, 24, "
    "2737.0, 1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP)"
)


def test_the_migration_creates_the_table_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE player_season_stats"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 74"))

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first.applied == ("074_player_season_stats",)
    assert second.applied == ()
    inspector = inspect(engine)
    assert [c["name"] for c in inspector.get_columns("player_season_stats")] == [
        "season",
        "season_type",
        "player_id",
        "player_name",
        "team_abbreviation",
        "age",
        "gp",
        "w",
        "l",
        "min",
        "fgm",
        "fga",
        "fg3m",
        "fg3a",
        "ftm",
        "fta",
        "oreb",
        "dreb",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "pf",
        "pts",
        "plus_minus",
        "fetched_at",
    ]
    assert inspector.get_pk_constraint("player_season_stats")["constrained_columns"] == [
        "season",
        "season_type",
        "player_id",
    ]


def test_a_rerun_keeps_the_rows_already_stored(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_INSERT.format(player=1)))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 74"))

    assert run_migrations(engine).applied == ("074_player_season_stats",)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM player_season_stats")).scalar() == 1


def test_one_player_has_one_row_per_season_and_type(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(_INSERT.format(player=1)))
        connection.execute(text(_INSERT.format(player=1)))


def test_the_downgrade_drops_only_the_table_and_its_record(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)

    with engine.begin() as connection:
        downgrade_player_season_stats(connection)

    assert "player_season_stats" not in inspect(engine).get_table_names()
    assert "usage_events" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        versions = connection.execute(text("SELECT version FROM schema_migrations")).scalars().all()
    assert 74 not in versions and 73 in versions
