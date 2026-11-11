"""Postgres checks for the stored daily lineups (crf04/statsplus#492): migration 088,
a rerun that leaves the same rows, and the team read across the Eastern day."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.migrations import run_migrations
from app.models.team_lineup import TeamLineup
from app.services.team_lineups import TeamLineupRepository, collect_team_lineups
from tests.services.test_team_lineups import (
    LATE_EVENING,
    OKC,
    OKC_INACTIVE,
    OKC_STARTERS,
    POR,
    RECORDED,
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
    yield engine
    engine.dispose()


def test_migration_088_reruns_and_a_second_collection_leaves_the_same_rows_on_postgres(engine):
    assert "team_lineups" in inspect(engine).get_table_names()
    assert run_migrations(engine).applied == ()
    schedule(engine, "0022600216", "2026-11-11T04:00:00+00:00", OKC, POR, season="2026-27")

    collect_team_lineups(engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING)
    with engine.connect() as connection:
        first = connection.execute(select(TeamLineup.__table__).order_by("game_id", "team_id")).all()
    collect_team_lineups(engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING)

    with engine.connect() as connection:
        assert connection.execute(select(TeamLineup.__table__).order_by("game_id", "team_id")).all() == first
    assert TeamLineupRepository(engine, clock=lambda: LATE_EVENING).read("OKC") == {
        "status": "posted",
        "game_id": "0022600216",
        "game_date": "2026-11-10",
        "matchup": "OKC @ POR",
        "feed_timestamp": "2026-11-10T22:29:43-05:00",
        "collected_at": "2026-11-11T03:30:00+00:00",
        "starters": OKC_STARTERS,
        "inactive": OKC_INACTIVE,
    }


def test_a_locked_table_fails_the_step_within_its_database_timeout_on_postgres(engine):
    import threading
    import time

    outcome = {}

    def collect():
        started = time.monotonic()
        outcome["result"] = collect_team_lineups(
            engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING
        )
        outcome["elapsed"] = time.monotonic() - started

    with engine.connect() as holder:
        holder.execute(text("LOCK TABLE team_lineups IN ACCESS EXCLUSIVE MODE"))
        worker = threading.Thread(target=collect)
        worker.start()
        worker.join(timeout=9)
        finished_while_locked = not worker.is_alive()
        holder.rollback()
    worker.join()

    assert finished_while_locked, "the step waited on the lock past its timeout"
    assert (outcome["result"]["state"], outcome["result"]["error"]) == ("failed", "OperationalError")
    assert 4.5 < outcome["elapsed"] < 9


def test_lineups_are_stored_while_the_game_lines_table_is_locked_on_postgres(engine, monkeypatch, capsys):
    import functools
    import json
    import threading
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from app.config.settings import OddsApiSettings
    from app.providers import nba_daily_lineups
    from app.services import game_lines
    from scripts import collect_odds

    scoreboard = json.loads(Path("tests/fixtures/espn/scoreboard_2026-11-10.json").read_text())
    monkeypatch.setattr(
        game_lines, "collect", functools.partial(game_lines.collect, fetch=lambda _day: scoreboard)
    )
    monkeypatch.setattr(nba_daily_lineups, "fetch_daily_lineups", lambda _day: RECORDED)
    monkeypatch.setattr(collect_odds, "SIDE_STEP_SECONDS", 2.0)
    settings = SimpleNamespace(odds_api=OddsApiSettings(), log_level="INFO")
    monkeypatch.setattr(collect_odds, "load_settings", lambda **_kw: settings)
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: SimpleNamespace(engine=engine))
    outcome = {}

    def cron_run():
        started = time.monotonic()
        outcome["code"] = collect_odds.main(["--database-url", "postgresql://odds.example/statsplus"])
        outcome["elapsed"] = time.monotonic() - started

    with engine.connect() as holder:
        holder.execute(text("LOCK TABLE game_lines IN ACCESS EXCLUSIVE MODE"))
        run = threading.Thread(target=cron_run)
        run.start()
        run.join(timeout=15)
        finished_while_locked = not run.is_alive()
        holder.rollback()
    run.join()

    assert finished_while_locked, "the game-lines step held the run past its bound"
    assert outcome["code"] == 0 and outcome["elapsed"] < 10
    out = [line for line in capsys.readouterr().out.splitlines() if not line.startswith('{"level"')]
    assert out[1] == "game_lines failed: TimeoutError: still running after 2.0 s"
    assert out[2].startswith("lineups: ") and '"state": "stored"' in out[2]
    with engine.connect() as connection:
        assert connection.execute(select(TeamLineup.__table__)).first() is not None
