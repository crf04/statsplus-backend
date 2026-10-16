"""``scripts/backfill_closing_lines.py``: its flags, guards and what it prints (#433)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from types import SimpleNamespace

from app.services.event_mapping_repository import EventMappingRepository
from scripts import backfill_closing_lines as script
from tests.support.closing_lines_world import (
    BOS_NYK,
    DEN_LAL,
    FakeHistoricalClient,
    build_world,
)

KEY = "odds-fake-key-5e4d3c2b1a0f9e8d7c6b5a4938271605"


def _rows(url, table):
    engine = create_engine(url)
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT * FROM {table}")).all()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return build_world(tmp_path)


@pytest.fixture
def light(world, monkeypatch):
    """The container reduced to what the script reads, over the world's database.

    ``build_dependencies`` takes seconds to compose; the tests that exist to
    prove the real wiring (the dry run and the spot check) do not use this.
    """

    from app.services.athlete_mapping_repository import AthleteMappingRepository

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            from app.services.athlete_catalog_service import _read_catalog_rows

            return _read_catalog_rows(world.engine, season, active_only=active_only)

    deps = SimpleNamespace(
        engine=world.engine,
        event_catalog_service=world.catalog_service,
        event_mapping_repository=EventMappingRepository(world.engine, settings=world.settings),
        athlete_catalog_service=Catalog(),
        athlete_mapping_repository=AthleteMappingRepository(world.engine),
    )
    monkeypatch.setattr(script, "build_dependencies", lambda settings: deps)
    return deps


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


def test_a_dry_run_prints_the_plan_and_the_estimate_and_makes_no_call_and_no_write(
    world, client, capsys
):
    code = script.main(["--dry-run", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0
    assert "games in 2025-26 (regular season and playoffs): 3" in out
    assert "to buy: 3 games over 2 game days" in out
    assert "estimated credits: 32 (3 x 10 for the games + 2 x 1 for the event lists" in out
    assert "dry run: no call was made" in out
    assert client.made == []  # the client is never even built, so no key is needed
    assert _rows(world.url, "closing_line_purchases") == []
    assert _rows(world.url, "closing_lines") == []


def test_a_dry_run_with_a_spot_check_estimates_only_those_games(world, light, client, capsys):
    script.main(["--dry-run", "--spot-check", "1", "--database-url", world.url])

    out = capsys.readouterr().out
    assert "to buy: 1 games over 1 game days" in out
    assert "estimated credits: 11" in out


def test_a_spot_check_buys_n_games_then_prints_the_coverage_report(
    world, client, monkeypatch, capsys
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
    assert "credits spent this run: 12" in out
    assert "players with 20+ minutes: 7; with a closing line: 5 (71.4%)" in out
    assert "  draftkings: 3 (42.9%)" in out and "  fanduel: 2 (28.6%)" in out
    assert "  0022500103 Jayson Tatum 36 min" in out
    assert KEY not in out and "apiKey" not in out
    assert world.url not in out and "closing.sqlite3" not in out
    # The client is the sportsbook region's, with the key from the environment only.
    (api_key, kwargs), = client.made
    assert (api_key, kwargs["regions"]) == (KEY, "us")
    assert {row[0] for row in _rows(world.url, "closing_line_purchases")} == {DEN_LAL, BOS_NYK}


def test_the_full_run_then_a_rerun_prints_zero_credits(world, light, client, monkeypatch, capsys):
    monkeypatch.setenv("ODDS_API_KEY", KEY)
    assert script.main(["--database-url", world.url]) == 0
    capsys.readouterr()
    spent_first = client.credits_spent

    assert script.main(["--database-url", world.url]) == 0

    out = capsys.readouterr().out
    assert spent_first == 22 and client.credits_spent == 22
    assert "already recorded: 3" in out and "to buy: 0 games" in out
    assert "credits spent this run: 0" in out


def test_the_spend_cap_is_read_from_the_command_line(world, light, client, monkeypatch, capsys):
    monkeypatch.setenv("ODDS_API_KEY", KEY)

    code = script.main(["--max-credits", "11", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0  # a guard doing its job is not a failure
    assert "stopped early: max_credits" in out
    assert client.credits_spent == 11


def test_the_remaining_floor_is_read_from_the_command_line(
    world, light, client, monkeypatch, capsys
):
    monkeypatch.setenv("ODDS_API_KEY", KEY)

    code = script.main(["--keep-remaining", "99989", "--database-url", world.url])

    out = capsys.readouterr().out
    assert code == 0
    assert "stopped early: keep_remaining" in out
    assert client.credits_spent == 11 and client.remaining == 99989


def test_the_spend_cap_default_is_15000_and_the_floor_5000(world, light, client, monkeypatch):
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


def test_a_run_without_a_key_fails_before_any_call(world, light, client, capsys):
    code = script.main(["--database-url", world.url])

    assert code == 1
    assert "ODDS_API_KEY is not set" in capsys.readouterr().out
    assert client.made == [] and client.credits_spent == 0


def test_a_rejected_key_is_a_failure_exit(world, light, client, monkeypatch, capsys):
    from app.providers.odds_api import OddsApiError

    monkeypatch.setenv("ODDS_API_KEY", KEY)
    client.errors["a1b2c3d4e5f60718293a4b5c6d7e8f90"] = OddsApiError(
        "unauthorized", status=401, path="/odds"
    )

    code = script.main(["--database-url", world.url])

    assert code == 1
    assert "stopped early: unauthorized" in capsys.readouterr().out


def test_a_database_without_migration_067_is_refused(tmp_path, client, capsys):
    url = f"sqlite:///{tmp_path / 'bare.db'}"
    create_engine(url).dispose()
    from app.migrations import run_migrations

    engine = create_engine(url)
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE closing_line_purchases"))

    assert script.main(["--dry-run", "--database-url", url]) == 1
    assert "migration 067 has not run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [["--max-credits", "0"], ["--spot-check", "0"], ["--keep-remaining", "-1"], []],
)
def test_bad_arguments_are_refused(argv, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exit_:
        script.main(argv)

    assert exit_.value.code == 2


def test_the_read_only_demo_database_is_refused(capsys):
    with pytest.raises(SystemExit) as exit_:
        script.main(["--dry-run", "--database-url", "sqlite:///nba_play_types.db"])

    assert exit_.value.code == 2
