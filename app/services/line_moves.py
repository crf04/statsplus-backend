"""How each posted line moved on a Slate Date's US Eastern day (crf04/statsplus#491).

Two stores already keep every value a source posted.  A provider's history is
``projection_observations``: every changed provider snapshot writes one row
for each of its offerings, so a cell's rows across the provider's promoted
generations are every value it held, and a Complete generation without the
cell is a moment it was not posted.  A book's history is
``sportsbook_prop_quotes``: a row whenever its line or a price changed, it
first posted the cell, re-posted it after a pull, or closed.  A pull writes
no book row, so when a book last showed a pulled line is not stored.

The day begins at 00:00 ET.  Its opening value is the one in force then, first
seen when that unbroken run of the value began (often the evening before), or
for a line first posted later, its first value.  A value equal to the one
before it is a re-post, never a move.  A started game's history ends where its
closing set does: a provider's generations polled by the tip, a book's rows
observed by it.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.domain.utc import assume_utc
from app.models.projection_archive import (
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.models.sportsbook_odds import SportsbookPropQuote
from app.providers.dfs import SnapshotStatus

#: A posted value: the line, then its Over and Under American prices (``None``
#: for a side with no per-pick price).
Value = tuple[float, int | None, int | None]
#: One source's cell: source, game, player.
CellKey = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class Sighting:
    at: datetime
    value: Value


@dataclass(frozen=True, slots=True)
class CellHistory:
    """Every value one source posted for one player and market, and when it was posted."""

    sightings: tuple[Sighting, ...]
    #: Whether the value of the last sighting before the day began was still posted then.
    posted_at_start: bool
    posted_now: bool
    #: When the source last showed the cell; ``None`` when the store does not know.
    last_seen_at: datetime | None
    name: str | None = None
    team_id: int | None = None


@dataclass(frozen=True, slots=True)
class _Run:
    value: Value
    since: datetime


def _day_runs(history: CellHistory, day_start: datetime) -> list[_Run]:
    """The day's values in order, each from when it was first seen; equal neighbours merge."""

    before = [sighting for sighting in history.sightings if sighting.at < day_start]
    start = before[-1:] if history.posted_at_start else []
    runs: list[_Run] = []
    for sighting in [*start, *(sighting for sighting in history.sightings if sighting.at >= day_start)]:
        if not runs or runs[-1].value != sighting.value:
            runs.append(_Run(sighting.value, sighting.at))
    if start:
        since = runs[0].since
        for sighting in reversed(before):
            if sighting.value != runs[0].value:
                break
            since = sighting.at
        runs[0] = _Run(runs[0].value, since)
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
    """A cell posted today and no longer posted: its last value and when it was last seen."""

    runs = _day_runs(history, day_start)
    if history.posted_now or not runs:
        return None
    return {
        **_value(runs[-1].value),
        "last_seen_at": None if history.last_seen_at is None else _iso_z(history.last_seen_at),
    }


@dataclass(frozen=True, slots=True)
class _Generation:
    generation_id: str
    scope: tuple[str, str]
    order: tuple[datetime, datetime, str]
    complete: bool
    polled_at: datetime
    #: The newest poll that confirmed this generation, its own included.
    confirmed_at: datetime


def _resolved(rows: Iterable[Any]) -> Value | None:
    """A generation's one value for a cell, as the Player Pool resolves it; ``None`` at two lines."""

    lines = {float(row.line_value) for row in rows}
    if len(lines) != 1:
        return None
    pairs = {
        (row.over_price, row.under_price)
        for row in rows
        if row.over_price is not None or row.under_price is not None
    }
    over, under = next(iter(pairs)) if len(pairs) == 1 else (None, None)
    return (next(iter(lines)), over, under)


def _posted_by(generations: Sequence[_Generation], end: int, present: Mapping[str, Any]) -> bool:
    """Whether a cell was posted after ``generations[:end]``: in the newest Complete one or a Partial since."""

    for index in range(end - 1, -1, -1):
        if generations[index].generation_id in present:
            return True
        if generations[index].complete:
            return False
    return False


def provider_histories(
    connection: Any,
    *,
    season: str,
    game_ids: Sequence[str],
    statistic_ids: Sequence[str],
    providers: Iterable[str],
    fences: Mapping[str, datetime],
    day_start: datetime,
) -> dict[CellKey, CellHistory]:
    """Each provider cell's history in the market's statistics for ``game_ids``.

    One statement reads the cells' targetable lines through the governed
    identity index (game, player, statistic); a second reads the providers'
    promoted generations since the first of those rows, with each one's
    snapshot status and polls.  ``fences`` holds a started game's tip.
    """

    providers = sorted(set(providers))
    if not game_ids or not statistic_ids or not providers:
        return {}
    observation = ProjectionObservation.__table__
    rows = connection.execute(
        select(
            observation.c.provider,
            observation.c.generation_id,
            observation.c.canonical_game_id,
            observation.c.canonical_player_id,
            observation.c.canonical_player_name,
            observation.c.canonical_team_id,
            observation.c.line_value,
            observation.c.over_price,
            observation.c.under_price,
            observation.c.observed_at,
        ).where(
            observation.c.canonical_game_id.in_(sorted(game_ids)),
            observation.c.canonical_statistic_id.in_(sorted(statistic_ids)),
            observation.c.provider.in_(providers),
            observation.c.targetable.is_(True),
            observation.c.line_value.is_not(None),
        )
    ).all()
    if not rows:
        return {}
    generations = _generations(
        connection,
        season=season,
        providers=providers,
        since=min(assume_utc(row.observed_at) for row in rows),
    )
    by_cell: dict[tuple[str, str, str, int], dict[str, list[Any]]] = {}
    for row in rows:
        generation = generations.get(str(row.generation_id))
        if generation is None:
            continue
        key = (*generation.scope, str(row.canonical_game_id), int(row.canonical_player_id))
        by_cell.setdefault(key, {}).setdefault(generation.generation_id, []).append(row)
    sequences: dict[tuple[str, str], list[_Generation]] = {}
    for generation in sorted(generations.values(), key=lambda generation: generation.order):
        sequences.setdefault(generation.scope, []).append(generation)
    # Each scope's generations a game's history reads (polled by its tip, if it started), and
    # how many of them came before the day began.
    eligible: dict[tuple[str, str, str], tuple[list[_Generation], int]] = {}
    histories: dict[CellKey, CellHistory] = {}
    for (provider, query_key, game_id, player_id), cell_rows in by_cell.items():
        fence = fences.get(game_id)
        if (provider, query_key, game_id) not in eligible:
            sequence = [g for g in sequences[(provider, query_key)] if fence is None or g.polled_at <= fence]
            eligible[(provider, query_key, game_id)] = (
                sequence,
                bisect_left([g.order[0] for g in sequence], day_start),
            )
        sequence, before_day = eligible[(provider, query_key, game_id)]
        present = sorted(
            (
                generation
                for generation in map(generations.__getitem__, cell_rows)
                if fence is None or generation.polled_at <= fence
            ),
            key=lambda generation: generation.order,
        )
        if not present:
            continue
        sightings = tuple(
            Sighting(generation.order[0], value)
            for generation in present
            for value in (_resolved(cell_rows[generation.generation_id]),)
            if value is not None
        )
        newest = cell_rows[present[-1].generation_id][-1]
        history = CellHistory(
            sightings=sightings,
            posted_at_start=_posted_by(sequence, before_day, cell_rows),
            posted_now=_posted_by(sequence, len(sequence), cell_rows),
            last_seen_at=present[-1].confirmed_at,
            name=newest.canonical_player_name,
            team_id=newest.canonical_team_id,
        )
        # A provider answers one query per season; should two scopes name a cell, the newer serves.
        previous = histories.get((provider, game_id, player_id))
        if previous is None or previous.last_seen_at < history.last_seen_at:
            histories[(provider, game_id, player_id)] = history
    return histories


def _generations(
    connection: Any, *, season: str, providers: Sequence[str], since: datetime
) -> dict[str, _Generation]:
    generation = ProjectionMaterializationGeneration.__table__
    snapshot = ProjectionProviderSnapshot.__table__
    poll = ProviderPoll.__table__
    rows = connection.execute(
        select(
            generation.c.generation_id,
            generation.c.provider,
            generation.c.query_key,
            generation.c.retrieved_at,
            generation.c.created_at,
            snapshot.c.snapshot_status,
            poll.c.completed_at,
        )
        .select_from(
            generation.join(snapshot, generation.c.snapshot_id == snapshot.c.snapshot_id).join(
                poll, generation.c.source_poll_id == poll.c.poll_id
            )
        )
        .where(
            generation.c.provider.in_(providers),
            generation.c.season == season,
            generation.c.outcome == "advanced",
            generation.c.retrieved_at >= since,
        )
    ).all()
    confirmations: dict[str, datetime] = {}
    if rows:
        for row in connection.execute(
            select(poll.c.generation_id, poll.c.retrieved_at).where(
                poll.c.generation_id.in_([str(row.generation_id) for row in rows]),
                poll.c.retrieved_at.is_not(None),
            )
        ):
            at = assume_utc(row.retrieved_at)
            confirmations[str(row.generation_id)] = max(confirmations.get(str(row.generation_id), at), at)
    return {
        str(row.generation_id): _Generation(
            generation_id=str(row.generation_id),
            scope=(str(row.provider), str(row.query_key)),
            order=(assume_utc(row.retrieved_at), assume_utc(row.created_at), str(row.generation_id)),
            complete=row.snapshot_status == SnapshotStatus.COMPLETE.value,
            polled_at=assume_utc(row.completed_at),
            confirmed_at=confirmations.get(str(row.generation_id), assume_utc(row.retrieved_at)),
        )
        for row in rows
    }


def book_histories(
    connection: Any,
    *,
    game_ids: Sequence[str],
    market: str,
    books: Iterable[str],
    posted: Iterable[tuple[str, int, str]],
    fences: Mapping[str, datetime],
) -> dict[CellKey, CellHistory]:
    """Each book cell's history for ``game_ids`` in ``market``, through the series index.

    ``posted`` holds the (game, player, book) of every latest row: what each
    book posts now.  A book cell's last row before the day began is taken as
    still posted then; books poll once overnight, so none pulls a line
    between that poll and midnight.
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
        at = assume_utc(row.observed_at)
        fence = fences.get(game_id)
        if fence is not None and at > fence:
            continue
        sightings.setdefault((str(row.book), game_id, int(row.canonical_player_id)), []).append(
            Sighting(at, (float(row.line), row.over_price, row.under_price))
        )
    return {
        (book, game_id, player_id): CellHistory(
            sightings=tuple(cell),
            posted_at_start=True,
            posted_now=(game_id, player_id, book) in posted,
            last_seen_at=None,
        )
        for (book, game_id, player_id), cell in sightings.items()
    }


__all__ = ["CellHistory", "Sighting", "book_histories", "moves_of", "provider_histories", "pulled_value"]
