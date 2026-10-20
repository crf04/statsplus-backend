"""The change-only line history retention derives from the projection archive (#502).

For each key (provider, provider market, canonical player, canonical
statistic, variant) ``projection_line_history`` holds one row per change of
``line_value``, ``over_price``, ``under_price``, ``price_value`` or
``market_status`` across every observation the archive ever held, plus a
``withdrawn`` row when a key present in one Complete generation of its scope
is absent from the next Complete generation.  A Partial generation never
implies a withdrawal.

``LineHistory.derive`` recomputes each key touched by new evidence (rows of
generations created since ``since``, or every row on the first run) from the
earliest new time onward: the key's recorded rows from there, its archived
rows from there and its withdrawals are merged in time order and compressed
against the last recorded row before that time.  A late observation therefore
lands between its neighbours and a later row it now repeats is removed.  A
recomputation of unchanged evidence writes nothing, so reruns are idempotent.

Rows the archive has already deleted survive only as their recorded changes:
a late value that falls inside a stretch of pruned repeats holds until the
next recorded change.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from sqlalchemy import and_, delete, insert, or_, select
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.projection_archive import (
    ProjectionLineHistory,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.providers.dfs import SnapshotStatus

HISTORY_KEY = (
    "provider",
    "provider_market_id",
    "canonical_player_id",
    "canonical_statistic_id",
    "market_variant",
)
HISTORY_VALUES = ("line_value", "over_price", "under_price", "price_value", "market_status")
#: The status of a key that left a Complete board.
WITHDRAWN = "withdrawn"
_WITHDRAWN_VALUES = (None, None, None, None, WITHDRAWN)

_OBS = ProjectionObservation.__table__
_GEN = ProjectionMaterializationGeneration.__table__
_SNAP = ProjectionProviderSnapshot.__table__
_POLL = ProviderPoll.__table__
_HISTORY = ProjectionLineHistory.__table__

Key = tuple[Any, ...]


@dataclass(order=True)
class _Event:
    observed_at: datetime
    #: At one instant an archived row outranks a derived withdrawal, which
    #: outranks an already recorded row.
    rank: int
    tiebreak: str
    values: tuple[Any, ...] = field(compare=False)
    game_id: str | None = field(compare=False)


@dataclass
class _KeyState:
    """One key's recomputation: its base, pending extra events and output."""

    start: datetime
    current: tuple[Any, ...] | None = None
    last_at: datetime | None = None
    extras: deque[_Event] = field(default_factory=deque)
    recorded: dict[datetime, tuple[int, tuple[Any, ...], str | None]] = field(default_factory=dict)
    output: dict[datetime, tuple[tuple[Any, ...], str | None]] = field(default_factory=dict)

    def feed(self, event: _Event) -> None:
        if self.last_at is not None and event.observed_at == self.last_at:
            return
        self.last_at = event.observed_at
        if event.values != self.current:
            self.output[event.observed_at] = (event.values, event.game_id)
            self.current = event.values

    def flush(self, before: datetime | None) -> None:
        while self.extras and (before is None or self.extras[0].observed_at < before):
            self.feed(self.extras.popleft())

    def drop_at(self, at: datetime) -> None:
        while self.extras and self.extras[0].observed_at == at:
            self.extras.popleft()


def history_key(row: Any) -> Key:
    return tuple(row[name] for name in HISTORY_KEY)


def _keyed(row: Any) -> bool:
    return all(row[name] is not None for name in HISTORY_KEY)


class LineHistory:
    """Derive and store one provider's change-only line history."""

    def __init__(self, engine: Engine, *, batch_size: int) -> None:
        self.engine = engine
        self.batch_size = batch_size

    def derive(self, provider: str, since: datetime | None, *, write: bool) -> dict[str, int]:
        """Recompute the keys new evidence touches; return rows written and removed."""

        tally = {"rows": 0, "removed": 0, "unkeyed": 0, "withdrawals": 0}
        starts: dict[Key, datetime] = {}
        new_generation = None if since is None else _GEN.c.created_at >= since
        for row in self._rows(provider, generation_filter=new_generation):
            if not _keyed(row):
                tally["unkeyed"] += 1
                continue
            key = history_key(row)
            observed_at = assume_utc(row["observed_at"])
            if key not in starts or observed_at < starts[key]:
                starts[key] = observed_at
        withdrawals = self._withdrawals(provider, since)
        for key, event in withdrawals:
            if key not in starts or event.observed_at < starts[key]:
                starts[key] = event.observed_at
        if not starts:
            return tally

        states = {key: _KeyState(start=start) for key, start in starts.items()}
        self._load_recorded(provider, states)
        for key, event in withdrawals:
            if event.observed_at >= states[key].start:
                states[key].extras.append(event)
        for state in states.values():
            state.extras = deque(sorted(state.extras))

        earliest = min(starts.values())
        for row in self._rows(provider, observed_from=earliest):
            if not _keyed(row):
                continue
            state = states.get(history_key(row))
            observed_at = assume_utc(row["observed_at"])
            if state is None or observed_at < state.start:
                continue
            state.flush(observed_at)
            state.drop_at(observed_at)
            state.feed(
                _Event(
                    observed_at,
                    0,
                    str(row["observation_id"]),
                    tuple(row[name] for name in HISTORY_VALUES),
                    row["canonical_game_id"],
                )
            )
        for state in states.values():
            state.flush(None)

        removals: list[int] = []
        additions: list[dict[str, Any]] = []
        for key, state in states.items():
            for at, (history_id, values, game_id) in state.recorded.items():
                if state.output.get(at) != (values, game_id):
                    removals.append(history_id)
            for at, (values, game_id) in state.output.items():
                recorded = state.recorded.get(at)
                if recorded is None or (recorded[1], recorded[2]) != (values, game_id):
                    additions.append(
                        {
                            **dict(zip(HISTORY_KEY, key, strict=True)),
                            "canonical_game_id": game_id,
                            "observed_at": at,
                            **dict(zip(HISTORY_VALUES, values, strict=True)),
                        }
                    )
        tally["rows"] = len(additions)
        tally["removed"] = len(removals)
        tally["withdrawals"] = sum(1 for row in additions if row["market_status"] == WITHDRAWN)
        if write:
            with self.engine.begin() as connection:
                for start in range(0, len(removals), 1000):
                    connection.execute(
                        delete(_HISTORY).where(_HISTORY.c.history_id.in_(removals[start : start + 1000]))
                    )
                for start in range(0, len(additions), self.batch_size):
                    connection.execute(insert(_HISTORY), additions[start : start + self.batch_size])
        return tally

    def _rows(
        self,
        provider: str,
        *,
        generation_filter: Any = None,
        observed_from: datetime | None = None,
    ) -> Iterator[Any]:
        """The provider's archived rows in time order, read in keyset batches."""

        columns = [_OBS.c[name] for name in (*HISTORY_KEY, *HISTORY_VALUES)]
        conditions = [_OBS.c.provider == provider]
        if generation_filter is not None:
            conditions.append(generation_filter)
        if observed_from is not None:
            conditions.append(_OBS.c.observed_at >= observed_from)
        cursor: tuple[Any, Any] | None = None
        while True:
            after = ()
            if cursor is not None:
                after = (
                    or_(
                        _OBS.c.observed_at > cursor[0],
                        and_(_OBS.c.observed_at == cursor[0], _OBS.c.observation_id > cursor[1]),
                    ),
                )
            with self.engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(_OBS.c.observation_id, _OBS.c.observed_at, _OBS.c.canonical_game_id, *columns)
                        .select_from(_OBS.join(_GEN, _GEN.c.generation_id == _OBS.c.generation_id))
                        .where(*conditions, *after)
                        .order_by(_OBS.c.observed_at, _OBS.c.observation_id)
                        .limit(self.batch_size)
                    )
                    .mappings()
                    .all()
                )
            if not rows:
                return
            cursor = (rows[-1]["observed_at"], rows[-1]["observation_id"])
            yield from rows

    def _withdrawals(self, provider: str, since: datetime | None) -> list[tuple[Key, _Event]]:
        """Keys a Complete board dropped, from each pair of consecutive Complete boards.

        A pair is read only when one of its boards is new and both still hold
        every row they were written with; a board retention already thinned
        cannot say what it lacked.
        """

        with self.engine.connect() as connection:
            boards = (
                connection.execute(
                    select(
                        _GEN.c.generation_id,
                        _GEN.c.season,
                        _GEN.c.query_key,
                        _GEN.c.retrieved_at,
                        _GEN.c.created_at,
                        _POLL.c.observation_count,
                    )
                    .select_from(
                        _GEN.join(_SNAP, _SNAP.c.snapshot_id == _GEN.c.snapshot_id).join(
                            _POLL, _POLL.c.poll_id == _GEN.c.source_poll_id
                        )
                    )
                    .where(
                        _GEN.c.provider == provider,
                        _GEN.c.is_replay.is_(False),
                        _SNAP.c.snapshot_status == SnapshotStatus.COMPLETE.value,
                    )
                    .order_by(
                        _GEN.c.season,
                        _GEN.c.query_key,
                        _GEN.c.retrieved_at,
                        _GEN.c.created_at,
                        _GEN.c.generation_id,
                    )
                )
                .mappings()
                .all()
            )
            events: list[tuple[Key, _Event]] = []

            def board_keys(board: Any) -> dict[Key, str | None] | None:
                rows = (
                    connection.execute(
                        select(_OBS.c.canonical_game_id, *[_OBS.c[name] for name in HISTORY_KEY]).where(
                            _OBS.c.generation_id == board["generation_id"]
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(rows) != int(board["observation_count"]):
                    return None
                return {history_key(row): row["canonical_game_id"] for row in rows if _keyed(row)}

            # Only the previous board's keys are held, whatever the archive's size.
            held: tuple[str, dict[Key, str | None] | None] | None = None
            for previous, following in pairwise(boards):
                if (previous["season"], previous["query_key"]) != (
                    following["season"],
                    following["query_key"],
                ):
                    continue
                if since is not None and all(
                    assume_utc(board["created_at"]) < since for board in (previous, following)
                ):
                    continue
                if held is None or held[0] != previous["generation_id"]:
                    held = (previous["generation_id"], board_keys(previous))
                before = held[1]
                after = board_keys(following)
                held = (following["generation_id"], after)
                if before is None or after is None:
                    continue
                at = assume_utc(following["retrieved_at"])
                for key in sorted(before.keys() - after.keys(), key=repr):
                    events.append(
                        (key, _Event(at, 1, str(following["generation_id"]), _WITHDRAWN_VALUES, before[key]))
                    )
        return events

    def _load_recorded(self, provider: str, states: dict[Key, _KeyState]) -> None:
        """Each key's last recorded row before its start, and its recorded rows from there."""

        market_ids = sorted({key[1] for key in states})
        with self.engine.connect() as connection:
            for start in range(0, len(market_ids), 1000):
                for row in connection.execute(
                    select(_HISTORY).where(
                        _HISTORY.c.provider == provider,
                        _HISTORY.c.provider_market_id.in_(market_ids[start : start + 1000]),
                    )
                ).mappings():
                    state = states.get(history_key(row))
                    if state is None:
                        continue
                    at = assume_utc(row["observed_at"])
                    values = tuple(row[name] for name in HISTORY_VALUES)
                    if at < state.start:
                        if state.last_at is None or at > state.last_at:
                            state.last_at, state.current = at, values
                        continue
                    state.recorded[at] = (int(row["history_id"]), values, row["canonical_game_id"])
                    state.extras.append(
                        _Event(at, 2, str(row["history_id"]), values, row["canonical_game_id"])
                    )
        # ``last_at`` held the base row only to pick it; the merge starts afresh.
        for state in states.values():
            state.last_at = None
