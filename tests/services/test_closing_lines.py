"""Book order, line choice and the game-log read of stored closing lines (#433)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.closing_lines import ClosingLine
from app.providers.odds_api import BOOK_ALLOWLIST
from app.services.closing_lines import (
    BOOK_ORDER,
    ClosingLineReader,
    PostedLine,
    choose_closing_lines,
    decode_posted_lines,
    encode_posted_lines,
)

WHEN = datetime(2025, 11, 5, 0, 24, 41, tzinfo=UTC)


def test_the_book_order_is_draftkings_then_fanduel_then_the_other_regulated_books():
    assert BOOK_ORDER == (
        "draftkings",
        "fanduel",
        "betmgm",
        "williamhill_us",
        "betrivers",
        "fanatics",
    )
    assert set(BOOK_ORDER) == BOOK_ALLOWLIST  # no offshore book can ever rank


def test_a_players_line_comes_from_the_first_book_of_the_order_that_posted_him():
    posted = [
        PostedLine("fanatics", "A", 10.5),
        PostedLine("betrivers", "A", 11.5),
        PostedLine("fanduel", "A", 12.5),
        PostedLine("betrivers", "B", 20.5),
        PostedLine("fanatics", "B", 21.5),
        PostedLine("fanatics", "C", 5.5),
        PostedLine("draftkings", "D", 30.5),
        PostedLine("fanduel", "D", 31.5),
    ]

    chosen = choose_closing_lines(posted, {"A": 1, "B": 2, "C": 3, "D": 4})

    assert chosen == {
        1: ("fanduel", 12.5),
        2: ("betrivers", 20.5),
        3: ("fanatics", 5.5),
        4: ("draftkings", 30.5),
    }


def test_the_order_does_not_depend_on_the_order_the_quotes_arrive_in():
    posted = [PostedLine("fanduel", "A", 12.5), PostedLine("draftkings", "A", 11.5)]

    assert (
        choose_closing_lines(posted, {"A": 1})
        == choose_closing_lines(list(reversed(posted)), {"A": 1})
        == {1: ("draftkings", 11.5)}
    )


def test_a_book_with_two_lines_for_one_player_is_skipped_for_him():
    posted = [
        PostedLine("draftkings", "Nikola Jokić", 28.5),
        PostedLine("draftkings", "Nikola Jokic", 29.5),  # two spellings, one player
        PostedLine("fanduel", "Nikola Jokic", 29.5),
    ]

    chosen = choose_closing_lines(posted, {"Nikola Jokić": 7, "Nikola Jokic": 7})

    assert chosen == {7: ("fanduel", 29.5)}


def test_two_spellings_of_one_player_that_agree_are_one_line():
    posted = [PostedLine("draftkings", "Nikola Jokić", 28.5), PostedLine("draftkings", "Nikola Jokic", 28.5)]

    assert choose_closing_lines(posted, {"Nikola Jokić": 7, "Nikola Jokic": 7}) == {7: ("draftkings", 28.5)}


def test_a_name_that_resolved_to_no_player_has_no_line():
    assert choose_closing_lines([PostedLine("draftkings", "Zach Unknown", 8.5)], {}) == {}


def test_quotes_round_trip_through_the_ledger_text():
    lines = (PostedLine("draftkings", "Nikola Jokić", 28.5), PostedLine("fanduel", "A B", 1.0))

    assert decode_posted_lines(encode_posted_lines(lines)) == lines
    assert decode_posted_lines(None) == () and decode_posted_lines("") == ()


def _reader(tmp_path, rows):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(ClosingLine.__table__),
            [
                dict(
                    canonical_game_id=game,
                    canonical_player_id=player,
                    market_category=market,
                    book=book,
                    line=line,
                    snapshot_at=WHEN,
                    odds_event_id="e",
                    bought_at=WHEN,
                )
                for game, player, market, book, line in rows
            ],
        )
    return ClosingLineReader(engine), engine


def test_the_reader_returns_each_games_line_for_the_player_and_prefers_the_better_book(tmp_path):
    reader, _ = _reader(
        tmp_path,
        [
            ("g1", 2544, "PTS", "fanduel", 25.5),
            ("g1", 2544, "PTS", "draftkings", 24.5),
            ("g2", 2544, "PTS", "betmgm", 23.5),
            ("g3", 2544, "REB", "draftkings", 7.5),  # another market
            ("g1", 99, "PTS", "draftkings", 1.5),  # another player
            ("g9", 2544, "PTS", "draftkings", 30.5),  # not asked for
        ],
    )

    found = reader.for_player(2544, ["g1", "g2", "g3", "g4"])

    assert {game: (line.value, line.book) for game, line in found.items()} == {
        "g1": (24.5, "draftkings"),
        "g2": (23.5, "betmgm"),
    }
    assert found["g1"].as_response() == {"value": 24.5, "book": "draftkings"}


def test_the_reader_reads_nothing_for_no_games(tmp_path):
    reader, _ = _reader(tmp_path, [("g1", 2544, "PTS", "draftkings", 24.5)])

    assert reader.for_player(2544, []) == {}


def test_a_storage_fault_reads_as_no_lines_instead_of_failing_the_game_logs(tmp_path, caplog):
    reader, engine = _reader(tmp_path, [("g1", 2544, "PTS", "draftkings", 24.5)])
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE closing_lines"))

    assert reader.for_player(2544, ["g1"]) == {}
    assert "Could not read closing lines" in caplog.text
