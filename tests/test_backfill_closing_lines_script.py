"""``scripts/backfill_closing_lines.py``: its flags, guards and what it prints (#433)."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
import pytest

from app.providers.odds_api import OddsApiError
from app.services.closing_lines import ClosingLineStore
from scripts import backfill_closing_lines as script
from tests.support.closing_lines_world import (
    BOS_NYK,
    DEN_LAL,
    DEN_LAL_EVENT,
    FakeHistoricalClient,
    build_world,
)

KEY = "odds-fake-key-5e4d3c2b1a0f9e8d7c6b5a4938271605"


def _rows(url, table):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(f"SELECT * FROM {table}")).all()
    finally:
        engine.dispose()


def _row_counts(url):
    """Every table's row count: a dry run must leave all of them as they were."""

    engine = create_engine(url)
    try:
        counts = {}
        with engine.connect() as connection:
            for table in inspect(engine).get_table_names():
                counts[table] = connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
        return counts
    finally:
        engine.dispose()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return build_world(tmp_path)


@pytest.fixture
def client(monkeypatch):
    """The script's OddsApiClient, replaced by the recorded-fixture fake."""

    made = []
    fake = FakeHistoricalClient()

    def build(api_key, **kwargs):
        made.append((api_key, kwargs))
        return fake

    monkeypatch.setattr(script, "OddsApiClient", build)
    fake.made = made
    return fake


@pytest.fixture
def unlocked(monkeypatch):
    """Paid runs over SQLite, which the store otherwise refuses (one process here)."""

    monkeypatch.setattr(
        script, "ClosingLineStore", lambda engine: ClosingLineStore(engine, lock_runs=False)
    )


def test_a_dry_run_prints_the_plan_and_the_estimate_and_makes_no_call_and_no_write(
    world, client, capsys
):
    before = _row_counts(world.url)

    code = script.main(["--dry-run", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0
    assert "games in 2025-26 (regular season and playoffs): 3" in out
    assert "to buy: 3 games over 2 game days" in out
    assert "estimated credits: 32 (3 x 10 for the games + 2 x 1 for the event lists" in out
    assert "credits already spent by earlier runs: 0; cap for the whole backfill (--max-credits): 15000" in out
    assert "dry run: no call was made" in out
    assert client.made == []  # the client is never even built, so no key is needed
    assert _row_counts(world.url) == before  # not one row anywhere, in any table


def test_the_script_composes_no_application_container():
    """``build_dependencies`` registers publication streams and starts the
    refresh-job dispatcher; a laptop run against production must do neither."""

    from pathlib import Path

    assert not hasattr(script, "build_dependencies")
    assert "build_dependencies" not in Path(script.__file__).read_text()


def test_a_dry_run_with_a_spot_check_estimates_only_those_games(world, client, capsys):
    script.main(["--dry-run", "--spot-check", "1", "--database-url", world.url])

    out = capsys.readouterr().out
    assert "to buy: 1 games over 1 game days" in out
    assert "estimated credits: 11" in out


def test_a_spot_check_buys_n_games_then_prints_the_coverage_report(
    world, client, unlocked, monkeypatch, capsys
):
    monkeypatch.setenv("ODDS_API_KEY", KEY)

    code = script.main(["--spot-check", "2", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0
    assert [call[0] for call in client.odds_calls] == [
        "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "c3d4e5f60718293a4b5c6d7e8f90a1b2",
    ]
    assert "games: planned=2 bought=1 empty=1 failed=0 no_event=0" in out
    assert "credits spent this run: 12; for the whole backfill: 12 (remaining at the vendor: 99988)" in out
    assert "players with 20+ minutes: 7; with a closing line: 5 (71.4%)" in out
    assert "  draftkings: 3 (42.9%)" in out and "  fanduel: 2 (28.6%)" in out
    assert "  0022500103 Jayson Tatum 36 min" in out
    assert "credits recorded (games and event lists): 12" in out
    assert KEY not in out and "apiKey" not in out
    assert world.url not in out and "closing.sqlite3" not in out
    # The client is the sportsbook region's, with the key from the environment only.
    (api_key, kwargs), = client.made
    assert (api_key, kwargs["regions"]) == (KEY, "us")
    assert {row[0] for row in _rows(world.url, "closing_line_purchases")} == {DEN_LAL, BOS_NYK}


def test_the_full_run_then_a_rerun_prints_zero_credits(world, client, unlocked, monkeypatch, capsys):
    monkeypatch.setenv("ODDS_API_KEY", KEY)
    assert script.main(["--database-url", world.url]) == 0
    capsys.readouterr()
    spent_first = client.credits_spent

    assert script.main(["--database-url", world.url]) == 0

    out = capsys.readouterr().out
    assert spent_first == 22 and client.credits_spent == 22
    assert "already recorded: 3" in out and "to buy: 0 games" in out
    assert "credits spent this run: 0; for the whole backfill: 22" in out


def test_the_spend_cap_is_read_from_the_command_line(world, client, unlocked, monkeypatch, capsys):
    monkeypatch.setenv("ODDS_API_KEY", KEY)

    code = script.main(["--max-credits", "11", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0  # a guard doing its job is not a failure
    assert "stopped early: max_credits" in out
    assert client.credits_spent == 11


def test_the_remaining_floor_is_read_from_the_command_line(
    world, client, unlocked, monkeypatch, capsys
):
    monkeypatch.setenv("ODDS_API_KEY", KEY)

    code = script.main(["--keep-remaining", "99989", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0
    assert "stopped early: keep_remaining" in out
    assert client.credits_spent == 11 and client.remaining == 99989


def test_the_spend_cap_default_is_15000_and_the_floor_5000(world, monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def plan(self, spot_check=None):
            raise SystemExit(0)

    monkeypatch.setattr(script, "ClosingLineBackfill", Recorder)

    with pytest.raises(SystemExit):
        script.main(["--dry-run", "--database-url", world.url])

    assert (seen["max_credits"], seen["keep_remaining"]) == (15000, 5000)


def test_a_paid_run_over_a_database_that_cannot_lock_runs_is_refused_before_any_call(
    world, client, monkeypatch, capsys
):
    monkeypatch.setenv("ODDS_API_KEY", KEY)

    code = script.main(["--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 1
    assert "failed: a paid backfill needs PostgreSQL" in out
    assert client.quota_calls == 0 and client.odds_calls == [] and client.event_calls == []


def test_a_run_without_a_key_fails_before_any_call(world, client, unlocked, capsys):
    code = script.main(["--database-url", world.url])

    assert code == 1
    assert "ODDS_API_KEY is not set" in capsys.readouterr().out
    assert client.made == [] and client.credits_spent == 0


def test_a_rejected_key_is_a_failure_exit(world, client, unlocked, monkeypatch, capsys):
    monkeypatch.setenv("ODDS_API_KEY", KEY)
    client.errors[DEN_LAL_EVENT] = OddsApiError("unauthorized", status=401, path="/odds")

    code = script.main(["--database-url", world.url])

    assert code == 1
    assert "stopped early: unauthorized" in capsys.readouterr().out


@pytest.mark.parametrize(
    "table", ["closing_line_purchases", "closing_line_spend", "closing_lines"]
)
def test_every_table_of_migration_067_is_required(world, client, capsys, table):
    engine = create_engine(world.url)
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {table}"))
    engine.dispose()

    assert script.main(["--dry-run", "--database-url", world.url]) == 1
    assert "migration 067 has not run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "flags",
    [["--max-credits", "0"], ["--spot-check", "0"], ["--keep-remaining", "-1"]],
)
def test_bad_arguments_are_refused_even_with_a_good_database(world, flags):
    with pytest.raises(SystemExit) as exit_:
        script.main(["--dry-run", "--database-url", world.url, *flags])

    assert exit_.value.code == 2


def test_a_missing_database_url_is_refused(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as exit_:
        script.main(["--dry-run"])

    assert exit_.value.code == 2


def test_the_read_only_demo_database_is_refused():
    with pytest.raises(SystemExit) as exit_:
        script.main(["--dry-run", "--database-url", "sqlite:///nba_play_types.db"])

    assert exit_.value.code == 2
