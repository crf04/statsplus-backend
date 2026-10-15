"""Store and read sportsbook player-prop quotes (#377).

``SportsbookQuoteStore`` writes one poll atomically: a history row only when a
book's line or price changed (plus every closing snapshot), and the
``sportsbook_prop_latest`` rows the Matchup serves.  ``SportsbookQuoteReader``
turns the latest rows of one game into the ``book_lines`` response field,
including the best-price flags.  Nothing here touches the DFS projection
archive.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.domain.utc import assume_utc
from app.models.sportsbook_odds import (
    OddsApiPoll,
    SportsbookPropLatest,
    SportsbookPropQuote,
)
from app.providers.odds_api import BOOK_ALLOWLIST, MARKET_CATEGORIES
from app.services.request_reads import read_connection

#: The categories ``book_lines`` may carry, in contract order.
BOOK_LINE_CATEGORIES = tuple(MARKET_CATEGORIES.values())


@dataclass(frozen=True, slots=True)
class ResolvedQuote:
    """One book's quote for a canonical player, ready to store."""

    canonical_player_id: int
    category: str
    book: str
    line: float
    over: int | None
    under: int | None
    last_update: datetime | None


@dataclass(frozen=True, slots=True)
class PollWrite:
    """What one stored poll changed."""

    poll_id: int
    history_rows: int
    latest_rows: int
    removed_rows: int


class SportsbookQuoteStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record_poll(
        self,
        *,
        odds_event_id: str,
        game_id: str,
        started_at: datetime,
        completed_at: datetime,
        cost: int | None,
        remaining: int | None,
        is_closing: bool,
        quotes: Sequence[ResolvedQuote],
        unresolved_count: int = 0,
        outcome: str = "complete",
        commence_time: datetime | None = None,
        replace_latest: bool = True,
        fence: Callable[[Connection], None] | None = None,
    ) -> PollWrite:
        """Store one successful response in a single transaction.

        A repeated identical quote adds no history row but still refreshes
        its latest row.  Latest rows the response no longer carries are
        removed (section 4's latest-poll omission rule) -- including when
        every quote was omitted -- unless ``replace_latest`` is false, which
        a caller uses for an upstream-empty response that only says props are
        not posted yet.  ``fence`` runs first inside the transaction and
        raises when the caller's lease is gone, so a stale worker commits
        nothing.
        """

        observed_at = assume_utc(completed_at)
        with self.engine.begin() as connection:
            if fence is not None:
                fence(connection)
            poll_id = connection.execute(
                insert(OddsApiPoll.__table__).values(
                    odds_event_id=odds_event_id,
                    canonical_game_id=game_id,
                    commence_time=None if commence_time is None else assume_utc(commence_time),
                    started_at=assume_utc(started_at),
                    completed_at=observed_at,
                    outcome=outcome,
                    cost=cost,
                    remaining=remaining,
                    is_closing=bool(is_closing),
                    quote_count=len(quotes),
                    unresolved_count=unresolved_count,
                )
            ).inserted_primary_key[0]
            if not quotes and not replace_latest:
                return PollWrite(int(poll_id), 0, 0, 0)

            latest = SportsbookPropLatest.__table__
            history = SportsbookPropQuote.__table__
            existing = {
                (row.canonical_player_id, row.market_category, row.book): row
                for row in connection.execute(
                    select(latest).where(latest.c.canonical_game_id == game_id)
                )
            }
            history_rows: list[dict[str, Any]] = []
            seen: set[tuple[int, str, str]] = set()
            latest_writes = 0
            for quote in quotes:
                key = (quote.canonical_player_id, quote.category, quote.book)
                seen.add(key)
                values = {
                    "poll_id": int(poll_id),
                    "line": quote.line,
                    "over_price": quote.over,
                    "under_price": quote.under,
                    "market_last_update": quote.last_update,
                    "observed_at": observed_at,
                    "is_closing": bool(is_closing),
                }
                previous = existing.get(key)
                changed = previous is None or (
                    float(previous.line),
                    previous.over_price,
                    previous.under_price,
                ) != (quote.line, quote.over, quote.under)
                if changed or is_closing:
                    history_rows.append(
                        {
                            **values,
                            "canonical_game_id": game_id,
                            "canonical_player_id": quote.canonical_player_id,
                            "market_category": quote.category,
                            "book": quote.book,
                        }
                    )
                if previous is None:
                    connection.execute(
                        insert(latest).values(
                            canonical_game_id=game_id,
                            canonical_player_id=quote.canonical_player_id,
                            market_category=quote.category,
                            book=quote.book,
                            **values,
                        )
                    )
                else:
                    connection.execute(
                        update(latest)
                        .where(
                            latest.c.canonical_game_id == game_id,
                            latest.c.canonical_player_id == quote.canonical_player_id,
                            latest.c.market_category == quote.category,
                            latest.c.book == quote.book,
                        )
                        .values(**values)
                    )
                latest_writes += 1
            if history_rows:
                connection.execute(insert(history), history_rows)
            stale = [key for key in existing if key not in seen]
            for player_id, category, book in stale:
                connection.execute(
                    delete(latest).where(
                        latest.c.canonical_game_id == game_id,
                        latest.c.canonical_player_id == player_id,
                        latest.c.market_category == category,
                        latest.c.book == book,
                    )
                )
            return PollWrite(
                int(poll_id), len(history_rows), latest_writes, len(stale)
            )

    def record_unstored_poll(
        self,
        *,
        odds_event_id: str,
        game_id: str,
        started_at: datetime,
        completed_at: datetime | None,
        outcome: str,
        is_closing: bool,
        cost: int | None = None,
        remaining: int | None = None,
        failure_reason: str | None = None,
        unresolved_count: int = 0,
        commence_time: datetime | None = None,
        fence: Callable[[Connection], None] | None = None,
    ) -> None:
        """Record an ``empty`` or ``failed`` poll that stored no quotes."""

        with self.engine.begin() as connection:
            if fence is not None:
                fence(connection)
            connection.execute(
                insert(OddsApiPoll.__table__).values(
                    odds_event_id=odds_event_id,
                    canonical_game_id=game_id,
                    commence_time=None if commence_time is None else assume_utc(commence_time),
                    started_at=assume_utc(started_at),
                    completed_at=None if completed_at is None else assume_utc(completed_at),
                    outcome=outcome,
                    cost=cost,
                    remaining=remaining,
                    is_closing=bool(is_closing),
                    failure_reason=failure_reason,
                    quote_count=0,
                    unresolved_count=unresolved_count,
                )
            )


def _decimal_odds(price: int) -> float:
    """Payout multiple of an American price, so +100 and -100 compare equal."""

    return 1 + price / 100 if price > 0 else 1 + 100 / -price


def _flag_best(prices: dict[str, int | None]) -> set[str]:
    """Books holding the best price among at least two quoting books."""

    quoted = {book: price for book, price in prices.items() if price is not None}
    if len(quoted) < 2:
        return set()
    best = max(_decimal_odds(price) for price in quoted.values())
    return {book for book, price in quoted.items() if _decimal_odds(price) == best}


def _iso_z(value: datetime) -> str:
    return assume_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_market_entry(rows: Iterable[Any]) -> dict[str, Any] | None:
    """One ``book_lines`` category entry from the latest rows of one market.

    ``best_over``/``best_under`` are decided only among books that post the
    same line, higher American price wins, ties all flag, and a book alone at
    its line (or alone on that side) is never flagged.
    """

    rows = [row for row in rows if row.book in BOOK_ALLOWLIST]
    if not rows:
        return None
    rows.sort(key=lambda row: (float(row.line), row.book))
    best_over: set[tuple[float, str]] = set()
    best_under: set[tuple[float, str]] = set()
    for line in {float(row.line) for row in rows}:
        same_line = [row for row in rows if float(row.line) == line]
        best_over |= {
            (line, book)
            for book in _flag_best({row.book: row.over_price for row in same_line})
        }
        best_under |= {
            (line, book)
            for book in _flag_best({row.book: row.under_price for row in same_line})
        }
    updated = [row.market_last_update or row.observed_at for row in rows]
    return {
        "as_of": _iso_z(max(assume_utc(value) for value in updated)),
        "closing": all(bool(row.is_closing) for row in rows),
        "books": [
            {
                "book": row.book,
                "line": float(row.line),
                "over": row.over_price,
                "under": row.under_price,
                "updated_at": _iso_z(row.market_last_update or row.observed_at),
                "best_over": (float(row.line), row.book) in best_over,
                "best_under": (float(row.line), row.book) in best_under,
            }
            for row in rows
        ],
    }


class SportsbookQuoteReader:
    """Read the ``book_lines`` of one game from the latest rows."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def book_lines(
        self, game_id: str, *, connection: Connection | None = None
    ) -> dict[int, dict[str, dict[str, Any]]]:
        """``{canonical_player_id: {category: entry}}`` for one game."""

        latest = SportsbookPropLatest.__table__
        with read_connection(self.engine, connection) as active:
            rows = active.execute(
                select(latest).where(
                    latest.c.canonical_game_id == game_id,
                    latest.c.book.in_(sorted(BOOK_ALLOWLIST)),
                    latest.c.market_category.in_(BOOK_LINE_CATEGORIES),
                )
            ).all()
        grouped: dict[tuple[int, str], list[Any]] = {}
        for row in rows:
            grouped.setdefault((row.canonical_player_id, row.market_category), []).append(row)
        result: dict[int, dict[str, dict[str, Any]]] = {}
        for (player_id, category), group in sorted(grouped.items()):
            entry = build_market_entry(group)
            if entry is not None:
                result.setdefault(player_id, {})[category] = entry
        return result


__all__ = [
    "BOOK_LINE_CATEGORIES",
    "PollWrite",
    "ResolvedQuote",
    "SportsbookQuoteReader",
    "SportsbookQuoteStore",
    "build_market_entry",
]
