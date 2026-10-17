"""Re-resolving the closing lines from the quotes kept on the ledger (#437).

A resolver fix reaches games that are already bought: the ledger keeps each
game's quotes, so the lines are written again with no call and no credit.  The
ledger below is seeded as the first backfill left it, before the fix: the lines
of the names it resolved, and an ``unresolved_count`` for those it did not.
Everything else (event catalog, box scores, name resolution, line table,
coverage query) is the real code over a migrated SQLite database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select, text

from app.models.athlete_catalog import AthleteCatalog
from app.models.closing_lines import ClosingLine, ClosingLinePurchase
from app.services.athlete_mapping_errors import AthleteMappingPersistenceError
from app.services.closing_lines import coverage_report
from tests.support.closing_lines_world import (
    BOS_NYK,
    BOS_NYK_EVENT,
    CURRY,
    DEN,
    DEN_LAL,
    DEN_LAL_EVENT,
    GSW,
    GSW_LAC,
    GSW_LAC_EVENT,
    JOKIC,
    LEBRON,
    PAYTON,
    REAVES,
    SEASON,
    add_player,
    build_world,
    seed_game,
)


def lines(world):
    table = ClosingLine.__table__
    with world.engine.connect() as connection:
        rows = connection.execute(
            select(table.c.canonical_game_id, table.c.canonical_player_id, table.c.book, table.c.line)
        )
        return {(game, player): (book, line) for game, player, book, line in rows}


def ledger(world):
    table = ClosingLinePurchase.__table__
    with world.engine.connect() as connection:
        return {
            row.canonical_game_id: (row.outcome, row.unresolved_count, row.credits)
            for row in connection.execute(select(table))
        }


def snapshot(world):
    """Every table's rows, in a stable order: what a dry run must leave alone."""

    with world.engine.connect() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            )
        ]
        return {
            table: sorted(map(repr, connection.execute(text(f'SELECT * FROM "{table}"')).all()))
            for table in tables
        }


def first_backfill_left_payton_unresolved(world):
    """GSW-LAC after the first backfill: Payton was posted as ``Gary Payton``."""

    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [
            ("draftkings", "Stephen Curry", 27.5),
            ("draftkings", "Gary Payton", 7.5),
            ("draftkings", "Zach Unknown", 3.5),
        ],
        lines=[(CURRY, "draftkings", 27.5)],
        unresolved=2,
    )


# -- what a re-resolve writes ---------------------------------------------------


def test_a_name_that_resolves_now_gets_its_line_and_the_unresolved_count_drops(tmp_path):
    world = build_world(tmp_path)
    first_backfill_left_payton_unresolved(world)

    summary = world.re_resolve()

    assert lines(world) == {
        (GSW_LAC, CURRY): ("draftkings", 27.5),
        (GSW_LAC, PAYTON): ("draftkings", 7.5),
    }
    # "Zach Unknown" is on no roster: still unresolved, and counted.
    assert ledger(world)[GSW_LAC] == ("complete", 1, 10)
    assert (summary.games, summary.changed, summary.lines_added, summary.lines_removed) == (
        1,
        1,
        1,
        0,
    )
    assert (summary.unresolved_before, summary.unresolved_after) == (2, 1)
    assert dict(summary.still_unresolved) == {"Zach Unknown": 1}


def test_the_book_order_still_decides_when_a_new_name_brings_a_better_book(tmp_path):
    """FanDuel posted Payton as the catalog spells him, DraftKings as ``Gary Payton``."""

    world = build_world(tmp_path)
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Gary Payton", 7.5), ("fanduel", "Gary Payton II", 8.5)],
        lines=[(PAYTON, "fanduel", 8.5)],
        unresolved=1,
    )

    summary = world.re_resolve()

    assert lines(world) == {(GSW_LAC, PAYTON): ("draftkings", 7.5)}
    assert (summary.lines_added, summary.lines_removed) == (1, 1)


def test_a_player_the_catalog_lists_inactive_gets_his_line_when_the_box_score_has_him(tmp_path):
    """Waived since: the catalog says inactive, the box score says he played."""

    world = build_world(tmp_path)
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.update()
            .where(AthleteCatalog.__table__.c.player_id == PAYTON)
            .values(is_active=False, is_active_for_season=False, roster_status="inactive")
        )
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Gary Payton II", 7.5)],
        unresolved=1,
    )

    world.re_resolve()

    assert lines(world) == {(GSW_LAC, PAYTON): ("draftkings", 7.5)}
    assert ledger(world)[GSW_LAC] == ("complete", 0, 10)


def test_a_name_that_resolves_but_loses_to_a_better_book_still_lowers_the_unresolved_count(tmp_path):
    """The lines are right already; only the ledger's count of misses is stale."""

    world = build_world(tmp_path)
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Gary Payton II", 7.5), ("fanduel", "Gary Payton", 8.5)],
        lines=[(PAYTON, "draftkings", 7.5)],
        unresolved=1,
    )

    summary = world.re_resolve()

    assert lines(world) == {(GSW_LAC, PAYTON): ("draftkings", 7.5)}
    assert ledger(world)[GSW_LAC] == ("complete", 0, 10)
    assert (summary.changed, summary.lines_added, summary.lines_removed) == (1, 0, 0)


def test_an_inactive_player_who_did_not_play_that_game_stays_unresolved(tmp_path):
    world = build_world(tmp_path)
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.update()
            .where(AthleteCatalog.__table__.c.player_id == PAYTON)
            .values(is_active=False, is_active_for_season=False, roster_status="inactive")
        )
    # Payton is in the GSW-LAC box score only; DEN-LAL is not his game.
    seed_game(
        world,
        DEN_LAL,
        DEN_LAL_EVENT,
        [("draftkings", "Gary Payton II", 7.5), ("draftkings", "Nikola Jokić", 28.5)],
        lines=[(JOKIC, "draftkings", 28.5)],
        unresolved=1,
    )

    summary = world.re_resolve()

    assert lines(world) == {(DEN_LAL, JOKIC): ("draftkings", 28.5)}
    assert summary.changed == 0


def test_only_games_with_stored_quotes_are_read_and_the_others_are_left_alone(tmp_path):
    world = build_world(tmp_path)
    first_backfill_left_payton_unresolved(world)
    seed_game(world, BOS_NYK, BOS_NYK_EVENT, [], outcome="empty")

    summary = world.re_resolve()

    assert ledger(world)[BOS_NYK] == ("empty", 0, 10)
    assert summary.games == 1


def test_a_game_whose_lines_were_never_written_is_completed(tmp_path):
    world = build_world(tmp_path)
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Stephen Curry", 27.5)],
        outcome="purchased",
    )
    # Books answered but posted no points props: nothing to write, still done.
    seed_game(world, BOS_NYK, BOS_NYK_EVENT, [], outcome="purchased")

    world.re_resolve()

    assert lines(world) == {(GSW_LAC, CURRY): ("draftkings", 27.5)}
    assert ledger(world)[GSW_LAC] == ("complete", 0, 10)
    assert ledger(world)[BOS_NYK] == ("complete", 0, 10)


def test_a_recorded_game_the_event_catalog_does_not_hold_is_skipped(tmp_path):
    world = build_world(tmp_path)
    seed_game(
        world,
        "0022599999",
        "ffffffffffffffffffffffffffffffff",
        [("draftkings", "Stephen Curry", 27.5)],
        unresolved=4,
    )

    summary = world.re_resolve()

    assert (summary.games, summary.skipped, summary.changed) == (0, 1, 0)
    assert ledger(world)["0022599999"] == ("complete", 4, 10)
    assert lines(world) == {}


# -- idempotent, free, and safe to rehearse -----------------------------------------


def _write_statements(world):
    """Collect every INSERT, UPDATE or DELETE sent to a closing-line table."""

    seen: list[str] = []

    @event.listens_for(world.engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().split(None, 1)[0].upper()
        if head in {"INSERT", "UPDATE", "DELETE"} and "closing_line" in statement:
            seen.append(statement)

    return seen


def test_a_second_run_changes_nothing_and_writes_nothing(tmp_path):
    world = build_world(tmp_path)
    first_backfill_left_payton_unresolved(world)
    seed_game(
        world,
        DEN_LAL,
        DEN_LAL_EVENT,
        [("draftkings", "LeBron James", 24.5), ("fanduel", "Nikola Jokic", 29.5)],
        lines=[(LEBRON, "draftkings", 24.5), (JOKIC, "fanduel", 29.5)],
    )
    assert world.re_resolve().changed == 1
    settled = (lines(world), ledger(world))
    writes = _write_statements(world)

    second = world.re_resolve()

    assert (second.games, second.changed, second.lines_added, second.lines_removed) == (
        2,
        0,
        0,
        0,
    )
    assert (second.unresolved_before, second.unresolved_after) == (1, 1)
    assert (lines(world), ledger(world)) == settled
    assert writes == []  # not even the same rows written again


def test_a_dry_run_writes_nothing_anywhere_and_projects_what_a_real_run_reaches(tmp_path):
    world = build_world(tmp_path)
    first_backfill_left_payton_unresolved(world)
    seed_game(
        world,
        DEN_LAL,
        DEN_LAL_EVENT,
        [("draftkings", "LeBron James", 24.5), ("draftkings", "Austin Reaves", 14.5)],
        lines=[(LEBRON, "draftkings", 24.5)],
        unresolved=0,
    )
    # A purchase whose lines were never written: a real run completes it, and the
    # projected report must say so too.
    seed_game(world, BOS_NYK, BOS_NYK_EVENT, [], outcome="purchased")
    before = snapshot(world)
    coverage_before = coverage_report(world.engine, SEASON)

    dry = world.re_resolve(dry_run=True)

    assert snapshot(world) == before  # not one row in any table, mapping tables included
    projected = coverage_report(world.engine, SEASON, projected=dry.projected)
    assert (coverage_before.with_line, projected.with_line) == (2, 4)
    assert dict(projected.by_book) == {"draftkings": 4}

    real = world.re_resolve()

    assert real == dry
    assert coverage_report(world.engine, SEASON) == projected
    assert snapshot(world) != before


def test_a_dry_run_honours_a_rejected_name_as_a_real_run_does(tmp_path):
    world = build_world(tmp_path)
    first_backfill_left_payton_unresolved(world)
    world.athlete_repository.reject("oddsapi", "name:garypayton", operator_id="operator", reason="not him")

    dry = world.re_resolve(dry_run=True)
    real = world.re_resolve()

    assert dry.lines_added == real.lines_added == 0
    assert (GSW_LAC, PAYTON) not in lines(world)
    assert dict(real.still_unresolved) == {"Gary Payton": 1, "Zach Unknown": 1}


def test_a_re_resolve_records_no_mapping_in_either_mode(tmp_path):
    """A posted name is one provider identity for the whole season; recording it
    game by game would let one game's player decide the next game's."""

    world = build_world(tmp_path)
    first_backfill_left_payton_unresolved(world)

    world.re_resolve(dry_run=True)
    world.re_resolve()

    assert world.athlete_repository.get_mapping("oddsapi", "name:garypayton") is None
    assert (GSW_LAC, PAYTON) in lines(world)  # resolved all the same


def test_one_posted_name_that_fits_different_men_in_different_games_is_resolved_game_by_game(
    tmp_path,
):
    """Synthetic father and son: a bare ``Tim Hardaway`` fits Jr. on one game's
    rosters and Sr. on another's.  The same in a dry run and a real run, and
    nothing changes the second time."""

    world = build_world(tmp_path)
    add_player(world, 900101, "Tim Hardaway Jr.", DEN, DEN_LAL)
    add_player(world, 900102, "Tim Hardaway Sr.", GSW, GSW_LAC)
    for game, event_id, line in [
        (DEN_LAL, DEN_LAL_EVENT, 20.5),
        (GSW_LAC, GSW_LAC_EVENT, 9.5),
    ]:
        seed_game(world, game, event_id, [("draftkings", "Tim Hardaway", line)], unresolved=1)

    dry = world.re_resolve(dry_run=True)
    first = world.re_resolve()
    second = world.re_resolve()

    assert lines(world) == {
        (DEN_LAL, 900101): ("draftkings", 20.5),
        (GSW_LAC, 900102): ("draftkings", 9.5),
    }
    assert ledger(world)[DEN_LAL] == ("complete", 0, 10)
    assert dry == first
    assert (second.changed, second.lines_added, second.lines_removed) == (0, 0, 0)


def unreadable(world, failing):
    """The world's mappings with one read down and the others working."""

    inner = world.athlete_repository

    class Mappings:
        def get_mapping(self, *args, **kwargs):
            return inner.get_mapping(*args, **kwargs)

        def get_mappings(self, *args, **kwargs):
            return inner.get_mappings(*args, **kwargs)

        def get_rejection(self, *args, **kwargs):
            return inner.get_rejection(*args, **kwargs)

        def get_rejections(self, *args, **kwargs):
            return inner.get_rejections(*args, **kwargs)

    def down(*args, **kwargs):
        raise AthleteMappingPersistenceError("storage is down")

    setattr(Mappings, failing, down)
    return Mappings()


@pytest.mark.parametrize("failing", ["get_mappings", "get_rejections"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_a_storage_fault_reading_the_mappings_stops_the_run_and_leaves_every_line(tmp_path, dry_run, failing):
    world = build_world(tmp_path)
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Stephen Curry", 27.5)],
        lines=[(CURRY, "draftkings", 27.5)],
    )

    with pytest.raises(AthleteMappingPersistenceError):
        world.re_resolve(dry_run=dry_run, mappings=unreadable(world, failing))

    # Not a miss: the line stays, and so does the ledger's count.
    assert lines(world) == {(GSW_LAC, CURRY): ("draftkings", 27.5)}
    assert ledger(world)[GSW_LAC] == ("complete", 0, 10)


def test_a_line_whose_name_is_rejected_since_is_removed_and_the_dry_run_projects_it(tmp_path):
    world = build_world(tmp_path)
    seed_game(
        world,
        GSW_LAC,
        GSW_LAC_EVENT,
        [("draftkings", "Stephen Curry", 27.5)],
        lines=[(CURRY, "draftkings", 27.5)],
    )
    world.athlete_repository.reject("oddsapi", "name:stephencurry", operator_id="operator", reason="not him")
    before = coverage_report(world.engine, SEASON)

    dry = world.re_resolve(dry_run=True)
    projected = coverage_report(world.engine, SEASON, projected=dry.projected)

    assert (before.with_line, projected.with_line) == (1, 0)
    real = world.re_resolve()
    assert (real.lines_added, real.lines_removed) == (0, 1)
    assert lines(world) == {}
    assert coverage_report(world.engine, SEASON) == projected


def test_a_quote_naming_a_player_of_another_game_stays_unresolved_and_is_counted(tmp_path):
    world = build_world(tmp_path)
    seed_game(
        world,
        DEN_LAL,
        DEN_LAL_EVENT,
        [("draftkings", "Austin Reaves", 14.5), ("draftkings", "Kawhi Leonard", 24.5)],
        lines=[(REAVES, "draftkings", 14.5)],
        unresolved=1,
    )

    summary = world.re_resolve()

    # Kawhi Leonard is on the Clippers, who are not in this game.
    assert lines(world) == {(DEN_LAL, REAVES): ("draftkings", 14.5)}
    assert dict(summary.still_unresolved) == {"Kawhi Leonard": 1}
    assert summary.changed == 0
