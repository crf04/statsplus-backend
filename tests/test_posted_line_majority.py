"""``posted_line.lines`` takes the majority line across providers and books
(crf04/statsplus#274).

Called at the ``PostedLinesReader`` seam with a stub event catalog, player pool
and sportsbook reader; every expected value is a literal.
"""

from __future__ import annotations

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


def test_prizepicks_posting_a_minority_line_loses_to_the_majority():
    pool = _pool({"prizepicks": {"PTS": 30.5}, "dabble": {"PTS": 29.5}, "underdog": {"PTS": 29.5}})
    books = _books(("draftkings", 29.5))

    result = _read(Pools(pool), Books({PLAYER: {"PTS": books}}))

    assert result["lines"] == {"PTS": {"value": 29.5, "provider": "dabble"}}


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
