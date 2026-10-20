"""Projection archive retention: three days of full evidence, a change-only line history.

``ProjectionArchiveRetention.run`` first derives ``projection_line_history``
from every archive row older than the window, then deletes the rows no reader
needs, scope by scope, in foreign-key order (observations, generations, polls,
snapshots) and in short batches.  Each delete batch holds the scope lock the
collector writes under and, before it commits, checks that every Latest
pointer, every closing membership and the active generation's snapshot still
resolve; a violation rolls that batch back and ends the run.

What a reader needs is kept whatever its age:

* Latest pointers and closing memberships, with their generations;
* the active generation, its snapshot (with ``evidence_document``) and every
  observation of that snapshot, which a mapping replay reads;
* what a closing replay reads (``ProjectionArchive._closing_replay_bounds``):
  for every started game, the baseline generation before its tip, the
  baseline's targetable rows for the game and every row for the game in the
  generations after it; for games yet to start, the same from the current
  baseline on.  ``get_retained_pool_for_game`` and a later freeze therefore
  replay the same rows after a prune;
* each scope's newest promoted and newest failed poll, which provider health
  and the write path's promotion fences read;
* every generation, snapshot and poll a kept row references.

``prizepicks`` is exempt until it writes Complete baselines.  A dry run reads
the same plan and writes nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, text, update
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc, utc_now
from app.models.projection_archive import (
    ClosingProjectionMembership,
    ClosingProjectionSet,
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionLineHistory,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProjectionRetentionRun,
    ProviderPoll,
    ProviderPollOutcome,
)
from app.services.projection_archive import (
    ProjectionArchive,
    _projection_closing_start_fence,
    newest_poll_first,
)
from app.services.statistic_catalog import StatisticCatalog

logger = logging.getLogger(__name__)

RETENTION_WINDOW = timedelta(days=3)
#: Providers whose rows are never read, kept or deleted by retention.
#: ``prizepicks`` writes only Partial snapshots, so its closing replay reads
#: from the season's first generation.
EXEMPT_PROVIDERS = frozenset({"prizepicks"})
DEFAULT_BATCH_SIZE = 5000
#: Snapshots carry their multi-megabyte evidence document.
DEFAULT_SNAPSHOT_BATCH_SIZE = 50
#: Session advisory lock that keeps two real runs from interleaving.
RETENTION_ADVISORY_LOCK_ID = 502_017_440

_OBS = ProjectionObservation.__table__
_GEN = ProjectionMaterializationGeneration.__table__
_POLL = ProviderPoll.__table__
_SNAP = ProjectionProviderSnapshot.__table__
_LATEST = LatestPlayerProjection.__table__
_MEMBER = ClosingProjectionMembership.__table__
_SET = ClosingProjectionSet.__table__
_LOCK = ProjectionArchiveScopeLock.__table__
_HISTORY = ProjectionLineHistory.__table__
_RUNS = ProjectionRetentionRun.__table__

_TABLES = (
    ("projection_observations", _OBS),
    ("projection_materialization_generations", _GEN),
    ("projection_provider_polls", _POLL),
    ("projection_provider_snapshots", _SNAP),
)
_HISTORY_KEY = (
    "provider",
    "provider_market_id",
    "canonical_player_id",
    "canonical_statistic_id",
    "market_variant",
)
_HISTORY_VALUES = ("line_value", "over_price", "under_price", "price_value", "market_status")


class RetentionSafetyViolation(RuntimeError):
    """A delete batch would leave a pointer unresolved; the batch rolls back."""


@dataclass(frozen=True)
class _Scope:
    provider: str
    season: str
    query_key: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.season}/{self.query_key}"


@dataclass(frozen=True)
class _ReplayRule:
    """What one closing replay reads: its baseline and replayed generations."""

    baseline_id: str | None
    generation_ids: frozenset[str]

    def needs(self, generation_id: str, targetable: bool) -> bool:
        # The baseline clears the replayed state, so only its targetable rows
        # enter it; every row of a later generation can retire a reference.
        if generation_id not in self.generation_ids:
            return False
        return targetable or generation_id != self.baseline_id


@dataclass(frozen=True)
class _ReplayRules:
    by_game: Mapping[str, _ReplayRule]
    future: _ReplayRule

    @property
    def baselines(self) -> set[str]:
        rules = (*self.by_game.values(), self.future)
        return {rule.baseline_id for rule in rules if rule.baseline_id is not None}

    def needs(self, game_id: str | None, generation_id: str, targetable: bool) -> bool:
        if game_id is None:
            return False
        rule = self.by_game.get(game_id)
        if rule is not None and rule.needs(generation_id, targetable):
            return True
        return self.future.needs(generation_id, targetable)


@dataclass(frozen=True)
class _ParentPlan:
    generations: tuple[str, ...]
    polls: tuple[str, ...]
    snapshots: tuple[str, ...]
    protected: Mapping[str, Mapping[str, int]]


@dataclass
class RetentionReport:
    """Exact counts for one run: what it deleted (or would), kept and recorded."""

    mode: str
    trigger: str
    cutoff: datetime
    outcome: str = "running"
    failure: dict[str, str] | None = None
    #: table -> provider -> {"before", "delete", "keep"}
    tables: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    #: table -> provider -> keep reason -> rows older than the window kept
    protected: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    #: provider -> {"rows": history rows written, "unkeyed": rows without a key}
    history: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "trigger": self.trigger,
            "cutoff": self.cutoff.isoformat(),
            "window_days": RETENTION_WINDOW.days,
            "outcome": self.outcome,
            "failure": self.failure,
            "exempt_providers": sorted(EXEMPT_PROVIDERS),
            "tables": _sorted(self.tables),
            "protected": _sorted(self.protected),
            "history": _sorted(self.history),
        }

    def _count(self, table: str, provider: str, bucket: str, count: int) -> None:
        cell = self.tables.setdefault(table, {}).setdefault(provider, {"before": 0, "delete": 0, "keep": 0})
        cell[bucket] += count
        if bucket != "keep":
            cell["keep"] = cell["before"] - cell["delete"]

    def _protect(self, table: str, provider: str, reason: str, count: int = 1) -> None:
        by_reason = self.protected.setdefault(table, {}).setdefault(provider, {})
        by_reason[reason] = by_reason.get(reason, 0) + count


def _sorted(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted(value[key]) for key in sorted(value)}
    return value


class ProjectionArchiveRetention:
    """Prune the projection archive to its window, recording line history first."""

    def __init__(
        self,
        engine: Engine,
        *,
        event_reader: Any,
        clock: Callable[[], datetime] = utc_now,
        batch_size: int = DEFAULT_BATCH_SIZE,
        snapshot_batch_size: int = DEFAULT_SNAPSHOT_BATCH_SIZE,
        statistic_catalog: StatisticCatalog | None = None,
    ) -> None:
        if batch_size < 1 or snapshot_batch_size < 1:
            raise ValueError("retention batch sizes must be positive")
        self.engine = engine
        self.event_reader = event_reader
        self.clock = clock
        self.batch_size = batch_size
        self.snapshot_batch_size = snapshot_batch_size
        # Only for its scope lock: deletes serialize with the collector there.
        self.archive = ProjectionArchive(engine, statistic_catalog or StatisticCatalog.load_default())

    # -- entry points --------------------------------------------------------

    def run(self, *, dry_run: bool, trigger: str = "manual") -> RetentionReport:
        """Derive history, then prune; a dry run reports the same and writes nothing."""

        now = assume_utc(self.clock())
        cutoff = now - RETENTION_WINDOW
        report = RetentionReport(mode="dry_run" if dry_run else "run", trigger=trigger, cutoff=cutoff)
        with self._run_lock() as acquired:
            if not acquired:
                report.outcome = "failed"
                report.failure = {"reason": "another retention run holds the lock"}
                logger.error("projection archive retention refused: %s", report.failure["reason"])
                return report
            run_id = None if dry_run else self._record_start(trigger, now, cutoff)
            try:
                self._count_before(report)
                scopes = self._scopes()
                self._derive_history(
                    sorted({scope.provider for scope in scopes}), cutoff, report, write=not dry_run
                )
                for scope in scopes:
                    self._prune_scope(scope, now, cutoff, report, write=not dry_run)
                report.outcome = "succeeded"
            except RetentionSafetyViolation as violation:
                report.outcome = "failed"
                report.failure = {"reason": str(violation)}
                logger.error("projection archive retention rolled back a batch: %s", violation)
            except Exception as error:
                report.outcome = "failed"
                report.failure = {"reason": type(error).__name__}
                logger.exception("projection archive retention failed; the current batch rolled back")
            finally:
                if run_id is not None:
                    self._record_finish(run_id, report)
        return report

    def run_nightly(self) -> RetentionReport:
        """The nightly run, which waits for the owner's first manual real run."""

        with self.engine.connect() as connection:
            approved = connection.execute(
                select(func.count())
                .select_from(_RUNS)
                .where(_RUNS.c.trigger == "manual", _RUNS.c.outcome == "succeeded")
            ).scalar_one()
        if not approved:
            report = RetentionReport(
                mode="run",
                trigger="nightly",
                cutoff=assume_utc(self.clock()) - RETENTION_WINDOW,
                outcome="skipped",
                failure={"reason": "no manual run has succeeded yet"},
            )
            logger.info("projection archive retention skipped: no manual run has succeeded yet")
            return report
        return self.run(dry_run=False, trigger="nightly")

    # -- run bookkeeping -----------------------------------------------------

    @contextmanager
    def _run_lock(self) -> Iterator[bool]:
        if self.engine.dialect.name != "postgresql":
            yield True
            return
        with self.engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": RETENTION_ADVISORY_LOCK_ID},
                ).scalar()
            )
            connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": RETENTION_ADVISORY_LOCK_ID},
                    )
                    connection.commit()

    def _record_start(self, trigger: str, now: datetime, cutoff: datetime) -> int:
        with self.engine.begin() as connection:
            return int(
                connection.execute(
                    insert(_RUNS)
                    .values(trigger=trigger, started_at=now, cutoff=cutoff, outcome="running")
                    .returning(_RUNS.c.run_id)
                ).scalar_one()
            )

    def _record_finish(self, run_id: int, report: RetentionReport) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(_RUNS)
                .where(_RUNS.c.run_id == run_id)
                .values(
                    finished_at=assume_utc(self.clock()),
                    outcome=report.outcome,
                    report=json.dumps(report.as_dict(), sort_keys=True),
                )
            )

    def _count_before(self, report: RetentionReport) -> None:
        with self.engine.connect() as connection:
            for name, table in _TABLES:
                for provider, count in connection.execute(
                    select(table.c.provider, func.count()).group_by(table.c.provider)
                ):
                    report._count(name, str(provider), "before", int(count))

    def _scopes(self) -> list[_Scope]:
        scopes: set[_Scope] = set()
        with self.engine.connect() as connection:
            for table in (_GEN, _POLL, _SNAP):
                for provider, season, query_key in connection.execute(
                    select(table.c.provider, table.c.season, table.c.query_key).distinct()
                ):
                    if str(provider) not in EXEMPT_PROVIDERS:
                        scopes.add(_Scope(str(provider), str(season), str(query_key)))
        return sorted(scopes, key=lambda scope: (scope.provider, scope.season, scope.query_key))

    # -- line history --------------------------------------------------------

    def _derive_history(
        self,
        providers: Iterable[str],
        cutoff: datetime,
        report: RetentionReport,
        *,
        write: bool,
    ) -> None:
        """Record each key's changes among rows older than the window, in time order.

        A key's rows at or before its last recorded row were already judged
        (by this run or an earlier one), so a rerun, or a row kept past the
        window, writes nothing new.
        """

        columns = [_OBS.c[name] for name in (*_HISTORY_KEY, *_HISTORY_VALUES)]
        for provider in providers:
            tally = report.history.setdefault(provider, {"rows": 0, "unkeyed": 0})
            last: dict[tuple[Any, ...], tuple[datetime, tuple[Any, ...]]] = {}
            cursor: tuple[Any, Any] | None = None
            while True:
                with self.engine.connect() as connection:
                    rows = (
                        connection.execute(
                            select(
                                _OBS.c.observation_id, _OBS.c.observed_at, _OBS.c.canonical_game_id, *columns
                            )
                            .select_from(_OBS.join(_GEN, _GEN.c.generation_id == _OBS.c.generation_id))
                            .where(
                                _OBS.c.provider == provider,
                                *_expired(cutoff),
                                *_after(cursor),
                            )
                            .order_by(_OBS.c.observed_at, _OBS.c.observation_id)
                            .limit(self.batch_size)
                        )
                        .mappings()
                        .all()
                    )
                    if not rows:
                        break
                    cursor = (rows[-1]["observed_at"], rows[-1]["observation_id"])
                    keyed = [row for row in rows if all(row[name] is not None for name in _HISTORY_KEY)]
                    tally["unkeyed"] += len(rows) - len(keyed)
                    unseen = {_history_key(row) for row in keyed} - last.keys()
                    if unseen:
                        last.update(self._last_history(connection, provider, unseen))
                changes = []
                for row in keyed:
                    key = _history_key(row)
                    observed_at = assume_utc(row["observed_at"])
                    values = tuple(row[name] for name in _HISTORY_VALUES)
                    prior = last.get(key)
                    if prior is not None and observed_at <= prior[0]:
                        continue
                    if prior is None or prior[1] != values:
                        changes.append(
                            {
                                **dict(zip(_HISTORY_KEY, key, strict=True)),
                                "canonical_game_id": row["canonical_game_id"],
                                "observed_at": observed_at,
                                **dict(zip(_HISTORY_VALUES, values, strict=True)),
                            }
                        )
                    last[key] = (observed_at, values)
                if changes and write:
                    with self.engine.begin() as connection:
                        connection.execute(insert(_HISTORY), changes)
                tally["rows"] += len(changes)

    @staticmethod
    def _last_history(
        connection: Any,
        provider: str,
        keys: set[tuple[Any, ...]],
    ) -> dict[tuple[Any, ...], tuple[datetime, tuple[Any, ...]]]:
        found: dict[tuple[Any, ...], tuple[datetime, tuple[Any, ...]]] = {}
        market_ids = sorted({key[1] for key in keys})
        for start in range(0, len(market_ids), 1000):
            for row in connection.execute(
                select(_HISTORY).where(
                    _HISTORY.c.provider == provider,
                    _HISTORY.c.provider_market_id.in_(market_ids[start : start + 1000]),
                )
            ).mappings():
                key = _history_key(row)
                if key not in keys:
                    continue
                observed_at = assume_utc(row["observed_at"])
                if key not in found or observed_at > found[key][0]:
                    found[key] = (observed_at, tuple(row[name] for name in _HISTORY_VALUES))
        return found

    # -- pruning -------------------------------------------------------------

    @contextmanager
    def _batch(self, scope: _Scope, *, write: bool) -> Iterator[Any]:
        """A delete batch under the collector's scope lock, or a read for a dry run."""

        if not write:
            with self.engine.connect() as connection:
                yield connection
            return
        with self.archive._scope_transaction(scope.provider, scope.season, scope.query_key) as connection:
            yield connection

    def _prune_scope(
        self,
        scope: _Scope,
        now: datetime,
        cutoff: datetime,
        report: RetentionReport,
        *,
        write: bool,
    ) -> None:
        with self.engine.connect() as connection:
            rules = self._replay_rules(connection, scope, now, cutoff)
        kept_refs: set[tuple[str, str, str]] = set()
        cursor: tuple[Any, Any] | None = None
        while True:
            with self._batch(scope, write=write) as connection:
                rows = (
                    connection.execute(
                        select(
                            _OBS.c.observation_id,
                            _OBS.c.observed_at,
                            _OBS.c.generation_id,
                            _OBS.c.snapshot_id,
                            _OBS.c.source_poll_id,
                            _OBS.c.canonical_game_id,
                            _OBS.c.targetable,
                        )
                        .select_from(_OBS.join(_GEN, _GEN.c.generation_id == _OBS.c.generation_id))
                        .where(
                            _OBS.c.provider == scope.provider,
                            *_in_scope(_GEN, scope),
                            *_expired(cutoff),
                            *_after(cursor),
                        )
                        .order_by(_OBS.c.observed_at, _OBS.c.observation_id)
                        .limit(self.batch_size)
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                cursor = (rows[-1]["observed_at"], rows[-1]["observation_id"])
                ids = [str(row["observation_id"]) for row in rows]
                in_latest = _ids_in(connection, _LATEST.c.observation_id, ids)
                in_closing = _ids_in(connection, _MEMBER.c.observation_id, ids)
                current = _current(connection, scope)
                active_snapshot = None if current is None else str(current["snapshot_id"])
                doomed = []
                for row in rows:
                    observation_id = str(row["observation_id"])
                    if observation_id in in_latest:
                        reason = "latest"
                    elif observation_id in in_closing:
                        reason = "closing_membership"
                    elif str(row["snapshot_id"]) == active_snapshot:
                        reason = "active_snapshot"
                    elif rules.needs(
                        row["canonical_game_id"], str(row["generation_id"]), bool(row["targetable"])
                    ):
                        reason = "closing_replay"
                    else:
                        doomed.append(observation_id)
                        continue
                    report._protect("projection_observations", scope.provider, reason)
                    kept_refs.add(
                        (str(row["generation_id"]), str(row["snapshot_id"]), str(row["source_poll_id"]))
                    )
                if write and doomed:
                    connection.execute(delete(_OBS).where(_OBS.c.observation_id.in_(doomed)))
                    _assert_pointers_resolve(connection, scope, current, observations=doomed)
                report._count("projection_observations", scope.provider, "delete", len(doomed))

        with self.engine.connect() as connection:
            plan = self._plan_parents(connection, scope, cutoff, kept_refs, rules)
        for table, reasons in plan.protected.items():
            for reason, count in reasons.items():
                report._protect(table, scope.provider, reason, count)
        for name, table, key, ids, size in (
            (
                "projection_materialization_generations",
                _GEN,
                _GEN.c.generation_id,
                plan.generations,
                self.batch_size,
            ),
            ("projection_provider_polls", _POLL, _POLL.c.poll_id, plan.polls, self.batch_size),
            (
                "projection_provider_snapshots",
                _SNAP,
                _SNAP.c.snapshot_id,
                plan.snapshots,
                self.snapshot_batch_size,
            ),
        ):
            if not write:
                report._count(name, scope.provider, "delete", len(ids))
                continue
            for start in range(0, len(ids), size):
                chunk = list(ids[start : start + size])
                with self._batch(scope, write=True) as connection:
                    current = _current(connection, scope)
                    deleted = [
                        str(value)
                        for value in connection.execute(
                            delete(table).where(key.in_(chunk), *_unreferenced(table, current)).returning(key)
                        ).scalars()
                    ]
                    _assert_pointers_resolve(
                        connection,
                        scope,
                        current,
                        generations=deleted if table is _GEN else (),
                    )
                report._count(name, scope.provider, "delete", len(deleted))

    def _replay_rules(self, connection: Any, scope: _Scope, now: datetime, cutoff: datetime) -> _ReplayRules:
        """What every closing replay of the scope reads: started games, then the rest."""

        games = sorted(
            str(game_id)
            for game_id in connection.execute(
                select(_OBS.c.canonical_game_id)
                .distinct()
                .select_from(_OBS.join(_GEN, _GEN.c.generation_id == _OBS.c.generation_id))
                .where(*_in_scope(_GEN, scope), *_expired(cutoff), _OBS.c.canonical_game_id.is_not(None))
            ).scalars()
        )
        started: dict[str, datetime] = {}
        if games:
            for event in self.event_reader.get_events_by_ids(scope.season, games):
                game_id = str(event.get("nba_game_id", ""))
                fence = _projection_closing_start_fence(event)
                if game_id in games and fence is not None:
                    started[game_id] = fence
        return _ReplayRules(
            by_game={
                game_id: self._replay_rule(connection, scope, fence) for game_id, fence in started.items()
            },
            future=self._replay_rule(connection, scope, now),
        )

    @staticmethod
    def _replay_rule(connection: Any, scope: _Scope, started_at: datetime) -> _ReplayRule:
        joined, replayed, baseline = ProjectionArchive._closing_replay_bounds(
            connection,
            provider=scope.provider,
            season=scope.season,
            query_key=scope.query_key,
            started_at=started_at,
        )
        generation_ids = frozenset(
            str(value)
            for value in connection.execute(
                select(_GEN.c.generation_id).select_from(joined).where(*replayed)
            ).scalars()
        )
        return _ReplayRule(
            baseline_id=None if baseline is None else str(baseline["generation_id"]),
            generation_ids=generation_ids,
        )

    def _plan_parents(
        self,
        connection: Any,
        scope: _Scope,
        cutoff: datetime,
        kept_refs: set[tuple[str, str, str]],
        rules: _ReplayRules,
    ) -> _ParentPlan:
        """Which generations, polls and snapshots outlive the kept observations."""

        generations = {
            str(row["generation_id"]): row
            for row in connection.execute(
                select(
                    _GEN.c.generation_id, _GEN.c.snapshot_id, _GEN.c.source_poll_id, _GEN.c.created_at
                ).where(*_in_scope(_GEN, scope))
            ).mappings()
        }
        polls = {
            str(row["poll_id"]): row
            for row in connection.execute(
                select(
                    _POLL.c.poll_id, _POLL.c.snapshot_id, _POLL.c.generation_id, _POLL.c.completed_at
                ).where(*_in_scope(_POLL, scope))
            ).mappings()
        }
        snapshots = {
            str(row["snapshot_id"]): row
            for row in connection.execute(
                select(_SNAP.c.snapshot_id, _SNAP.c.retrieved_at).where(*_in_scope(_SNAP, scope))
            ).mappings()
        }
        expired_generations = [
            key for key, row in generations.items() if assume_utc(row["created_at"]) < cutoff
        ]
        # Rows the window keeps that point outside their own generation's
        # snapshot or poll (a mapping replay copies its source row's).
        refs = set(kept_refs)
        refs.update(
            (str(generation_id), str(snapshot_id), str(poll_id))
            for generation_id, snapshot_id, poll_id in connection.execute(
                select(_OBS.c.generation_id, _OBS.c.snapshot_id, _OBS.c.source_poll_id)
                .distinct()
                .select_from(_OBS.join(_GEN, _GEN.c.generation_id == _OBS.c.generation_id))
                .where(
                    *_in_scope(_GEN, scope),
                    or_(_OBS.c.observed_at >= cutoff, _GEN.c.created_at >= cutoff),
                    or_(
                        _GEN.c.created_at < cutoff,
                        _OBS.c.snapshot_id != _GEN.c.snapshot_id,
                        _OBS.c.source_poll_id != _GEN.c.source_poll_id,
                    ),
                )
            )
        )
        current = _current(connection, scope)
        latest_generations = {
            str(value)
            for value in connection.execute(
                select(_LATEST.c.generation_id).distinct().where(*_in_scope(_LATEST, scope))
            ).scalars()
        }
        closing_generations = _ids_in(connection, _MEMBER.c.generation_id, expired_generations)
        baselines = rules.baselines

        protected: dict[str, dict[str, int]] = {}

        def protect(table: str, reason: str) -> None:
            protected.setdefault(table, {})
            protected[table][reason] = protected[table].get(reason, 0) + 1

        keep_generations: set[str] = set()
        for key in expired_generations:
            if current is not None and key == str(current["generation_id"]):
                reason = "active"
            elif key in latest_generations:
                reason = "latest"
            elif key in closing_generations:
                reason = "closing_membership"
            elif key in baselines:
                reason = "closing_replay"
            else:
                continue
            keep_generations.add(key)
            protect("projection_materialization_generations", reason)
        keep_generations.update(
            key for key, row in generations.items() if assume_utc(row["created_at"]) >= cutoff
        )

        keep_polls = {key for key, row in polls.items() if assume_utc(row["completed_at"]) >= cutoff}
        for kind in ("promoted", "failed"):
            health = _newest_poll(connection, scope, kind)
            if health is not None and health not in keep_polls:
                keep_polls.add(health)
                protect("projection_provider_polls", "provider_health")

        keep_snapshots = {key for key, row in snapshots.items() if assume_utc(row["retrieved_at"]) >= cutoff}
        if current is not None and str(current["snapshot_id"]) not in keep_snapshots:
            keep_snapshots.add(str(current["snapshot_id"]))
            protect("projection_provider_snapshots", "active")

        referenced = {
            "projection_materialization_generations": set(),
            "projection_provider_polls": set(),
            "projection_provider_snapshots": set(),
        }

        def reference(table: str, keep: set[str], key: str | None, known: Mapping[str, Any]) -> bool:
            if key is None or key in keep or key not in known:
                return False
            keep.add(key)
            referenced[table].add(key)
            return True

        for generation_id, snapshot_id, poll_id in refs:
            reference("projection_materialization_generations", keep_generations, generation_id, generations)
            reference("projection_provider_snapshots", keep_snapshots, snapshot_id, snapshots)
            reference("projection_provider_polls", keep_polls, poll_id, polls)
        changed = True
        while changed:
            changed = False
            for key in tuple(keep_generations):
                row = generations[key]
                changed |= reference(
                    "projection_provider_polls", keep_polls, str(row["source_poll_id"]), polls
                )
                reference("projection_provider_snapshots", keep_snapshots, str(row["snapshot_id"]), snapshots)
            for key in tuple(keep_polls):
                row = polls[key]
                snapshot_id = row["snapshot_id"]
                reference(
                    "projection_provider_snapshots",
                    keep_snapshots,
                    None if snapshot_id is None else str(snapshot_id),
                    snapshots,
                )
                generation_id = row["generation_id"]
                changed |= reference(
                    "projection_materialization_generations",
                    keep_generations,
                    None if generation_id is None else str(generation_id),
                    generations,
                )
        for table, keys in referenced.items():
            if keys:
                protected.setdefault(table, {})["referenced"] = len(keys)

        return _ParentPlan(
            generations=tuple(
                sorted(
                    (key for key in generations if key not in keep_generations),
                    key=lambda key: (assume_utc(generations[key]["created_at"]), key),
                )
            ),
            polls=tuple(
                sorted(
                    (key for key in polls if key not in keep_polls),
                    key=lambda key: (assume_utc(polls[key]["completed_at"]), key),
                )
            ),
            snapshots=tuple(
                sorted(
                    (key for key in snapshots if key not in keep_snapshots),
                    key=lambda key: (assume_utc(snapshots[key]["retrieved_at"]), key),
                )
            ),
            protected=protected,
        )


def _history_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[name] for name in _HISTORY_KEY)


def _in_scope(table: Any, scope: _Scope) -> tuple[Any, ...]:
    return (
        table.c.provider == scope.provider,
        table.c.season == scope.season,
        table.c.query_key == scope.query_key,
    )


def _expired(cutoff: datetime) -> tuple[Any, ...]:
    """An observation, and its generation, both older than the window."""

    return (_OBS.c.observed_at < cutoff, _GEN.c.created_at < cutoff)


def _after(cursor: tuple[Any, Any] | None) -> tuple[Any, ...]:
    if cursor is None:
        return ()
    observed_at, observation_id = cursor
    return (
        or_(
            _OBS.c.observed_at > observed_at,
            and_(_OBS.c.observed_at == observed_at, _OBS.c.observation_id > observation_id),
        ),
    )


def _ids_in(connection: Any, column: Any, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(ids), 1000):
        found.update(
            str(value)
            for value in connection.execute(
                select(column).distinct().where(column.in_(ids[start : start + 1000]))
            ).scalars()
        )
    return found


def _current(connection: Any, scope: _Scope) -> Any | None:
    """The scope's active generation and snapshot, as the write path reads them."""

    return ProjectionArchive._current_materialization(
        connection, provider=scope.provider, season=scope.season, query_key=scope.query_key
    )


def _newest_poll(connection: Any, scope: _Scope, kind: str) -> str | None:
    condition = (
        _POLL.c.promoted.is_(True)
        if kind == "promoted"
        else _POLL.c.outcome == ProviderPollOutcome.FAILED.value
    )
    value = connection.execute(
        select(_POLL.c.poll_id)
        .where(*_in_scope(_POLL, scope), condition)
        .order_by(*newest_poll_first(_POLL))
        .limit(1)
    ).scalar_one_or_none()
    return None if value is None else str(value)


def _unreferenced(table: Any, current: Any | None) -> tuple[Any, ...]:
    """Re-check at delete time that nothing still points at a planned row."""

    if table is _GEN:
        key = _GEN.c.generation_id
        guards = [
            ~select(_OBS.c.observation_id).where(_OBS.c.generation_id == key).exists(),
            ~select(_LATEST.c.generation_id).where(_LATEST.c.generation_id == key).exists(),
            ~select(_MEMBER.c.generation_id).where(_MEMBER.c.generation_id == key).exists(),
        ]
        if current is not None:
            guards.append(key != str(current["generation_id"]))
        return tuple(guards)
    if table is _POLL:
        key = _POLL.c.poll_id
        return (
            ~select(_GEN.c.generation_id).where(_GEN.c.source_poll_id == key).exists(),
            ~select(_OBS.c.observation_id).where(_OBS.c.source_poll_id == key).exists(),
        )
    key = _SNAP.c.snapshot_id
    guards = [
        ~select(_OBS.c.observation_id).where(_OBS.c.snapshot_id == key).exists(),
        ~select(_GEN.c.generation_id).where(_GEN.c.snapshot_id == key).exists(),
        ~select(_POLL.c.poll_id).where(_POLL.c.snapshot_id == key).exists(),
    ]
    if current is not None:
        guards.append(key != str(current["snapshot_id"]))
    return tuple(guards)


def _assert_pointers_resolve(
    connection: Any,
    scope: _Scope,
    current: Any | None,
    *,
    observations: Iterable[str] = (),
    generations: Iterable[str] = (),
) -> None:
    """Before a batch commits: Latest, closing memberships and the active snapshot resolve.

    Every pointer resolved when the batch began (the foreign keys guarantee it
    on PostgreSQL), so a closing membership can stop resolving only by naming
    a row this batch deleted; Latest and the active generation are checked
    whole.
    """

    dangling_latest = connection.execute(
        select(func.count())
        .select_from(_LATEST)
        .where(
            *_in_scope(_LATEST, scope),
            or_(
                ~select(_OBS.c.observation_id)
                .where(_OBS.c.observation_id == _LATEST.c.observation_id)
                .exists(),
                ~select(_GEN.c.generation_id).where(_GEN.c.generation_id == _LATEST.c.generation_id).exists(),
            ),
        )
    ).scalar_one()
    if dangling_latest:
        raise RetentionSafetyViolation(f"{scope.label}: {dangling_latest} Latest pointers would not resolve")
    observations = list(observations)
    generations = list(generations)
    dangling_closing = 0
    for column, ids in ((_MEMBER.c.observation_id, observations), (_MEMBER.c.generation_id, generations)):
        for start in range(0, len(ids), 1000):
            dangling_closing += connection.execute(
                select(func.count()).select_from(_MEMBER).where(column.in_(ids[start : start + 1000]))
            ).scalar_one()
    if dangling_closing:
        raise RetentionSafetyViolation(
            f"{scope.label}: {dangling_closing} closing memberships would not resolve"
        )
    if current is not None and _current(connection, scope) is None:
        raise RetentionSafetyViolation(f"{scope.label}: the active generation's snapshot would not resolve")


__all__ = [
    "EXEMPT_PROVIDERS",
    "RETENTION_WINDOW",
    "ProjectionArchiveRetention",
    "RetentionReport",
    "RetentionSafetyViolation",
]
