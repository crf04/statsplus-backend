"""Postgres checks for on/off lineup segments (crf04/statsplus#410): migration
084, the signed 64-bit mask predicate, parity through the real SQL, and
concurrent writers of one team-season."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from threading import Barrier, BrokenBarrierError

import pytest
from sqlalchemy import create_engine, event, inspect, text

from app.migrations import downgrade_lineup_segments, run_migrations
from app.services.lineup_segment_repository import write_game
from app.services.lineup_segments import COUNTS, GameSegments, Segment
from scripts.on_off_parity import parity, within_tolerance

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

TABLES = {"lineup_roster_slots", "lineup_segment_games", "lineup_segment_players", "lineup_segment_seasons"}


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


def test_migration_084_is_idempotent_and_reversible_on_postgres(engine):
    assert TABLES <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 84"))
    assert run_migrations(engine).applied == ("084_lineup_segments",)
    assert run_migrations(engine).applied == ()

    with engine.begin() as connection:
        downgrade_lineup_segments(connection)
    assert not TABLES & set(inspect(engine).get_table_names())
    assert run_migrations(engine).applied == ("084_lineup_segments",)


def test_slot_63_sets_the_sign_bit_and_the_mask_predicate_still_selects_on_postgres(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lineup_segment_players VALUES "
                "('0022500001', 1, 0, 7, '2025-26', now(), :mask, 6000, 0,0,0,0,0,0,0,0, 0,0,0,0,0)"
            ),
            {"mask": -(1 << 63) | 1},
        )
        selected = connection.execute(
            text(
                "SELECT count(*) FROM lineup_segment_players "
                "WHERE (lineup_mask & CAST(:on AS bigint)) = CAST(:on AS bigint) AND (lineup_mask & 2) = 0"
            ),
            {"on": -(1 << 63)},
        ).scalar()
    assert selected == 1


def test_the_den_2026_27_splits_match_pbp_stats_through_postgres(engine):
    results = parity("den_2026_27", engine)

    assert sorted(results) == ["jokic_off", "jokic_on"]
    assert all(within_tolerance(result) for result in results.values())


def _two_team_game(game_id, home, away):
    players = {team: tuple(team * 10 + n for n in range(1, 6)) for team in (home, away)}
    return GameSegments(
        game_id,
        home,
        away,
        {player: str(player) for lineup in players.values() for player in lineup},
        tuple(
            Segment(
                team,
                0,
                players[team],
                6000,
                {p: dict.fromkeys(COUNTS, 0) for p in players[team]},
                0,
                0,
                0,
                0,
                0,
            )
            for team in (home, away)
        ),
    )


def test_writers_of_a_home_and_away_game_and_its_return_game_both_store(engine):
    """A writer of 1-home/2-away and one of 2-home/1-away, each paused right after
    its first slot lock (or, taking none, its first slot read), both store their
    game with distinct slots.

    The pause lets both writers get that far before either goes on: writers
    that lock team-seasons in home/away order then deadlock, and writers that
    take no lock both hand out the same slots.
    """

    barrier = Barrier(2)

    @event.listens_for(engine, "after_cursor_execute")
    def pause_after_first_slot_statement(connection, cursor, statement, parameters, context, executemany):
        slot_statement = "pg_advisory_xact_lock" in statement or "FROM lineup_roster_slots" in statement
        if slot_statement and not connection.info.get("paused"):
            connection.info["paused"] = True
            # A writer blocked behind the other's lock never arrives.
            with suppress(BrokenBarrierError):
                barrier.wait(timeout=2)

    def store(game):
        with engine.begin() as connection:
            connection.exec_driver_sql("SET deadlock_timeout = '100ms'")
            connection.exec_driver_sql("SET statement_timeout = '10s'")
            write_game(connection, "2025-26", game, datetime.now(UTC))
        return "stored"

    games = [_two_team_game("0022500001", 1, 2), _two_team_game("0022500002", 2, 1)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = []
        for future in [executor.submit(store, game) for game in games]:
            try:
                outcomes.append(future.result(timeout=30))
            except Exception as error:
                outcomes.append(f"{type(error).__name__}: {str(error).splitlines()[0]}")

    assert outcomes == ["stored", "stored"]
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT game_id FROM lineup_segment_games WHERE rejected_reason IS NULL ORDER BY game_id")
        ).scalars()
        slots = connection.execute(
            text("SELECT team_id, player_id, slot FROM lineup_roster_slots ORDER BY team_id, slot")
        ).all()
    assert list(stored) == ["0022500001", "0022500002"]
    assert slots == [(team, team * 10 + n, n - 1) for team in (1, 2) for n in range(1, 6)]
