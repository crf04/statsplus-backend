"""The book history read covers only the requested cells (crf04/statsplus#491)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.line_moves import book_histories
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteStore

GAME = "0022500602"
#: Earlier games of the season: none of their rows is a requested cell.
EARLIER = [f"00225{number:05d}" for number in range(100, 160)]
TATUM = 1628369
START = datetime(2026, 1, 15, 3, tzinfo=UTC)


class _CountingConnection:
    """Counts the rows each statement returns."""

    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, statement):
        result = self.connection.execute(statement).all()
        self.rows.append(len(result))
        return _Rows(result)


class _Rows(list):
    def all(self):
        return list(self)


def _poll(store, game_id, at, quotes):
    store.record_poll(
        odds_event_id=f"odds-{game_id}",
        game_id=game_id,
        started_at=at,
        completed_at=at,
        cost=1,
        remaining=100,
        is_closing=False,
        quotes=quotes,
    )


def _read(engine):
    with engine.connect() as connection:
        counting = _CountingConnection(connection)
        histories = book_histories(
            counting, game_ids=[GAME], market="PTS", books=["draftkings", "fanduel"], posted=()
        )
    return histories, counting.rows


def test_the_read_returns_only_the_requested_games_market_and_books(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'books.sqlite3'}")
    run_migrations(engine)
    store = SportsbookQuoteStore(engine)
    for step, line in enumerate((27.5, 28.5)):
        at = START + timedelta(hours=step)
        _poll(
            store,
            GAME,
            at,
            [
                ResolvedQuote(TATUM, "PTS", "draftkings", line, -110, -110, at),
                # Another market and a book not asked for: neither is read.
                ResolvedQuote(TATUM, "3PM", "draftkings", 3.5 + step, -120, 100, at),
                ResolvedQuote(TATUM, "PTS", "betmgm", line, -115, -105, at),
            ],
        )
    histories, rows = _read(engine)

    # A season of other games' lines, every one of them a change.
    for index, game_id in enumerate(EARLIER):
        for step in range(5):
            at = START - timedelta(days=index + 1, hours=step)
            _poll(
                store, game_id, at, [ResolvedQuote(TATUM, "PTS", "draftkings", 20.5 + step, -110, -110, at)]
            )
    after, rows_after = _read(engine)

    assert after == histories
    assert [sighting.value for sighting in histories[("draftkings", GAME, TATUM)].sightings] == [
        (27.5, -110, -110),
        (28.5, -110, -110),
    ]
    assert (rows, rows_after) == ([2], [2])
