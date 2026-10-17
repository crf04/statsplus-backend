"""Postgres checks for the closing-line backfill (#433): migration 067, real
numerics and timestamps, the end-to-end run and rerun, and the run lock."""

from __future__ import annotations

from datetime import datetime, timezone
import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import downgrade_closing_lines, run_migrations
from app.services.closing_lines import ClosingLineReader, ClosingLineStore, coverage_report
from tests.support.closing_lines_world import (
    CURRY,
    DEN_LAL,
    GSW_LAC,
    GSW_LAC_EVENT,
    LEBRON,
    PAYTON,
    SEASON,
    FakeHistoricalClient,
    build_world,
    seed_game,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

TABLES = {"closing_lines", "closing_line_purchases"}


@pytest.fixture
def url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return url


def test_migration_067_is_idempotent_and_reversible_on_postgres(url):
    engine = create_engine(url)
    run_migrations(engine)
    assert TABLES <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 67"))
    assert run_migrations(engine).applied == ("067_closing_lines",)
    assert run_migrations(engine).applied == ()

    with engine.begin() as connection:
        downgrade_closing_lines(connection)
    assert not TABLES & set(inspect(engine).get_table_names())
    assert run_migrations(engine).applied == ("067_closing_lines",)
    engine.dispose()


def test_a_run_and_a_rerun_on_postgres_store_lines_once_and_spend_nothing_twice(url, tmp_path):
    world = build_world(tmp_path, database_url=url)
    first = FakeHistoricalClient()

    world.backfill(first).run()
    second = FakeHistoricalClient()
    world.backfill(second).run()

    assert first.credits_spent == 22 and second.credits_spent == 0
    assert second.odds_calls == [] and second.event_calls == []
    found = ClosingLineReader(world.engine).for_player(LEBRON, [DEN_LAL, GSW_LAC])
    assert {game: (line.value, line.book) for game, line in found.items()} == {
        DEN_LAL: (24.5, "draftkings")
    }
    assert coverage_report(world.engine, SEASON).by_book == {"draftkings": 5, "fanduel": 2}
    with world.engine.connect() as connection:
        line = connection.execute(
            text("SELECT line, snapshot_at FROM closing_lines WHERE canonical_player_id = :p"),
            {"p": LEBRON},
        ).one()
    assert float(line.line) == 24.5
    # The same instant, whatever zone the session reports it in.
    assert line.snapshot_at == datetime(2025, 11, 5, 0, 24, 41, tzinfo=timezone.utc)


def test_a_second_run_while_one_is_active_buys_nothing(url, tmp_path):
    world = build_world(tmp_path, database_url=url)
    other = ClosingLineStore(create_engine(url))
    client = FakeHistoricalClient()

    with other.exclusive_run() as held:  # another process is mid-backfill
        assert held is True
        summary = world.backfill(client).run()

    assert summary.stop == "another_run_active"
    assert client.quota_calls == 0 and client.odds_calls == [] and client.event_calls == []
    # The lock goes with the run that held it.
    assert world.backfill(FakeHistoricalClient()).run().stop is None


def _stored_lines(world):
    with world.engine.connect() as connection:
        return {
            (game, player): (book, float(line))
            for game, player, book, line in connection.execute(
                text(
                    "SELECT canonical_game_id, canonical_player_id, book, line "
                    "FROM closing_lines"
                )
            )
        }


def _left_unresolved(world):
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Stephen Curry", 27.5), ("draftkings", "Gary Payton", 7.5)],
        lines=[(CURRY, "draftkings", 27.5)],
        unresolved=1,
    )


def test_a_re_resolve_on_postgres_adds_the_line_and_a_second_run_changes_nothing(url, tmp_path):
    world = build_world(tmp_path, database_url=url)
    _left_unresolved(world)

    first = world.re_resolve()
    settled = _stored_lines(world)
    second = world.re_resolve()

    assert settled == {
        (GSW_LAC, CURRY): ("draftkings", 27.5),
        (GSW_LAC, PAYTON): ("draftkings", 7.5),
    }
    assert (first.changed, first.lines_added, first.lines_removed) == (1, 1, 0)
    assert (second.games, second.changed, second.lines_added, second.lines_removed) == (1, 0, 0, 0)
    assert _stored_lines(world) == settled
    with world.engine.connect() as connection:
        assert connection.execute(
            text("SELECT unresolved_count, credits FROM closing_line_purchases")
        ).one() == (0, 10)


def test_a_re_resolve_while_a_backfill_is_active_writes_nothing_but_a_dry_run_reads(url, tmp_path):
    world = build_world(tmp_path, database_url=url)
    _left_unresolved(world)
    other = ClosingLineStore(create_engine(url))

    with other.exclusive_run() as held:  # another process is mid-backfill
        assert held is True
        refused = world.re_resolve()
        dry = world.re_resolve(dry_run=True)

    assert refused.stop == "another_run_active"
    assert _stored_lines(world) == {(GSW_LAC, CURRY): ("draftkings", 27.5)}
    assert (dry.changed, dry.lines_added) == (1, 1)  # a dry run needs no lock
    # The lock goes with the run that held it.
    assert world.re_resolve().stop is None
    assert (GSW_LAC, PAYTON) in _stored_lines(world)
