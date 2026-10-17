"""Postgres checks for the rotation store and reads (crf04/statsplus#223):
migration 066, whole-game replace, and the route's SQL."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from app.services.rotation_derivation import derive_rotation
from app.services.rotation_repository import RotationRepository
from tests.support.rotation_world import (
    COLLECTED_AT,
    SEASON,
    Catalog,
    game,
    line,
    service_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

FIXTURES = Path("tests/fixtures/nba_live_data")


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def _recorded():
    return derive_rotation(
        json.loads((FIXTURES / "boxscore_overtime.json").read_text()),
        json.loads((FIXTURES / "playbyplay_overtime.json").read_text()),
    )


def test_migration_066_is_idempotent_and_cascades_on_postgres(engine):
    assert set(inspect(engine).get_table_names()) >= {
        "rotation_games", "rotation_player_games", "rotation_stints",
    }
    assert run_migrations(engine).applied == ()
    repository = RotationRepository(engine)
    repository.replace_game(SEASON, _recorded(), COLLECTED_AT)

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM rotation_games WHERE game_id = '0022500123'"))

    with engine.connect() as connection:
        for table in ("rotation_player_games", "rotation_stints"):
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_replacing_a_game_twice_leaves_identical_rows_on_postgres(engine):
    repository = RotationRepository(engine)
    dump = text(
        "SELECT p.player_id, p.minutes_cs, p.started, p.closed, s.seq, s.minutes_cs "
        "FROM rotation_player_games p LEFT JOIN rotation_stints s "
        "ON s.game_id = p.game_id AND s.player_id = p.player_id "
        "ORDER BY p.player_id, s.seq"
    )

    repository.replace_game(SEASON, _recorded(), COLLECTED_AT)
    with engine.connect() as connection:
        first = [tuple(row) for row in connection.execute(dump)]
    repository.replace_game(SEASON, _recorded(), COLLECTED_AT)
    with engine.connect() as connection:
        second = [tuple(row) for row in connection.execute(dump)]

    assert second == first
    assert len(first) == 74


def test_the_player_and_team_reads_run_on_postgres(engine):
    repository = RotationRepository(engine)
    repository.replace_game(SEASON, game("0022500001", "2026-01-01", "DET", "BOS", [
        line(1, "Unit One", "DET", minutes=30, started=True, closed=True,
             periods=(10, 10, 5, 5, 0), stints=((1, "12:00", 4, "0:00", 30),)),
        line(2, "Unit Two", "DET", minutes=0),
    ]), COLLECTED_AT)
    repository.replace_game(SEASON, game("0022500002", "2026-01-03", "BOS", "DET", [
        line(1, "Unit One", "DET", minutes=20, started=True,
             periods=(5, 5, 5, 5, 0), stints=((1, "12:00", 2, "0:00", 20),)),
    ]), COLLECTED_AT)
    service = service_for(
        repository, Catalog((1, "Unit One", "DET"), (2, "Unit Two", "DET"))
    )

    player = service.get_player_rotation(
        player_name="unit one", player_team=None, last_n=1, season=None
    )
    team = service.get_team_rotation(team="DET", last_n=1, season=None)

    assert player["recent"]["minutes"] == 20.0
    assert player["season_summary"]["minutes"] == 25.0
    assert player["games"][0]["matchup"] == "DET @ BOS"
    assert player["games"][0]["stints"] == [
        {"in": {"period": 1, "clock": "12:00"}, "out": {"period": 2, "clock": "0:00"},
         "minutes": 20.0},
    ]
    # Unit Two is not on the box score of DET's most recent game, so drops out.
    assert [(p["name"], p["minutes_change"]) for p in team["players"]] == [
        ("Unit One", -5.0),
    ]


def test_attempts_count_rejections_but_not_failures_and_clear_on_store(engine):
    repository = RotationRepository(engine)
    repository.record_attempt(SEASON, "0022500123", "rejected", "bad", COLLECTED_AT)
    repository.record_attempt(SEASON, "0022500123", "failed", "timeout", COLLECTED_AT)
    repository.record_attempt(SEASON, "0022500123", "rejected", "worse", COLLECTED_AT)

    attempt = repository.attempts(SEASON)["0022500123"]
    assert (attempt["rejections"], attempt["last_outcome"], attempt["last_reason"]) == (
        2, "rejected", "worse",
    )

    repository.replace_game(SEASON, _recorded(), COLLECTED_AT)
    assert repository.attempts(SEASON) == {}
