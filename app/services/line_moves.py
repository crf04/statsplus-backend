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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, literal, or_, select, tuple_, union, union_all

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


def _lines(rows: Iterable[Any]) -> list[Any]:
    """The rows that post a line: the pool's candidates."""

    return [row for row in rows if row.targetable and row.line_value is not None]


def _resolved(rows: Sequence[Any]) -> Value | None:
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


@dataclass(frozen=True, slots=True)
class _Sequence:
    """A scope's generations one game reads, in order, and where each Complete one falls."""

    generations: list[_Generation]
    index: dict[str, int]
    #: For each position, the first Complete generation at or after it (``len`` when none).
    next_complete: list[int]
    #: How many generations came before the day began.
    before_day: int

    @classmethod
    def of(cls, generations: list[_Generation], day_start: datetime) -> _Sequence:
        next_complete = [len(generations)] * (len(generations) + 1)
        for position in range(len(generations) - 1, -1, -1):
            next_complete[position] = (
                position if generations[position].complete else next_complete[position + 1]
            )
        return cls(
            generations,
            {generation.generation_id: position for position, generation in enumerate(generations)},
            next_complete,
            bisect_left([generation.order[0] for generation in generations], day_start),
        )

    def replay(
        self, events: Sequence[int], rows: Mapping[str, list[Any]], end: int
    ) -> tuple[bool, int | None]:
        """Replay a cell's market references through ``generations[:end]`` as Latest applies them.

        A Complete generation replaces the provider's offerings; a Partial one
        withdraws each reference it observes and posts the targetable ones,
        carrying the rest.  Returns whether the cell was posted afterwards and
        the position of the last generation that posted it; one that carried it
        did not see it.
        """

        live: set[str] = set()
        last = None
        applied = -1
        for position in [*(event for event in events if event < end), end]:
            if live and self.next_complete[applied + 1] < position:
                live = set()
            if position == end:
                break
            generation_rows = rows[self.generations[position].generation_id]
            posted = {row.market_reference for row in _lines(generation_rows)}
            if self.generations[position].complete:
                live = posted
            else:
                live = (live - {row.market_reference for row in generation_rows}) | posted
            if posted:
                last = position
            applied = position
        return bool(live), last


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

    Reads go through the governed identity index (game, player, statistic) and
    span only the cells' own history: their line-bearing targetable rows; the
    providers' promoted generations since the first of those rows, with each
    one's snapshot status and newest confirming poll; and the untargetable rows
    of those generations that are Partial, which withdraw an offering.
    ``fences`` holds a started game's tip.
    """

    providers = sorted(set(providers))
    if not game_ids or not statistic_ids or not providers:
        return {}
    observation = ProjectionObservation.__table__
    columns = (
        observation.c.provider,
        observation.c.generation_id,
        observation.c.market_reference,
        observation.c.targetable,
        observation.c.canonical_game_id,
        observation.c.canonical_player_id,
        observation.c.canonical_player_name,
        observation.c.canonical_team_id,
        observation.c.line_value,
        observation.c.over_price,
        observation.c.under_price,
    )
    cells = (
        observation.c.canonical_game_id.in_(sorted(game_ids)),
        observation.c.canonical_statistic_id.in_(sorted(statistic_ids)),
        observation.c.provider.in_(providers),
    )
    posting = and_(observation.c.targetable.is_(True), observation.c.line_value.is_not(None))
    rows = connection.execute(select(*columns).where(*cells, posting)).all()
    if not rows:
        return {}
    generation = ProjectionMaterializationGeneration.__table__
    snapshot = ProjectionProviderSnapshot.__table__
    partial = (
        select(generation.c.generation_id)
        .select_from(generation.join(snapshot, generation.c.snapshot_id == snapshot.c.snapshot_id))
        .where(
            generation.c.provider.in_(providers),
            generation.c.season == season,
            generation.c.outcome == "advanced",
            snapshot.c.snapshot_status == SnapshotStatus.PARTIAL.value,
        )
    )
    rows += connection.execute(
        select(*columns).where(*cells, ~posting, observation.c.generation_id.in_(partial))
    ).all()
    generations = _generations(
        connection,
        season=season,
        held=sorted({str(row.generation_id) for row in rows}),
        fences=fences.values(),
    )
    by_cell: dict[tuple[str, str, str, int], dict[str, list[Any]]] = {}
    for row in rows:
        generation = generations.get(str(row.generation_id))
        if generation is None:
            continue
        key = (*generation.scope, str(row.canonical_game_id), int(row.canonical_player_id))
        by_cell.setdefault(key, {}).setdefault(generation.generation_id, []).append(row)
    scopes: dict[tuple[str, str], list[_Generation]] = {}
    for generation in sorted(generations.values(), key=lambda generation: generation.order):
        scopes.setdefault(generation.scope, []).append(generation)
    # Each scope's generations a game reads: those polled by its tip, if it started.
    sequences: dict[tuple[str, str, str], _Sequence] = {}
    histories: dict[CellKey, CellHistory] = {}
    last_posted: dict[CellKey, _Generation] = {}
    for (provider, query_key, game_id, player_id), cell_rows in by_cell.items():
        if (provider, query_key, game_id) not in sequences:
            fence = fences.get(game_id)
            sequences[(provider, query_key, game_id)] = _Sequence.of(
                [g for g in scopes[(provider, query_key)] if fence is None or g.polled_at <= fence], day_start
            )
        sequence = sequences[(provider, query_key, game_id)]
        events = sorted(
            sequence.index[generation_id] for generation_id in cell_rows if generation_id in sequence.index
        )
        posted_now, last = sequence.replay(events, cell_rows, len(sequence.generations))
        if last is None:
            continue
        sightings = tuple(
            Sighting(sequence.generations[event].order[0], value)
            for event in events
            for value in (_resolved(_lines(cell_rows[sequence.generations[event].generation_id])),)
            if value is not None
        )
        newest = cell_rows[sequence.generations[events[-1]].generation_id][-1]
        history = CellHistory(
            sightings=sightings,
            posted_at_start=sequence.replay(events, cell_rows, sequence.before_day)[0],
            posted_now=posted_now,
            last_seen_at=sequence.generations[last].order[0],
            name=newest.canonical_player_name,
            team_id=newest.canonical_team_id,
        )
        # A provider answers one query per season; should two scopes name a cell, the newer serves.
        previous = histories.get((provider, game_id, player_id))
        if previous is None or previous.last_seen_at < history.last_seen_at:
            histories[(provider, game_id, player_id)] = history
            last_posted[(provider, game_id, player_id)] = sequence.generations[last]
    pulled = {key: last_posted[key] for key, history in histories.items() if not history.posted_now}
    confirmations = _confirmations(connection, season=season, generations=set(pulled.values()))
    for key, generation in pulled.items():
        histories[key] = replace(
            histories[key], last_seen_at=confirmations.get(generation.generation_id, generation.order[0])
        )
    return histories


def _generations(
    connection: Any, *, season: str, held: Sequence[str], fences: Iterable[datetime]
) -> dict[str, _Generation]:
    """The promoted generations a replay of the cells reads.

    Those are the ones holding the cells' rows, and the first Complete
    generation after each of them, which withdraws a cell it omits; a started
    game replays only generations polled by its tip, so the first Complete
    after each is found again among those.  No other generation changes a
    replay: a Partial one without the cell carries it, and a later Complete
    one finds it already withdrawn.
    """

    generation = ProjectionMaterializationGeneration.__table__
    snapshot = ProjectionProviderSnapshot.__table__
    poll = ProviderPoll.__table__
    order = (generation.c.retrieved_at, generation.c.created_at, generation.c.generation_id)
    fields = (
        "generation_id",
        "provider",
        "query_key",
        "retrieved_at",
        "created_at",
        "snapshot_status",
        "completed_at",
    )
    described = (
        select(
            *(generation.c[field] for field in fields[:5]), snapshot.c.snapshot_status, poll.c.completed_at
        )
        .select_from(
            generation.join(snapshot, generation.c.snapshot_id == snapshot.c.snapshot_id).join(
                poll, generation.c.source_poll_id == poll.c.poll_id
            )
        )
        .where(generation.c.season == season, generation.c.outcome == "advanced")
    )
    holding = described.where(generation.c.generation_id.in_(held)).cte("holding")
    selects = [select(*(holding.c[field] for field in fields))]
    for index, fence in enumerate([None, *sorted(set(fences))]):
        completes = (
            described.add_columns(
                *(
                    func.lag(column)
                    .over(partition_by=(generation.c.provider, generation.c.query_key), order_by=order)
                    .label(f"previous_{column.name}")
                    for column in order
                )
            )
            .where(
                snapshot.c.snapshot_status == SnapshotStatus.COMPLETE.value,
                generation.c.provider.in_(select(holding.c.provider)),
                *(() if fence is None else (poll.c.completed_at <= fence,)),
            )
            .cte(f"completes_{index}")
        )
        held_before = (
            select(holding.c.generation_id)
            .where(
                holding.c.provider == completes.c.provider,
                holding.c.query_key == completes.c.query_key,
                tuple_(holding.c.retrieved_at, holding.c.created_at, holding.c.generation_id)
                < tuple_(completes.c.retrieved_at, completes.c.created_at, completes.c.generation_id),
                or_(
                    completes.c.previous_generation_id.is_(None),
                    tuple_(
                        completes.c.previous_retrieved_at,
                        completes.c.previous_created_at,
                        completes.c.previous_generation_id,
                    )
                    <= tuple_(holding.c.retrieved_at, holding.c.created_at, holding.c.generation_id),
                ),
            )
            .exists()
        )
        selects.append(select(*(completes.c[field] for field in fields)).where(held_before))
    return {
        str(row.generation_id): _Generation(
            generation_id=str(row.generation_id),
            scope=(str(row.provider), str(row.query_key)),
            order=(assume_utc(row.retrieved_at), assume_utc(row.created_at), str(row.generation_id)),
            complete=row.snapshot_status == SnapshotStatus.COMPLETE.value,
            polled_at=assume_utc(row.completed_at),
        )
        for row in connection.execute(union(*selects)).all()
    }


#: Past any poll: a run no other generation's poll has ended yet.
_END_OF_TIME = datetime(9999, 12, 31, tzinfo=UTC)


def _confirmations(
    connection: Any, *, season: str, generations: Iterable[_Generation]
) -> dict[str, datetime]:
    """Each generation's newest confirming poll: the last of the run of its scope's polls that name it.

    Both bounds go through the polls' scope index (provider, season, query,
    completion time): the run starts at the generation's own poll and ends at
    the scope's next poll that names another generation.
    """

    poll = ProviderPoll.__table__
    confirming, later = poll.alias("confirming"), poll.alias("later")
    selects = []
    for generation in sorted(generations, key=lambda generation: generation.generation_id):
        provider, query_key = generation.scope

        def scope(table: Any, provider: str = provider, query_key: str = query_key) -> tuple[Any, ...]:
            return (table.c.provider == provider, table.c.season == season, table.c.query_key == query_key)

        run_end = (
            select(later.c.completed_at)
            .where(
                *scope(later),
                later.c.completed_at > generation.polled_at,
                later.c.generation_id.is_not(None),
                later.c.generation_id != generation.generation_id,
            )
            .order_by(later.c.completed_at)
            .limit(1)
            .scalar_subquery()
        )
        selects.append(
            select(
                literal(generation.generation_id).label("generation_id"),
                func.max(confirming.c.retrieved_at).label("confirmed_at"),
            ).where(
                *scope(confirming),
                confirming.c.generation_id == generation.generation_id,
                confirming.c.completed_at >= generation.polled_at,
                confirming.c.completed_at < func.coalesce(run_end, _END_OF_TIME),
            )
        )
    if not selects:
        return {}
    return {
        str(row.generation_id): assume_utc(row.confirmed_at)
        for row in connection.execute(union_all(*selects)).all()
        if row.confirmed_at is not None
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
