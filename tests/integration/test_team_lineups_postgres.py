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


#: The cron's own ``main()`` in a fresh process: Sentry on (offline), the real
#: Postgres engine, recorded ESPN and NBA.com documents, odds off or keyless
#: (argv[2]) and, when argv[3] is set, a shorter side-step bound.  Its last line
#: is the time from ``main()`` to the end of shutdown, which joins non-daemon
#: threads before exit handlers run.
CRON_PROCESS = """
import atexit, functools, json, sys, time
from types import SimpleNamespace
from sentry_sdk.transport import Transport
from sqlalchemy import create_engine
from app.config.settings import OddsApiSettings
from app.providers import nba_daily_lineups
from app.services import game_lines
from app.utils import sentry_setup
from scripts import collect_odds

class Offline(Transport):
    def capture_envelope(self, envelope):
        pass

url, odds, bound = sys.argv[1], sys.argv[2], sys.argv[3]
scoreboard = json.load(open("tests/fixtures/espn/scoreboard_2026-11-10.json"))
lineups = json.load(open("tests/fixtures/nba_daily_lineups/00_daily_lineups_20261110.json"))
game_lines.collect = functools.partial(game_lines.collect, fetch=lambda _day: scoreboard)
nba_daily_lineups.fetch_daily_lineups = lambda _day: lineups
if bound:
    collect_odds.SIDE_STEP_SECONDS = float(bound)
collect_odds.init_sentry = lambda: sentry_setup.init_sentry(transport=Offline())
settings = SimpleNamespace(odds_api=OddsApiSettings(enabled=odds == "keyless"), log_level="INFO")
collect_odds.load_settings = lambda **_kw: settings
collect_odds.build_dependencies = lambda _settings: SimpleNamespace(engine=create_engine(url))
started = time.monotonic()
atexit.register(lambda: print(f"shutdown after {time.monotonic() - started:.1f}", flush=True))
raise SystemExit(collect_odds.main(["--database-url", url]))
"""


def _cron_run(url, odds, bound=""):
    import os
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-c", CRON_PROCESS, url, odds, bound],
        env={**os.environ, "SENTRY_DSN": "https://public@o0.ingest.sentry.io/1"},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _waiting_on_a_lock(engine, holder_pid):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND pid <> :holder"
            ),
            {"holder": holder_pid},
        ).scalar()


def _printed(finished):
    return [line for line in finished.stdout.splitlines() if not line.startswith('{"level"')]


@pytest.mark.parametrize("odds, code", [("off", 0), ("keyless", 1)])
def test_with_game_lines_locked_the_step_fails_on_its_database_timeout_and_leaves_no_session(
    engine, odds, code
):
    url = engine.url.render_as_string(hide_password=False)
    with engine.connect() as holder:
        holder.execute(text("LOCK TABLE game_lines IN ACCESS EXCLUSIVE MODE"))
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()

        finished = _cron_run(url, odds)
        left_waiting = _waiting_on_a_lock(engine, holder_pid)
        holder.rollback()

    assert finished.returncode == code, finished.stdout + finished.stderr
    *out, shutdown = _printed(finished)
    # lock_timeout and statement_timeout are equal, so either may cancel the statement.
    assert out[1].startswith("game_lines failed: OperationalError: ") and "timeout" in out[1]
    (lineups,) = [line for line in out if line.startswith("lineups")]
    assert '"state": "stored"' in lineups
    assert float(shutdown.split()[-1]) < 15
    assert left_waiting == 0
    with engine.connect() as connection:
        assert len(connection.execute(select(TeamLineup.__table__)).all()) == 12


@pytest.mark.parametrize("odds, code", [("off", 0), ("keyless", 1)])
def test_a_step_abandoned_at_its_bound_lets_the_process_exit_and_its_session_ends(engine, odds, code):
    import time

    url = engine.url.render_as_string(hide_password=False)
    with engine.connect() as holder:
        holder.execute(text("LOCK TABLE game_lines IN ACCESS EXCLUSIVE MODE"))
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()

        finished = _cron_run(url, odds, bound="0.5")
        exited = time.monotonic()
        while _waiting_on_a_lock(engine, holder_pid) and time.monotonic() - exited < 10:
            time.sleep(0.2)
        session_ended_after = time.monotonic() - exited
        holder.rollback()

    assert finished.returncode == code, finished.stdout + finished.stderr
    *out, shutdown = _printed(finished)
    assert out[1] == "game_lines failed: TimeoutError: still running after 0.5 s"
    # A shutdown that waited for the abandoned step would take its 5 s lock timeout.
    assert shutdown.startswith("shutdown after ") and float(shutdown.split()[-1]) < 3
    assert session_ended_after < 7


def test_a_bounded_transaction_cancels_a_slow_statement_and_caps_lock_waits_on_postgres(engine):
    import time

    from sqlalchemy.exc import OperationalError

    from app.utils.db import bound_postgres_transaction

    started = time.monotonic()
    with pytest.raises(OperationalError, match="statement timeout"), engine.begin() as connection:
        bound_postgres_transaction(connection)
        assert connection.execute(text("SHOW lock_timeout")).scalar() == "5s"
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "5s"
        connection.execute(text("SELECT pg_sleep(10)"))
    assert time.monotonic() - started < 7
    with engine.connect() as connection:
        with connection.begin():
            bound_postgres_transaction(connection)
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "0"
        assert connection.execute(text("SHOW lock_timeout")).scalar() == "0"
