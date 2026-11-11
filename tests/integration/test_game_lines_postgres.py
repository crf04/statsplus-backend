"""Postgres checks for the stored game lines (crf04/statsplus#493): migration 087,
a rerun that rereads without changing a value, and the Matchup read."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from app.services.game_context import GameContextRepository
from tests.services.test_game_lines import (
    BEFORE_TIP,
    GAMES,
    LATER,
    NYK_GAME,
    collect_while_a_later_run_writes,
    line,
    run,
    scoreboard,
    team,
)
from tests.support.game_context_world import schedule

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


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
    for game_id, (away, home, tip) in GAMES.items():
        schedule(engine, game_id, tip, team(away), team(home), season="2026-27")
    yield engine
    engine.dispose()


def test_migration_087_reruns_and_a_second_collection_changes_no_line_on_postgres(engine):
    assert "game_lines" in set(inspect(engine).get_table_names())
    assert run_migrations(engine).applied == ()
    assert run(engine, scoreboard(), BEFORE_TIP)["inserted"] == 6
    first = line(engine, NYK_GAME)

    summary = run(engine, scoreboard(), LATER)

    assert (summary["inserted"], summary["updated"], summary["unchanged"]) == (0, 0, 6)
    assert line(engine, NYK_GAME) == {**first, "read_at": "2026-11-10T23:55:00+00:00"}
    assert first["teams"][1] == {
        "team_id": 1610612752,
        "tricode": "NYK",
        "is_home": True,
        "spread": -15.5,
        "implied_total": 119.0,
    }
    with engine.connect() as connection:
        assert GameContextRepository(engine).read(NYK_GAME, connection=connection)["game_line"] == line(
            engine, NYK_GAME
        )


def test_an_earlier_read_finishing_later_keeps_the_newer_line_on_postgres(engine):
    payload = scoreboard()
    payload["events"][0]["competitions"][0]["odds"][0].update(details="NY -14", spread=-14.0, overUnder=224.0)
    run(engine, payload, LATER)

    summary = run(engine, scoreboard(), BEFORE_TIP)

    assert (summary["inserted"], summary["updated"], summary["stale"]) == (0, 0, 6)
    stored = line(engine, NYK_GAME)
    assert (stored["spread"], stored["total"], stored["read_at"]) == (
        -14.0,
        224.0,
        "2026-11-10T23:55:00+00:00",
    )


def test_a_run_that_read_earlier_loses_to_a_later_run_that_writes_while_it_is_open_on_postgres(engine):
    later_engine = create_engine(engine.url)
    try:
        later, summary = collect_while_a_later_run_writes(engine, later_engine)
    finally:
        later_engine.dispose()

    assert (later["inserted"], summary["inserted"], summary["stale"]) == (6, 0, 6)
    stored = line(engine, NYK_GAME)
    assert (stored["spread"], stored["total"], stored["read_at"]) == (
        -14.0,
        224.0,
        "2026-11-10T23:55:00+00:00",
    )
