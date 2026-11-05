"""Closing sportsbook lines: the book order, the store, and the game-log read (#433).

The backfill (``closing_line_backfill``) buys each game's closing
``player_points`` snapshot once and records the purchase here before anything
is parsed.  This module owns what is written and read:

* ``choose_closing_lines`` picks one line per player by a fixed book order;
* ``ClosingLineStore`` is the ledger and the line table the backfill writes;
* ``ClosingLineReader`` serves ``/api/games/game_logs`` its ``closing_line`` and
  ``closing_lines`` (crf04/statsplus#429): from 2026-27 on, the live collector's
  closing quotes in ``sportsbook_prop_quotes``; before, the ``closing_lines``
  table;
* ``closing_quote_gaps`` names the started games with no closing quote stored;
* ``coverage_report`` measures how many players who played 20+ minutes have a
  line, by source book.

``ClosingLineStore.recorded_games`` reads back every game whose quotes are on the
ledger, so the lines can be written again after a resolver fix without a call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_events import is_started_event, player_game_log_season_type
from app.domain.utc import assume_utc
from app.models.closing_lines import ClosingLine, ClosingLinePurchase, ClosingLineSpend
from app.models.player_game_log import PlayerGameLog
from app.models.sportsbook_odds import SportsbookPropQuote
from app.providers.odds_api import BOOK_ALLOWLIST, MARKET_CATEGORIES, ParsedEventOdds

logger = logging.getLogger(__name__)

#: The only market the backfill buys: the standard points market (not
#: ``player_points_alternate``) and its Matchup category.
ODDS_MARKET = "player_points"
MARKET_CATEGORY = MARKET_CATEGORIES[ODDS_MARKET]

#: The ledger key (``closing_line_purchases.market_category``) of a purchase of
#: all five standard markets in one call: the 2026-27 gap backfill
#: (crf04/statsplus#429), whose quotes become closing quotes in
#: ``sportsbook_prop_quotes`` rather than rows of ``closing_lines``.
ALL_MARKETS = "ALL"
#: The categories each ledger key's purchase keeps.
LEDGER_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    MARKET_CATEGORY: (MARKET_CATEGORY,),
    ALL_MARKETS: tuple(MARKET_CATEGORIES.values()),
}

#: The first season served from the live collector's closing quotes
#: (``sportsbook_prop_quotes.is_closing``); earlier seasons read the
#: backfilled ``closing_lines`` table (crf04/statsplus#429).
LIVE_CLOSING_FIRST_SEASON = "2026-27"

#: Which book's line is kept when several posted the same player: DraftKings,
#: then FanDuel (crf04/statsplus#216), then the remaining regulated ``us``
#: books of ``BOOK_ALLOWLIST`` in this fixed order (roughly by US handle:
#: BetMGM, Caesars, BetRivers, Fanatics).  Offshore books are never requested
#: by name and are dropped by the allowlist before this order is consulted.
BOOK_ORDER: tuple[str, ...] = (
    "draftkings",
    "fanduel",
    "betmgm",
    "williamhill_us",
    "betrivers",
    "fanatics",
)
if set(BOOK_ORDER) != set(BOOK_ALLOWLIST) or len(BOOK_ORDER) != len(BOOK_ALLOWLIST):
    raise RuntimeError("BOOK_ORDER must rank exactly the books of BOOK_ALLOWLIST")

#: A player counts toward coverage with at least this many minutes.
COVERAGE_MINUTES = 20

#: Held for the duration of a backfill so two runs cannot buy the same game.
_BACKFILL_LOCK_ID = 0x434C4F53  # "CLOS"


class RunRefused(RuntimeError):
    """A paid run the store cannot make safe (no cross-process lock)."""


@dataclass(frozen=True, slots=True)
class PostedLine:
    """One book's posted line for a player name, as the response said it.

    A points purchase keeps only the book, name and line; a five-market
    purchase also keeps the category, prices and update time, so its quotes
    are stored exactly as a live closing poll stores them.
    """

    book: str
    player_name: str
    line: float
    category: str = MARKET_CATEGORY
    over: int | None = None
    under: int | None = None
    last_update: datetime | None = None


def posted_lines(
    parsed: ParsedEventOdds, categories: Sequence[str] = (MARKET_CATEGORY,)
) -> tuple[PostedLine, ...]:
    """The quotes of a parsed response in ``categories``, in a stable order."""

    return tuple(
        sorted(
            (
                PostedLine(
                    quote.book,
                    quote.player_name,
                    quote.line,
                    quote.category,
                    quote.over,
                    quote.under,
                    quote.last_update,
                )
                for quote in parsed.quotes
                if quote.category in categories
            ),
            key=lambda item: (item.category, item.book, item.player_name, item.line),
        )
    )


def encode_posted_lines(lines: Sequence[PostedLine], *, full: bool = False) -> str:
    """Compact JSON: ``[book, name, line]``, or with ``full`` every field."""

    if not full:
        return json.dumps([[item.book, item.player_name, item.line] for item in lines], separators=(",", ":"))
    return json.dumps(
        [
            [
                item.book,
                item.player_name,
                item.line,
                item.category,
                item.over,
                item.under,
                None if item.last_update is None else assume_utc(item.last_update).isoformat(),
            ]
            for item in lines
        ],
        separators=(",", ":"),
    )


def decode_posted_lines(text_value: str | None) -> tuple[PostedLine, ...]:
    if not text_value:
        return ()
    decoded = []
    for book, name, line, *rest in json.loads(text_value):
        if not rest:
            decoded.append(PostedLine(book, name, float(line)))
            continue
        category, over, under, updated = rest
        decoded.append(
            PostedLine(
                book,
                name,
                float(line),
                category,
                over,
                under,
                None if updated is None else assume_utc(datetime.fromisoformat(updated)),
            )
        )
    return tuple(decoded)


def choose_closing_lines(
    posted: Sequence[PostedLine], players: Mapping[str, int]
) -> dict[int, tuple[str, float]]:
    """``{player id: (book, line)}``: the first book of ``BOOK_ORDER`` that posted him.

    ``players`` maps a posted name to a canonical player id; a name absent from
    it has no line.  A book that posted two different lines for one player
    (two spellings that resolve to him, or two points) is skipped for that
    player rather than guessed at, the same rule the response parser applies.
    """

    by_player: dict[int, dict[str, set[float]]] = {}
    for item in posted:
        player_id = players.get(item.player_name)
        if player_id is not None:
            by_player.setdefault(player_id, {}).setdefault(item.book, set()).add(item.line)
    chosen: dict[int, tuple[str, float]] = {}
    for player_id, books in by_player.items():
        for book in BOOK_ORDER:
            lines = books.get(book)
            if lines and len(lines) == 1:
                chosen[player_id] = (book, next(iter(lines)))
                break
    return chosen


@dataclass(frozen=True, slots=True)
class StoredPurchase:
    """A recorded purchase whose lines are not yet written."""

    game_id: str
    odds_event_id: str
    snapshot_at: datetime
    bought_at: datetime
    posted: tuple[PostedLine, ...]


@dataclass(frozen=True, slots=True)
class RecordedGame:
    """A game whose quotes are on the ledger, and what is written for it."""

    purchase: StoredPurchase
    #: ``purchased`` (lines not yet written) or ``complete``.
    outcome: str
    unresolved: int
    #: ``(player id, book, line)`` of every stored line of the game.
    lines: frozenset[tuple[int, str, float]]


def _stored_purchase(row: Any) -> StoredPurchase:
    return StoredPurchase(
        game_id=row.canonical_game_id,
        odds_event_id=row.odds_event_id,
        snapshot_at=assume_utc(row.snapshot_at),
        bought_at=assume_utc(row.bought_at),
        posted=decode_posted_lines(row.quotes),
    )


class ClosingLineStore:
    """The purchase ledger, the spend log and the closing-line table.

    ``market`` is the ledger key the store reads and writes purchases under:
    ``PTS`` for the 2025-26 points backfill, ``ALL`` for a five-market purchase.
    The line table, ``complete`` and the re-resolve read are the points
    backfill's only.
    """

    def __init__(self, engine: Engine, *, lock_runs: bool = True, market: str = MARKET_CATEGORY) -> None:
        if market not in LEDGER_CATEGORIES:
            raise ValueError(f"unknown ledger market {market!r}")
        self.engine = engine
        self.market = market
        #: Only PostgreSQL can keep two processes from buying the same game.
        #: A test over SQLite, which has one process, opts out.
        self._lock_runs = lock_runs

    @contextmanager
    def exclusive_run(self) -> Iterator[bool]:
        """Yield whether this process holds the backfill lock for the run.

        PostgreSQL gets a session advisory lock on a connection kept open for
        the run; a second run sees ``False`` and must not buy.  Any other
        database cannot serialize runs across processes (SQLite lets two of
        them buy the same game before either writes), so a paid run there is
        refused unless the store was built with ``lock_runs=False``.
        """

        if self.engine.dialect.name != "postgresql":
            if self._lock_runs:
                raise RunRefused(
                    "a paid backfill needs PostgreSQL: its advisory lock is what "
                    "keeps two runs from buying the same game"
                )
            yield True
            return
        with self.engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": _BACKFILL_LOCK_ID}
                ).scalar()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _BACKFILL_LOCK_ID})

    def ready(self) -> bool:
        """Whether migration 067 has created its tables."""

        from sqlalchemy import inspect

        names = set(inspect(self.engine).get_table_names())
        return {
            ClosingLine.__tablename__,
            ClosingLinePurchase.__tablename__,
            ClosingLineSpend.__tablename__,
        } <= names

    def ledger(self) -> dict[str, str]:
        """``{game id: outcome}`` of every recorded purchase."""

        table = ClosingLinePurchase.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table.c.canonical_game_id, table.c.outcome).where(
                    table.c.market_category == self.market
                )
            )
            return {game_id: outcome for game_id, outcome in rows}

    def credits_recorded(self, subjects: Collection[str] | None = None) -> int:
        """Credits the backfill has cost, over every run.

        The games' ledger credits plus the spend log: events lists, and calls
        that may have been charged without returning anything usable.
        ``subjects`` limits the spend log to those game days and game ids, so
        one season's campaign is not charged with another's event lists.
        """

        games = ClosingLinePurchase.__table__
        spend = ClosingLineSpend.__table__
        logged_query = select(func.coalesce(func.sum(spend.c.credits), 0))
        if subjects is not None:
            logged_query = logged_query.where(spend.c.subject.in_(sorted(subjects)))
        with self.engine.connect() as connection:
            on_games = connection.execute(
                select(func.coalesce(func.sum(games.c.credits), 0)).where(
                    games.c.market_category == self.market
                )
            ).scalar()
            logged = connection.execute(logged_query).scalar() if subjects is None or subjects else 0
        return int(on_games or 0) + int(logged or 0)

    def record_spend(
        self,
        *,
        kind: str,
        subject: str,
        credits: int,
        remaining: int | None,
        spent_at: datetime,
        events_listed: int | None = None,
    ) -> None:
        """Log credits spent outside a game's ledger row (kept for every run)."""

        with self.engine.begin() as connection:
            connection.execute(
                insert(ClosingLineSpend.__table__).values(
                    kind=kind,
                    subject=subject,
                    credits=credits,
                    remaining=remaining,
                    events_listed=events_listed,
                    spent_at=spent_at,
                )
            )

    def record_receipt(
        self,
        *,
        game_id: str,
        odds_event_id: str,
        requested_for: datetime,
        credits: int,
        remaining: int | None,
        bought_at: datetime,
    ) -> None:
        """Record a paid (or free, empty) 200 response before it is parsed."""

        self._replace_failed(
            game_id,
            dict(
                odds_event_id=odds_event_id,
                requested_for=requested_for,
                outcome="received",
                http_status=200,
                credits=credits,
                remaining=remaining,
                bought_at=bought_at,
            ),
        )

    def record_failure(
        self,
        *,
        game_id: str,
        odds_event_id: str,
        requested_for: datetime,
        http_status: int | None,
        reason: str,
        bought_at: datetime,
    ) -> None:
        """Record a call that cost nothing and returned no body; a rerun retries."""

        self._replace_failed(
            game_id,
            dict(
                odds_event_id=odds_event_id,
                requested_for=requested_for,
                outcome="failed",
                http_status=http_status,
                failure_reason=reason,
                bought_at=bought_at,
            ),
        )

    def _replace_failed(self, game_id: str, values: Mapping[str, Any]) -> None:
        table = ClosingLinePurchase.__table__
        with self.engine.begin() as connection:
            # Only a failed attempt is ever replaced; a recorded purchase is
            # never overwritten, whatever the caller believes.
            connection.execute(
                delete(table).where(
                    table.c.canonical_game_id == game_id,
                    table.c.market_category == self.market,
                    table.c.outcome == "failed",
                )
            )
            connection.execute(
                insert(table).values(canonical_game_id=game_id, market_category=self.market, **values)
            )

    def store_quotes(
        self,
        game_id: str,
        *,
        snapshot_at: datetime | None,
        parsed: ParsedEventOdds,
    ) -> tuple[PostedLine, ...]:
        """Keep the parsed quotes on the receipt (``received`` -> next)."""

        lines = posted_lines(parsed, LEDGER_CATEGORIES[self.market])
        table = ClosingLinePurchase.__table__
        with self.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(
                    table.c.canonical_game_id == game_id,
                    table.c.market_category == self.market,
                    table.c.outcome == "received",
                )
                .values(
                    outcome="purchased" if parsed.bookmaker_count else "empty",
                    snapshot_at=snapshot_at,
                    quotes=encode_posted_lines(lines, full=self.market != MARKET_CATEGORY),
                )
            )
        return lines

    def purchased(self) -> list[StoredPurchase]:
        """Purchases whose quotes are stored but whose lines are not written."""

        table = ClosingLinePurchase.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    table.c.canonical_game_id,
                    table.c.odds_event_id,
                    table.c.snapshot_at,
                    table.c.bought_at,
                    table.c.quotes,
                )
                .where(
                    table.c.market_category == self.market,
                    table.c.outcome == "purchased",
                )
                .order_by(table.c.canonical_game_id)
            ).all()
        return [_stored_purchase(row) for row in rows]

    def claim_completion(self, connection: Connection, game_id: str, *, unresolved: int) -> bool:
        """Mark a ``purchased`` row ``complete`` inside the caller's transaction.

        ``False`` when the row is not ``purchased`` (already complete, or never
        bought): the caller then rolls back what it wrote with the claim, so a
        purchase is written exactly once however often it is finished.
        """

        table = ClosingLinePurchase.__table__
        result = connection.execute(
            update(table)
            .where(
                table.c.canonical_game_id == game_id,
                table.c.market_category == self.market,
                table.c.outcome == "purchased",
            )
            .values(outcome="complete", unresolved_count=unresolved)
        )
        return result.rowcount == 1

    def recorded_games(self) -> list[RecordedGame]:
        """Every game whose quotes are stored, with the lines written for it.

        ``purchased`` and ``complete`` rows, in game order: the input of a
        re-resolve, which needs no call because the quotes are on the ledger.
        """

        ledger = ClosingLinePurchase.__table__
        lines = ClosingLine.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    ledger.c.canonical_game_id,
                    ledger.c.odds_event_id,
                    ledger.c.snapshot_at,
                    ledger.c.bought_at,
                    ledger.c.quotes,
                    ledger.c.outcome,
                    ledger.c.unresolved_count,
                )
                .where(
                    ledger.c.market_category == MARKET_CATEGORY,
                    ledger.c.outcome.in_(("purchased", "complete")),
                )
                .order_by(ledger.c.canonical_game_id)
            ).all()
            stored: dict[str, set[tuple[int, str, float]]] = {}
            for game_id, player_id, book, line in connection.execute(
                select(
                    lines.c.canonical_game_id,
                    lines.c.canonical_player_id,
                    lines.c.book,
                    lines.c.line,
                ).where(lines.c.market_category == MARKET_CATEGORY)
            ):
                stored.setdefault(game_id, set()).add((int(player_id), book, float(line)))
        return [
            RecordedGame(
                purchase=_stored_purchase(row),
                outcome=row.outcome,
                unresolved=int(row.unresolved_count or 0),
                lines=frozenset(stored.get(row.canonical_game_id, ())),
            )
            for row in rows
        ]

    def complete(
        self,
        purchase: StoredPurchase,
        *,
        chosen: Mapping[int, tuple[str, float]],
        unresolved: int,
    ) -> None:
        """Write one game's chosen lines and mark it ``complete``, atomically."""

        lines = ClosingLine.__table__
        ledger = ClosingLinePurchase.__table__
        with self.engine.begin() as connection:
            connection.execute(
                delete(lines).where(
                    lines.c.canonical_game_id == purchase.game_id,
                    lines.c.market_category == MARKET_CATEGORY,
                )
            )
            if chosen:
                connection.execute(
                    insert(lines),
                    [
                        dict(
                            canonical_game_id=purchase.game_id,
                            canonical_player_id=player_id,
                            market_category=MARKET_CATEGORY,
                            book=book,
                            line=line,
                            snapshot_at=purchase.snapshot_at,
                            odds_event_id=purchase.odds_event_id,
                            bought_at=purchase.bought_at,
                        )
                        for player_id, (book, line) in sorted(chosen.items())
                    ],
                )
            connection.execute(
                update(ledger)
                .where(
                    ledger.c.canonical_game_id == purchase.game_id,
                    ledger.c.market_category == MARKET_CATEGORY,
                )
                .values(outcome="complete", unresolved_count=unresolved)
            )


@dataclass(frozen=True, slots=True)
class ClosingLineValue:
    """What ``closing_line`` carries on one game-log row."""

    value: float
    book: str

    def as_response(self) -> dict[str, Any]:
        return {"value": self.value, "book": self.book}


def _best_by_book(rows: Iterable[tuple[Any, str, float]]) -> dict[Any, ClosingLineValue]:
    """``{key: line}`` from ``(key, book, line)`` rows: the first book of ``BOOK_ORDER`` wins."""

    rank = {book: position for position, book in enumerate(BOOK_ORDER)}
    best: dict[Any, tuple[int, ClosingLineValue]] = {}
    for key, book, line in rows:
        position = rank.get(book, len(rank))
        if key not in best or position < best[key][0]:
            best[key] = (position, ClosingLineValue(float(line), book))
    return {key: value for key, (_, value) in best.items()}


class ClosingLineReader:
    """Reads stored closing lines for the game-log response."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def markets_for_player(
        self, player_id: int, game_ids: Sequence[str], *, season: str
    ) -> dict[str, dict[str, ClosingLineValue]]:
        """``{game id: {market: line}}``, one book per market, for ``closing_lines``.

        From ``LIVE_CLOSING_FIRST_SEASON`` on the lines are the live collector's
        closing quotes (``sportsbook_prop_quotes`` with ``is_closing``) in the
        five standard markets; before it they are the backfilled
        ``closing_lines`` table (2025-26: points only).  The book is the first
        of ``BOOK_ORDER`` that posted the player in that market.  A game with no
        line is absent, and a storage fault is logged and reads as "no lines".
        """

        ids = sorted({str(game_id) for game_id in game_ids})
        if not ids:
            return {}
        try:
            if season >= LIVE_CLOSING_FIRST_SEASON:
                rows = self._closing_quotes(int(player_id), ids)
            else:
                table = ClosingLine.__table__
                with self.engine.connect() as connection:
                    rows = connection.execute(
                        select(
                            table.c.canonical_game_id, table.c.market_category, table.c.book, table.c.line
                        ).where(
                            table.c.canonical_player_id == int(player_id),
                            table.c.canonical_game_id.in_(ids),
                        )
                    ).all()
        except SQLAlchemyError:
            logger.warning("Could not read closing lines for the game logs", exc_info=True)
            return {}
        chosen = _best_by_book(((game_id, market), book, line) for game_id, market, book, line in rows)
        result: dict[str, dict[str, ClosingLineValue]] = {}
        for (game_id, market), value in sorted(chosen.items()):
            result.setdefault(game_id, {})[market] = value
        return result

    def _closing_quotes(self, player_id: int, game_ids: Sequence[str]) -> list[tuple[str, str, str, float]]:
        """``(game, market, book, line)`` of the player's latest closing snapshot per game.

        A game has one closing poll unless a postponement reopened it; then the
        newest snapshot is the close, and a book it no longer carried is not
        read from the older one.
        """

        table = SportsbookPropQuote.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    table.c.canonical_game_id,
                    table.c.market_category,
                    table.c.book,
                    table.c.line,
                    table.c.observed_at,
                ).where(
                    table.c.canonical_player_id == player_id,
                    table.c.canonical_game_id.in_(list(game_ids)),
                    table.c.is_closing.is_(True),
                    table.c.market_category.in_(LEDGER_CATEGORIES[ALL_MARKETS]),
                    table.c.book.in_(BOOK_ORDER),
                )
            ).all()
        newest: dict[str, datetime] = {}
        for row in rows:
            observed = assume_utc(row.observed_at)
            if row.canonical_game_id not in newest or observed > newest[row.canonical_game_id]:
                newest[row.canonical_game_id] = observed
        return [
            (row.canonical_game_id, row.market_category, row.book, float(row.line))
            for row in rows
            if assume_utc(row.observed_at) == newest[row.canonical_game_id]
        ]

    def for_player(self, player_id: int, game_ids: Sequence[str]) -> dict[str, ClosingLineValue]:
        """``{game id: line}`` for the games that have one; best book wins.

        A storage fault is logged and reads as "no lines": the game logs are
        the product, the line is an addition.
        """

        ids = sorted({str(game_id) for game_id in game_ids})
        if not ids:
            return {}
        table = ClosingLine.__table__
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    select(table.c.canonical_game_id, table.c.book, table.c.line).where(
                        table.c.canonical_player_id == int(player_id),
                        table.c.market_category == MARKET_CATEGORY,
                        table.c.canonical_game_id.in_(ids),
                    )
                ).all()
        except SQLAlchemyError:
            logger.warning("Could not read closing lines for the game logs", exc_info=True)
            return {}
        return _best_by_book(rows)


def games_with_closing_quotes(engine: Engine, game_ids: Iterable[str]) -> set[str]:
    """The games of ``game_ids`` with at least one closing quote stored."""

    ids = sorted({str(game_id) for game_id in game_ids})
    if not ids:
        return set()
    table = SportsbookPropQuote.__table__
    with engine.connect() as connection:
        return {
            str(game_id)
            for (game_id,) in connection.execute(
                select(table.c.canonical_game_id)
                .where(table.c.canonical_game_id.in_(ids), table.c.is_closing.is_(True))
                .distinct()
            )
        }


def closing_quote_gaps(
    engine: Engine, events: Iterable[Mapping[str, Any]], *, now: datetime
) -> list[Mapping[str, Any]]:
    """The started regular-season and playoff games with no closing quote.

    ``events`` are Event Catalog rows, kept in their order (the catalog lists
    them by tip); a postponed game has not started.  This is the gap check of
    crf04/statsplus#429: every game it names is one the 2026-27 backfill buys.
    """

    started = [
        event
        for event in events
        if player_game_log_season_type(event) is not None and is_started_event(event, now=now)
    ]
    covered = games_with_closing_quotes(engine, (event["nba_game_id"] for event in started))
    return [event for event in started if str(event["nba_game_id"]) not in covered]


def describe_gap(event: Mapping[str, Any]) -> str:
    """``<game id> tip <UTC minute>``: how the gap check names a game."""

    from app.services.event_resolver import stored_timestamp

    tip = stored_timestamp(event.get("scheduled_at"))
    return f"{event['nba_game_id']} tip {'unknown' if tip is None else tip.strftime('%Y-%m-%dT%H:%MZ')}"


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Players with 20+ minutes in a bought game, and who has a line."""

    games_by_outcome: Mapping[str, int]
    players: int
    with_line: int
    by_book: Mapping[str, int]
    #: ``(game id, player name, minutes)`` of players without a line.
    missing: tuple[tuple[str, str, float], ...] = field(default=())

    @property
    def share(self) -> float:
        return self.with_line / self.players if self.players else 0.0


def coverage_report(
    engine: Engine,
    season: str,
    *,
    projected: Mapping[str, Mapping[int, tuple[str, float]]] | None = None,
) -> CoverageReport:
    """Coverage over the games the ledger holds, failed attempts excluded.

    ``projected`` replaces the stored lines of the games it names with the lines
    given (``{game id: {player id: (book, line)}}``), which is how a dry run
    reports the coverage a re-resolve would reach without writing it.
    """

    ledger = ClosingLinePurchase.__table__
    lines = ClosingLine.__table__
    logs = PlayerGameLog.__table__
    with engine.connect() as connection:
        outcomes = {
            game_id: outcome
            for game_id, outcome in connection.execute(
                select(ledger.c.canonical_game_id, ledger.c.outcome).where(
                    ledger.c.market_category == MARKET_CATEGORY,
                    ledger.c.outcome != "failed",
                )
            )
        }
        players = (
            connection.execute(
                select(logs.c.game_id, logs.c.player_id, logs.c.player_name, logs.c.minutes)
                .where(
                    logs.c.season == season,
                    logs.c.minutes >= COVERAGE_MINUTES,
                    logs.c.game_id.in_(list(outcomes)),
                )
                .order_by(logs.c.game_id, logs.c.player_id)
            ).all()
            if outcomes
            else []
        )
        stored = {
            (game_id, player_id): book
            for game_id, player_id, book in connection.execute(
                select(lines.c.canonical_game_id, lines.c.canonical_player_id, lines.c.book)
                .where(lines.c.market_category == MARKET_CATEGORY)
                .order_by(lines.c.canonical_game_id, lines.c.canonical_player_id)
            )
        }
    if projected:
        # A real run completes every game it writes lines for.
        outcomes = {
            game_id: "complete" if game_id in projected else outcome for game_id, outcome in outcomes.items()
        }
        stored = {key: book for key, book in stored.items() if key[0] not in projected}
        for game_id, chosen in projected.items():
            for player_id, (book, _) in chosen.items():
                stored[(game_id, player_id)] = book
    games_by_outcome: dict[str, int] = {}
    for outcome in outcomes.values():
        games_by_outcome[outcome] = games_by_outcome.get(outcome, 0) + 1
    by_book: dict[str, int] = {}
    missing: list[tuple[str, str, float]] = []
    for game_id, player_id, name, minutes in players:
        book = stored.get((game_id, player_id))
        if book is None:
            missing.append((game_id, name, float(minutes)))
        else:
            by_book[book] = by_book.get(book, 0) + 1
    return CoverageReport(
        games_by_outcome=games_by_outcome,
        players=len(players),
        with_line=len(players) - len(missing),
        by_book=by_book,
        missing=tuple(missing),
    )


__all__ = [
    "ALL_MARKETS",
    "BOOK_ORDER",
    "COVERAGE_MINUTES",
    "LEDGER_CATEGORIES",
    "LIVE_CLOSING_FIRST_SEASON",
    "MARKET_CATEGORY",
    "ODDS_MARKET",
    "ClosingLineReader",
    "ClosingLineStore",
    "ClosingLineValue",
    "CoverageReport",
    "PostedLine",
    "RecordedGame",
    "RunRefused",
    "StoredPurchase",
    "choose_closing_lines",
    "closing_quote_gaps",
    "coverage_report",
    "decode_posted_lines",
    "describe_gap",
    "encode_posted_lines",
    "games_with_closing_quotes",
    "posted_lines",
]
