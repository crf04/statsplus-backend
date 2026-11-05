"""Closing quotes are kept for good (crf04/statsplus#429).

``sportsbook_prop_quotes`` rows with ``is_closing`` are the 2026-27 closing
lines the game logs serve.  The store only ever inserts history rows: later
polls of the game, a poll that drops a book, an empty poll and a postponement's
second close leave a closing row exactly as it was written.  No retention job
touches the table; the second test fails when new code starts writing it, so
that code is checked for this rule.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.sportsbook_odds import SportsbookPropQuote
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteStore

GAME = "0022600101"
TIP = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _quote(player, line, observed):
    return ResolvedQuote(player, "PTS", "draftkings", line, -110, -110, observed)


def _closing_rows(engine):
    table = SportsbookPropQuote.__table__
    with engine.connect() as connection:
        return connection.execute(
            select(table).where(table.c.is_closing.is_(True)).order_by(table.c.quote_id)
        ).all()


def test_later_polls_never_delete_or_rewrite_a_closing_quote(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'odds.db'}")
    run_migrations(engine)
    store = SportsbookQuoteStore(engine)

    def poll(observed, quotes, *, is_closing, replace_latest=True):
        store.record_poll(
            odds_event_id="event",
            game_id=GAME,
            started_at=observed,
            completed_at=observed,
            cost=5,
            remaining=1000,
            is_closing=is_closing,
            quotes=quotes,
            commence_time=TIP,
            replace_latest=replace_latest,
        )

    close = TIP - timedelta(minutes=7)
    poll(close, [_quote(2544, 24.5, close), _quote(203999, 28.5, close)], is_closing=True)
    kept = _closing_rows(engine)
    assert [(row.canonical_player_id, row.line) for row in kept] == [(2544, 24.5), (203999, 28.5)]

    later = TIP + timedelta(days=1)
    # A changed line, a dropped player, an empty answer, and a postponement's
    # second closing poll.
    poll(later, [_quote(2544, 26.5, later)], is_closing=False)
    poll(later + timedelta(hours=1), [], is_closing=False)
    poll(later + timedelta(hours=2), [], is_closing=False, replace_latest=False)
    poll(later + timedelta(hours=3), [_quote(2544, 27.5, later)], is_closing=True)

    assert _closing_rows(engine)[:2] == kept


#: The only code that may write ``sportsbook_prop_quotes``, and what it does:
#: the store inserts history rows and never updates or deletes them; the
#: migration creates the table and its reversal drops it.
WRITERS = {
    "app/services/sportsbook_quotes.py",
    "app/migrations.py",
}
#: Code that names the table only to declare it, read it, or write it through
#: ``SportsbookQuoteStore`` (the 2026-27 gap backfill).
READERS = {
    "app/models/sportsbook_odds.py",
    "app/services/closing_lines.py",
    "app/services/closing_line_backfill.py",
    "scripts/backfill_closing_lines.py",
}


def test_no_other_code_names_the_closing_quote_table():
    """A new writer of the table must be checked for the keep-forever rule first."""

    pattern = re.compile(r"\bSportsbookPropQuote\b|\bsportsbook_prop_quotes\b")
    naming = {
        str(path.relative_to(ROOT))
        for folder in ("app", "scripts")
        for path in (ROOT / folder).rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }

    assert naming == WRITERS | READERS
