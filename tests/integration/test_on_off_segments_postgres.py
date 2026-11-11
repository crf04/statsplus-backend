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
from sqlalchemy import create_engine, event, insert, inspect, text

from app.migrations import downgrade_lineup_segments, run_migrations
from app.models.event_catalog import EventCatalogEntry
from app.services.lineup_segment_repository import LineupSegmentRepository, write_game
from app.services.lineup_segments import COUNTS, GameSegments, Segment
from scripts.lineup_segment_backfill import fill_makes
from scripts.on_off_parity import load_games, parity, within_tolerance
from tests.services.test_on_off_segments import BOS, BROWN, FakeLiveData, _collection, _dump, _event

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


def test_naming_no_player_sums_every_segment_of_the_team_on_postgres(engine):
    with engine.begin() as connection:
        write_game(
            connection, "2025-26", _two_team_game("0022500001", 1, 2), datetime(2026, 11, 1, tzinfo=UTC)
        )
        write_game(
            connection, "2025-26", _two_team_game("0022500002", 2, 1), datetime(2026, 11, 2, tzinfo=UTC)
        )
    repository = LineupSegmentRepository(engine)
    repository.record_coverage("2025-26", datetime(2026, 11, 3, tzinfo=UTC))

    split = repository.read_split("2025-26", 1, (), ())

    assert sorted((row["player_id"], row["duration_cs"]) for row in split.rows) == [
        (11, 12000),
        (12, 12000),
        (13, 12000),
        (14, 12000),
        (15, 12000),
    ]
    assert split.written_at == datetime(2026, 11, 2, tzinfo=UTC)


def test_a_split_counts_the_team_games_it_covers_on_postgres(engine):
    with engine.begin() as connection:
        write_game(
            connection, "2025-26", _two_team_game("0022500001", 1, 2), datetime(2026, 11, 1, tzinfo=UTC)
        )
        write_game(
            connection, "2025-26", _two_team_game("0022500002", 2, 1), datetime(2026, 11, 2, tzinfo=UTC)
        )
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [
                _event("0022500001", datetime(2025, 10, 22, 23, 0, tzinfo=UTC), home=("A", 1), away=("B", 2)),
                _event("0022500002", datetime(2025, 10, 24, 23, 0, tzinfo=UTC), home=("B", 2), away=("A", 1)),
                _event("0022500003", datetime(2025, 10, 26, 23, 0, tzinfo=UTC), home=("A", 1), away=("B", 2)),
            ],
        )
    repository = LineupSegmentRepository(engine)
    repository.record_coverage("2025-26", datetime(2026, 11, 3, tzinfo=UTC))

    split = repository.read_split("2025-26", 1, (11,), ())

    assert (
        split.team_games,
        split.games_included,
        split.games_through,
        split.split_games,
        split.last_split_game_date,
    ) == (3, 2, "2025-10-24", 2, "2025-10-24")
    assert [game["game_id"] for game in split.games_missing] == ["0022500003"]


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


def test_migration_089_adds_the_makes_without_rewriting_the_table_on_postgres(engine):
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE lineup_segment_players DROP COLUMN fgm, DROP COLUMN ftm"))
        connection.execute(
            text(
                "INSERT INTO lineup_segment_players VALUES "
                "('0022500001', 1, 0, 7, '2025-26', now(), 1, 6000, 5,1,2,1,3,1,0,0, 10,2,1,0,9)"
            )
        )
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 89"))
    relfilenode = "SELECT relfilenode FROM pg_class WHERE relname = 'lineup_segment_players'"
    with engine.connect() as connection:
        before = connection.execute(text(relfilenode)).scalar()

    assert run_migrations(engine).applied == ("089_lineup_segment_makes",)
    assert run_migrations(engine).applied == ()

    with engine.connect() as connection:
        assert connection.execute(text(relfilenode)).scalar() == before
        assert connection.execute(text("SELECT pts, fgm, ftm FROM lineup_segment_players")).all() == [
            (5, None, None)
        ]


def test_the_makes_fill_ends_with_the_rows_a_fresh_collection_writes_on_postgres(engine):
    games = load_games("bos_2025_26")
    game_ids = sorted(games)[:2]
    _collection(engine, games).collect("2025-26", game_ids=game_ids)
    fresh = _dump(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE lineup_segment_players SET fgm = NULL, ftm = NULL"))
    segments = LineupSegmentRepository(engine)

    result = fill_makes(FakeLiveData(games), segments, "2025-26", throttle_seconds=0)

    assert (result["filled"], result["mismatched"], result["remaining"]) == (2, [], 0)
    assert _dump(engine) == fresh
    assert segments.games_without_makes("2025-26") == []
    segments.record_coverage("2025-26", datetime(2026, 11, 3, tzinfo=UTC))

    def brown():
        row = next(r for r in segments.read_split("2025-26", BOS, (), ()).rows if r["player_id"] == BROWN)
        return row["fgm"], row["ftm"]

    assert brown() == (20, 10)
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE lineup_segment_players SET fgm = NULL, ftm = NULL WHERE game_id = '{game_ids[1]}'")
        )
    assert brown() == (None, None)
