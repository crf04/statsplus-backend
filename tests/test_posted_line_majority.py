"""``posted_line.lines`` takes the majority line across providers and books
(crf04/statsplus#274).

Called at the ``PostedLinesReader`` seam with a stub event catalog, player pool
and sportsbook reader; every expected value is a literal.
"""

from __future__ import annotations

from app.services.posted_lines import LineSource, line_sources
from tests.test_posted_line_prices import PLAYER, Books, Closing, Pools, _pool, _read


def _books(*lines):
    return {"books": [{"book": book, "line": line, "over": -110, "under": -110} for book, line in lines]}


def test_one_outlier_never_beats_the_line_seven_sources_post():
    pool = _pool({"dabble": {"PTS": 30.5}, "underdog": {"PTS": 29.5}})
    books = _books(
        ("betmgm", 29.5),
        ("draftkings", 29.5),
        ("fanatics", 29.5),
        ("fanduel", 29.5),
        ("williamhill_us", 29.5),
        ("betrivers", 29.5),
    )

    result = _read(Pools(pool), Books({PLAYER: {"PTS": books}}))

    assert result["lines"] == {"PTS": {"value": 29.5, "provider": "underdog"}}


def test_a_tie_goes_to_the_line_of_the_first_source_prizepicks_first():
    pool = _pool({"dabble": {"PTS": 29.5}, "prizepicks": {"PTS": 28.5}})
    books = _books(("draftkings", 29.5), ("fanduel", 28.5))

    result = _read(Pools(pool), Books({PLAYER: {"PTS": books}}))

    assert result["lines"] == {"PTS": {"value": 28.5, "provider": "prizepicks"}}


def test_a_majority_no_provider_posts_is_named_by_the_first_book_in_served_order():
    pool = _pool({"dabble": {"PTS": 30.5, "REB": 7.5}})
    books = _books(("fanduel", 29.5), ("draftkings", 29.5))

    result = _read(Pools(pool), Books({PLAYER: {"PTS": books}}))

    assert result["lines"] == {
        "PTS": {"value": 29.5, "provider": "fanduel"},
        "REB": {"value": 7.5, "provider": "dabble"},
    }


def test_without_book_lines_the_majority_among_providers_wins():
    pool = _pool({"dabble": {"PTS": 30.5}, "novig": {"PTS": 29.5}, "underdog": {"PTS": 29.5}})

    result = _read(Pools(pool))

    assert result["lines"] == {"PTS": {"value": 29.5, "provider": "novig"}}


def test_the_closing_line_fallback_ignores_the_book_majority():
    books = _books(("fanduel", 27.5), ("betmgm", 27.5))

    result = _read(Pools(None), Books({PLAYER: {"PTS": books}}), Closing())

    assert result["lines"] == {"PTS": {"value": 24.5, "provider": "draftkings"}}


def test_line_sources_lists_providers_then_books_with_their_prices():
    sources = line_sources(
        "PTS",
        {"underdog": {"PTS": 29.5}, "dabble": {"PTS": 30.5, "REB": 7.5}, "prizepicks": {"REB": 7.5}},
        {"underdog": {"PTS": {"over": -112, "under": None}}},
        {"PTS": {"books": [{"book": "fanduel", "line": 29.5, "over": -105, "under": -115}]}},
    )

    assert sources == [
        LineSource("provider", "dabble", 30.5),
        LineSource("provider", "underdog", 29.5, -112, None),
        LineSource("book", "fanduel", 29.5, -105, -115),
    ]
