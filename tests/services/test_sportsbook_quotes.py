"""Best-price rules, change-only storage, and the book_lines read (#377)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, insert, select

from app.migrations import run_migrations
from app.models.sportsbook_odds import SportsbookPropLatest, SportsbookPropQuote
from app.services.sportsbook_quotes import (
    ResolvedQuote,
    SportsbookQuoteReader,
    SportsbookQuoteStore,
    build_market_entry,
)

T0 = datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)
GAME = "0022600001"


def row(book, line, over, under, *, updated="2026-10-21T21:02:40Z", closing=False):
    stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    return SimpleNamespace(
        book=book, line=line, over_price=over, under_price=under,
        market_last_update=stamp, observed_at=stamp, is_closing=closing,
    )


def flags(entry):
    return [(b["book"], b["best_over"], b["best_under"]) for b in entry["books"]]


# -- best price among equal lines only ---------------------------------------


def test_higher_price_wins_across_the_negative_and_positive_ranges():
    entry = build_market_entry(
        [
            row("draftkings", 27.5, -115, -105),
            row("fanduel", 27.5, -110, -110),
            row("betmgm", 27.5, 120, -125),
            row("betrivers", 27.5, -105, 105),
        ]
    )

    # +120 beats -105, -110, -115 on the Over; +105 beats -105, -110, -125.
    assert flags(entry) == [
        ("betmgm", True, False),
        ("betrivers", False, True),
        ("draftkings", False, False),
        ("fanduel", False, False),
    ]


def test_minus_105_beats_minus_115():
    entry = build_market_entry([row("draftkings", 27.5, -115, -115), row("fanduel", 27.5, -105, -105)])

    assert flags(entry) == [("draftkings", False, False), ("fanduel", True, True)]


def test_ties_all_flag():
    entry = build_market_entry(
        [
            row("draftkings", 27.5, -110, -105),
            row("fanduel", 27.5, -110, -110),
            row("betmgm", 27.5, -115, -105),
        ]
    )

    assert flags(entry) == [
        ("betmgm", False, True),
        ("draftkings", True, True),
        ("fanduel", True, False),
    ]


def test_even_money_written_as_plus_or_minus_100_is_a_tie():
    entry = build_market_entry([row("draftkings", 27.5, 100, -105), row("fanduel", 27.5, -100, -110)])

    assert flags(entry) == [("draftkings", True, True), ("fanduel", True, False)]


def test_books_at_different_lines_are_never_compared():
    entry = build_market_entry(
        [
            row("draftkings", 26.5, 140, -170),
            row("fanduel", 27.5, -140, 110),
            row("betmgm", 27.5, -125, -105),
        ]
    )

    # draftkings is alone at 26.5: despite the biggest Over price, no flag.
    assert [(b["book"], b["line"]) for b in entry["books"]] == [
        ("draftkings", 26.5), ("betmgm", 27.5), ("fanduel", 27.5),
    ]
    assert flags(entry) == [
        ("draftkings", False, False),
        ("betmgm", True, False),
        ("fanduel", False, True),
    ]


def test_a_book_alone_at_its_line_is_never_best():
    entry = build_market_entry([row("draftkings", 27.5, -110, -110)])

    assert flags(entry) == [("draftkings", False, False)]


def test_a_missing_side_is_none_and_cannot_win_or_be_compared_alone():
    entry = build_market_entry(
        [
            row("williamhill_us", 27.5, 105, None),
            row("draftkings", 27.5, -115, -105),
            row("fanduel", 27.5, None, -110),
        ]
    )

    assert [(b["book"], b["over"], b["under"]) for b in entry["books"]] == [
        ("draftkings", -115, -105), ("fanduel", None, -110), ("williamhill_us", 105, None),
    ]
    # Over: +105 beats -115 among the two that post it.  Under: -105 beats -110.
    assert flags(entry) == [
        ("draftkings", False, True),
        ("fanduel", False, False),
        ("williamhill_us", True, False),
    ]


def test_entry_metadata_is_sorted_stamped_and_closing_only_when_all_closing():
    live = build_market_entry(
        [
            row("fanduel", 27.5, -110, -110, updated="2026-10-21T21:03:02Z"),
            row("draftkings", 27.5, -115, -105, updated="2026-10-21T21:02:40Z", closing=True),
        ]
    )
    closed = build_market_entry(
        [row("fanduel", 27.5, -110, -110, closing=True), row("draftkings", 27.5, -115, -105, closing=True)]
    )

    assert live == {
        "as_of": "2026-10-21T21:03:02Z",
        "closing": False,
        "books": [
            {"book": "draftkings", "line": 27.5, "over": -115, "under": -105,
             "updated_at": "2026-10-21T21:02:40Z", "best_over": False, "best_under": True},
            {"book": "fanduel", "line": 27.5, "over": -110, "under": -110,
             "updated_at": "2026-10-21T21:03:02Z", "best_over": True, "best_under": False},
        ],
    }
    assert closed["closing"] is True


def test_offshore_rows_are_never_served_even_if_stored():
    entry = build_market_entry(
        [row("bovada", 27.5, 150, 130), row("draftkings", 27.5, -115, -105)]
    )

    assert [b["book"] for b in entry["books"]] == ["draftkings"]
    assert build_market_entry([row("bovada", 27.5, 150, 130)]) is None


# -- change-only storage ------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'quotes.sqlite3'}")
    run_migrations(engine)
    return SimpleNamespace(
        engine=engine, writer=SportsbookQuoteStore(engine), reader=SportsbookQuoteReader(engine)
    )


def q(player, category, book, line, over, under, stamp="2026-10-21T21:00:00Z"):
    return ResolvedQuote(player, category, book, line, over, under,
                         datetime.fromisoformat(stamp.replace("Z", "+00:00")))


def poll(store, quotes, *, at=T0, closing=False, event="evt"):
    return store.writer.record_poll(
        odds_event_id=event, game_id=GAME, started_at=at, completed_at=at,
        cost=5, remaining=19990, is_closing=closing, quotes=quotes,
    )


def count(store, model):
    with store.engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(model.__table__)).scalar_one()


BASE = [
    q(2544, "PTS", "draftkings", 27.5, -115, -105),
    q(2544, "PTS", "fanduel", 27.5, -110, -110),
    q(2544, "REB", "draftkings", 7.5, -105, -115),
]


def test_first_poll_writes_every_quote_to_history_and_latest(store):
    write = poll(store, BASE)

    assert (write.history_rows, write.latest_rows, write.removed_rows) == (3, 3, 0)
    assert (count(store, SportsbookPropQuote), count(store, SportsbookPropLatest)) == (3, 3)


def test_an_identical_poll_writes_no_history_but_still_refreshes_latest(store):
    poll(store, BASE, at=T0)

    write = poll(store, BASE, at=T0 + timedelta(hours=1))

    assert (write.history_rows, write.latest_rows) == (0, 3)
    assert count(store, SportsbookPropQuote) == 3
    with store.engine.connect() as connection:
        observed = {r.observed_at.replace(tzinfo=timezone.utc) for r in connection.execute(select(SportsbookPropLatest.__table__))}
    assert observed == {T0 + timedelta(hours=1)}


def test_one_changed_price_writes_exactly_one_history_row(store):
    poll(store, BASE, at=T0)
    moved = [q(2544, "PTS", "draftkings", 27.5, -120, -100)] + BASE[1:]

    write = poll(store, moved, at=T0 + timedelta(hours=1))

    assert write.history_rows == 1
    with store.engine.connect() as connection:
        series = connection.execute(
            select(SportsbookPropQuote.__table__.c.over_price, SportsbookPropQuote.__table__.c.under_price)
            .where(SportsbookPropQuote.__table__.c.book == "draftkings",
                   SportsbookPropQuote.__table__.c.market_category == "PTS")
            .order_by(SportsbookPropQuote.__table__.c.quote_id)
        ).all()
    assert series == [(-115, -105), (-120, -100)]
    assert store.reader.book_lines(GAME)[2544]["PTS"]["books"][0]["over"] == -120


def test_a_line_move_with_the_same_prices_is_a_change(store):
    poll(store, BASE, at=T0)
    moved = [q(2544, "PTS", "draftkings", 28.5, -115, -105)] + BASE[1:]

    assert poll(store, moved, at=T0 + timedelta(hours=1)).history_rows == 1


def test_a_closing_snapshot_is_always_written_even_when_nothing_changed(store):
    poll(store, BASE, at=T0)

    write = poll(store, BASE, at=T0 + timedelta(hours=1), closing=True)

    assert write.history_rows == 3
    with store.engine.connect() as connection:
        closing_rows = connection.execute(
            select(func.count()).select_from(SportsbookPropQuote.__table__)
            .where(SportsbookPropQuote.__table__.c.is_closing.is_(True))
        ).scalar_one()
    assert closing_rows == 3
    entry = store.reader.book_lines(GAME)[2544]["PTS"]
    assert entry["closing"] is True


def test_quotes_a_later_response_no_longer_carries_are_removed_from_latest(store):
    poll(store, BASE, at=T0)

    write = poll(store, BASE[:1], at=T0 + timedelta(hours=1))

    assert write.removed_rows == 2
    assert set(store.reader.book_lines(GAME)[2544]) == {"PTS"}
    assert [b["book"] for b in store.reader.book_lines(GAME)[2544]["PTS"]["books"]] == ["draftkings"]
    assert count(store, SportsbookPropQuote) == 3  # history is kept


def test_a_response_without_quotes_leaves_latest_untouched(store):
    poll(store, BASE, at=T0)

    write = poll(store, [], at=T0 + timedelta(hours=1))

    assert (write.history_rows, write.latest_rows, write.removed_rows) == (0, 0, 0)
    assert count(store, SportsbookPropLatest) == 3


def test_reader_returns_only_that_games_players_and_empty_for_unknown_games(store):
    poll(store, BASE)

    assert set(store.reader.book_lines(GAME)) == {2544}
    assert store.reader.book_lines("0022600999") == {}


def test_reader_drops_a_stored_offshore_row_defensively(store):
    poll(store, BASE)
    with store.engine.begin() as connection:
        connection.execute(
            insert(SportsbookPropLatest.__table__).values(
                canonical_game_id=GAME, canonical_player_id=2544, market_category="PTS",
                book="bovada", poll_id=1, line=27.5, over_price=900, under_price=-900,
                observed_at=T0, is_closing=False,
            )
        )

    books = [b["book"] for b in store.reader.book_lines(GAME)[2544]["PTS"]["books"]]

    assert books == ["draftkings", "fanduel"]
