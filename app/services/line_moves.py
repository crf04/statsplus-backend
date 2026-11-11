"""How each sportsbook line moved on a Slate Date's US Eastern day (crf04/statsplus#491).

A book's history is ``sportsbook_prop_quotes``: a row whenever its line or a
price changed, it first posted the line, re-posted it after a pull, or
closed, and the table is never pruned.  The DFS and prediction-market
providers have no line history here yet.

The day begins at 00:00 ET.  A cell's opening value is the one in force then,
with the time it was stored (the evening before, for a book's opening line),
or for a line first posted later, its first value.  A
value equal to the one before it is a re-post, never a move.  A pull writes no
row, so when a book last showed a pulled line is not stored; a pulled entry
carries when its last value was first stored instead.  A book's night poll is
its only one before 00:00 ET, so no line it posted is pulled before the day
begins.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.domain.utc import assume_utc
from app.models.sportsbook_odds import SportsbookPropQuote

#: A posted value: the line, then its Over and Under American prices.
Value = tuple[float, int | None, int | None]
#: One book's cell: book, game, player.
CellKey = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class Sighting:
    at: datetime
    value: Value


@dataclass(frozen=True, slots=True)
class CellHistory:
    """Every value one book posted for one player and market, and whether it posts one now."""

    sightings: tuple[Sighting, ...]
    posted_now: bool


@dataclass(frozen=True, slots=True)
class _Run:
    value: Value
    since: datetime


def _day_runs(history: CellHistory, day_start: datetime) -> list[_Run]:
    """The day's values in order, each from when it was first seen; equal neighbours merge."""

    before = [sighting for sighting in history.sightings if sighting.at < day_start]
    runs: list[_Run] = []
    for sighting in [*before[-1:], *(sighting for sighting in history.sightings if sighting.at >= day_start)]:
        if not runs or runs[-1].value != sighting.value:
            runs.append(_Run(sighting.value, sighting.at))
    return runs


def _iso_z(value: datetime) -> str:
    return assume_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _value(value: Value) -> dict[str, Any]:
    line, over, under = value
    return {"line": line, "over": over, "under": under}


def moves_of(history: CellHistory | None, day_start: datetime) -> dict[str, Any] | None:
    """A posted cell's opening value today, its previous value, and whether it never changed."""

    runs = [] if history is None else _day_runs(history, day_start)
    if not runs:
        return None
    return {
        "open": {**_value(runs[0].value), "seen_at": _iso_z(runs[0].since)},
        "previous": (
            None if len(runs) == 1 else {**_value(runs[-2].value), "changed_at": _iso_z(runs[-1].since)}
        ),
        "unchanged": len(runs) == 1,
    }


def pulled_value(history: CellHistory, day_start: datetime) -> dict[str, Any] | None:
    """A cell posted today and no longer posted: its last value and when that value was first seen."""

    runs = _day_runs(history, day_start)
    if history.posted_now or not runs:
        return None
    return {**_value(runs[-1].value), "seen_at": _iso_z(runs[-1].since)}


def book_histories(
    connection: Any,
    *,
    game_ids: Sequence[str],
    market: str,
    books: Iterable[str],
    posted: Iterable[tuple[str, int, str]],
) -> dict[CellKey, CellHistory]:
    """Each book cell's history for ``game_ids`` in ``market``, in one statement.

    The read goes through the series index (game, player, market, book, time),
    so it covers only the requested games' rows.  ``posted`` holds the (game,
    player, book) of every latest row: what each book posts now.
    """

    books = sorted(set(books))
    if not game_ids or not books:
        return {}
    quote = SportsbookPropQuote.__table__
    rows = connection.execute(
        select(
            quote.c.canonical_game_id,
            quote.c.canonical_player_id,
            quote.c.book,
            quote.c.line,
            quote.c.over_price,
            quote.c.under_price,
            quote.c.observed_at,
        )
        .where(
            quote.c.canonical_game_id.in_(sorted(game_ids)),
            quote.c.market_category == market,
            quote.c.book.in_(books),
        )
        .order_by(quote.c.observed_at, quote.c.quote_id)
    ).all()
    posted = set(posted)
    sightings: dict[CellKey, list[Sighting]] = {}
    for row in rows:
        game_id = str(row.canonical_game_id)
        sightings.setdefault((str(row.book), game_id, int(row.canonical_player_id)), []).append(
            Sighting(assume_utc(row.observed_at), (float(row.line), row.over_price, row.under_price))
        )
    return {
        (book, game_id, player_id): CellHistory(
            sightings=tuple(cell), posted_now=(game_id, player_id, book) in posted
        )
        for (book, game_id, player_id), cell in sightings.items()
    }


__all__ = ["CellHistory", "Sighting", "book_histories", "moves_of", "pulled_value"]
