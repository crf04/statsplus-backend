"""Projection archive retention: three days of full evidence, a change-only line history.

``ProjectionArchiveRetention.run`` first brings ``projection_line_history``
up to date (``app.services.projection_line_history``; the first run derives it
from every row of the archive, whatever its age, and records the marker the
nightly run requires), then deletes the rows no reader needs, scope by scope,
in foreign-key order (observations, generations, polls, snapshots) and in
short batches.  Each delete batch holds the scope lock the collector writes
under and, before it commits, checks that every Latest pointer, every closing
membership and the active generation pointer still resolve; a violation rolls
that batch back and ends the run.

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
* each scope's newest Complete board whole, which the next run's withdrawal
  history compares against;
* each scope's newest promoted and newest failed poll, which provider health
  and the write path's promotion fences read;
* every generation, snapshot and poll a kept row references.

``prizepicks`` is exempt until it writes Complete baselines.  A dry run reads
the same plan, including the evidence bytes kept by reason, and writes nothing.
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
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProjectionRetentionRun,
    ProviderPoll,
    ProviderPollOutcome,
)
from app.providers.dfs import SnapshotStatus
from app.services.projection_archive import (
    ProjectionArchive,
    _projection_closing_start_fence,
    newest_poll_first,
)
from app.services.projection_line_history import LineHistory
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
#: A run re-reads generations created this long before the last history
#: marker, so one whose transaction committed late is never skipped.
HISTORY_OVERLAP = timedelta(hours=6)

_OBS = ProjectionObservation.__table__
_GEN = ProjectionMaterializationGeneration.__table__
_POLL = ProviderPoll.__table__
_SNAP = ProjectionProviderSnapshot.__table__
_LATEST = LatestPlayerProjection.__table__
_MEMBER = ClosingProjectionMembership.__table__
_LOCK = ProjectionArchiveScopeLock.__table__
_RUNS = ProjectionRetentionRun.__table__

_TABLES = (
    ("projection_observations", _OBS),
    ("projection_materialization_generations", _GEN),
    ("projection_provider_polls", _POLL),
    ("projection_provider_snapshots", _SNAP),
)
_GENERATIONS = "projection_materialization_generations"
_POLLS = "projection_provider_polls"
_SNAPSHOTS = "projection_provider_snapshots"
#: Keep reasons in the order attribution prefers them.
_REASONS = (
    "active",
    "latest",
    "closing_membership",
    "closing_replay",
    "history_board",
    "active_snapshot",
    "provider_health",
    "window",
)


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
    #: Evidence document bytes: of the snapshots to delete, and of the old
    #: snapshots kept, by keep reason.
    delete_bytes: int
    kept_bytes: Mapping[str, int]


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
    #: provider -> {"rows", "removed", "withdrawals", "unkeyed"}
    history: dict[str, dict[str, int]] = field(default_factory=dict)
    #: provider -> {"before", "delete", "keep", "kept_old": {reason: bytes}}
    evidence_bytes: dict[str, dict[str, Any]] = field(default_factory=dict)

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
            "evidence_bytes": _sorted(self.evidence_bytes),
        }

    def _count(self, table: str, provider: str, bucket: str, count: int) -> None:
        cell = self.tables.setdefault(table, {}).setdefault(provider, {"before": 0, "delete": 0, "keep": 0})
        cell[bucket] += count
        if bucket != "keep":
            cell["keep"] = cell["before"] - cell["delete"]

    def _protect(self, table: str, provider: str, reason: str, count: int = 1) -> None:
        by_reason = self.protected.setdefault(table, {}).setdefault(provider, {})
        by_reason[reason] = by_reason.get(reason, 0) + count

    def _bytes(self, provider: str) -> dict[str, Any]:
        return self.evidence_bytes.setdefault(provider, {"before": 0, "delete": 0, "keep": 0, "kept_old": {}})


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
        self.line_history = LineHistory(engine, batch_size=batch_size)
        # Only for its scope lock: deletes serialize with the collector there.
        self.archive = ProjectionArchive(engine, statistic_catalog or StatisticCatalog.load_default())

    # -- entry points --------------------------------------------------------

    def run(self, *, dry_run: bool, trigger: str = "manual") -> RetentionReport:
        """Bring history up to date, then prune; a dry run reports the same and writes nothing."""

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
                since = self._history_since()
                for provider in sorted({scope.provider for scope in scopes}):
                    report.history[provider] = self.line_history.derive(provider, since, write=not dry_run)
                if run_id is not None:
                    # Durable before any delete: the backfill (or catch-up) is complete.
                    self._record_history_through(run_id, now)
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
        """The nightly run, which waits for the owner's first manual run and its backfill."""

        with self.engine.connect() as connection:
            approved = connection.execute(
                select(func.count())
                .select_from(_RUNS)
                .where(
                    _RUNS.c.trigger == "manual",
                    _RUNS.c.outcome == "succeeded",
                    _RUNS.c.history_through.is_not(None),
                )
            ).scalar_one()
        if not approved:
            report = RetentionReport(
                mode="run",
                trigger="nightly",
                cutoff=assume_utc(self.clock()) - RETENTION_WINDOW,
                outcome="skipped",
                failure={"reason": "no manual run has backfilled history and succeeded yet"},
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

    def _record_history_through(self, run_id: int, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(update(_RUNS).where(_RUNS.c.run_id == run_id).values(history_through=now))

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

    def _history_since(self) -> datetime | None:
        """Where history needs new evidence from; ``None`` until a backfill completes."""

        with self.engine.connect() as connection:
            marker = connection.execute(select(func.max(_RUNS.c.history_through))).scalar_one()
        return None if marker is None else assume_utc(marker) - HISTORY_OVERLAP

    def _evidence_size(self) -> Any:
        if self.engine.dialect.name == "postgresql":
            return func.pg_column_size(_SNAP.c.evidence_document)
        return func.length(_SNAP.c.evidence_document)

    def _count_before(self, report: RetentionReport) -> None:
        with self.engine.connect() as connection:
            for name, table in _TABLES:
                for provider, count in connection.execute(
                    select(table.c.provider, func.count()).group_by(table.c.provider)
                ):
                    report._count(name, str(provider), "before", int(count))
            for provider, size in connection.execute(
                select(_SNAP.c.provider, func.sum(self._evidence_size())).group_by(_SNAP.c.provider)
            ):
                cell = report._bytes(str(provider))
                cell["before"] = cell["keep"] = int(size or 0)

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
            _assert_active_pointer_resolves(connection, scope)
            rules = self._replay_rules(connection, scope, now, cutoff)
            history_board = _newest_complete_board(connection, scope)
        kept_refs: dict[tuple[str, str, str], str] = {}
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
                    elif str(row["generation_id"]) == history_board:
                        reason = "history_board"
                    else:
                        doomed.append(observation_id)
                        continue
                    report._protect("projection_observations", scope.provider, reason)
                    refs = (str(row["generation_id"]), str(row["snapshot_id"]), str(row["source_poll_id"]))
                    kept_refs.setdefault(refs, reason)
                if write and doomed:
                    connection.execute(delete(_OBS).where(_OBS.c.observation_id.in_(doomed)))
                    _assert_pointers_resolve(connection, scope, current, observations=doomed)
                report._count("projection_observations", scope.provider, "delete", len(doomed))

        with self.engine.connect() as connection:
            plan = self._plan_parents(connection, scope, cutoff, kept_refs, rules, history_board)
        for table, reasons in plan.protected.items():
            for reason, count in reasons.items():
                report._protect(table, scope.provider, reason, count)
        evidence = report._bytes(scope.provider)
        for reason, size in plan.kept_bytes.items():
            evidence["kept_old"][reason] = evidence["kept_old"].get(reason, 0) + size
        if not write:
            evidence["delete"] += plan.delete_bytes
            evidence["keep"] = evidence["before"] - evidence["delete"]
        for name, table, key, ids, size in (
            (_GENERATIONS, _GEN, _GEN.c.generation_id, plan.generations, self.batch_size),
            (_POLLS, _POLL, _POLL.c.poll_id, plan.polls, self.batch_size),
            (_SNAPSHOTS, _SNAP, _SNAP.c.snapshot_id, plan.snapshots, self.snapshot_batch_size),
        ):
            if not write:
                report._count(name, scope.provider, "delete", len(ids))
                continue
            for start in range(0, len(ids), size):
                chunk = list(ids[start : start + size])
                with self._batch(scope, write=True) as connection:
                    current = _current(connection, scope)
                    returned = [key]
                    if table is _SNAP:
                        returned.append(self._evidence_size())
                    deleted = connection.execute(
                        delete(table)
                        .where(key.in_(chunk), *_unreferenced(table, current))
                        .returning(*returned)
                    ).all()
                    _assert_pointers_resolve(
                        connection,
                        scope,
                        current,
                        generations=[str(row[0]) for row in deleted] if table is _GEN else (),
                    )
                report._count(name, scope.provider, "delete", len(deleted))
                if table is _SNAP:
                    evidence["delete"] += sum(int(row[1] or 0) for row in deleted)
                    evidence["keep"] = evidence["before"] - evidence["delete"]

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
        kept_refs: Mapping[tuple[str, str, str], str],
        rules: _ReplayRules,
        history_board: str | None,
    ) -> _ParentPlan:
        """Which generations, polls and snapshots outlive the kept observations, and why."""

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
                select(_SNAP.c.snapshot_id, _SNAP.c.retrieved_at, self._evidence_size().label("size")).where(
                    *_in_scope(_SNAP, scope)
                )
            ).mappings()
        }
        expired_generations = [
            key for key, row in generations.items() if assume_utc(row["created_at"]) < cutoff
        ]
        current = _current(connection, scope)
        latest_generations = {
            str(value)
            for value in connection.execute(
                select(_LATEST.c.generation_id).distinct().where(*_in_scope(_LATEST, scope))
            ).scalars()
        }
        closing_generations = _ids_in(connection, _MEMBER.c.generation_id, expired_generations)
        baselines = rules.baselines
        # Rows the window keeps that point outside their own generation's
        # snapshot or poll (a mapping replay copies its source row's).
        window_refs = {
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
        }

        # Seeds per keep reason, in attribution order.
        seeds: dict[str, dict[str, set[str]]] = {
            reason: {_GENERATIONS: set(), _POLLS: set(), _SNAPSHOTS: set()} for reason in _REASONS
        }
        for key in expired_generations:
            if current is not None and key == str(current["generation_id"]):
                seeds["active"][_GENERATIONS].add(key)
            if key in latest_generations:
                seeds["latest"][_GENERATIONS].add(key)
            if key in closing_generations:
                seeds["closing_membership"][_GENERATIONS].add(key)
            if key in baselines:
                seeds["closing_replay"][_GENERATIONS].add(key)
            if key == history_board:
                seeds["history_board"][_GENERATIONS].add(key)
        if current is not None:
            seeds["active"][_SNAPSHOTS].add(str(current["snapshot_id"]))
        for (generation_id, snapshot_id, poll_id), reason in kept_refs.items():
            seeds[reason][_GENERATIONS].add(generation_id)
            seeds[reason][_SNAPSHOTS].add(snapshot_id)
            seeds[reason][_POLLS].add(poll_id)
        for kind in ("promoted", "failed"):
            health = _newest_poll(connection, scope, kind)
            if health is not None:
                seeds["provider_health"][_POLLS].add(health)
        window = seeds["window"]
        window[_GENERATIONS].update(
            k for k, r in generations.items() if assume_utc(r["created_at"]) >= cutoff
        )
        window[_POLLS].update(k for k, r in polls.items() if assume_utc(r["completed_at"]) >= cutoff)
        window[_SNAPSHOTS].update(k for k, r in snapshots.items() if assume_utc(r["retrieved_at"]) >= cutoff)
        for generation_id, snapshot_id, poll_id in window_refs:
            window[_GENERATIONS].add(generation_id)
            window[_SNAPSHOTS].add(snapshot_id)
            window[_POLLS].add(poll_id)

        known = {_GENERATIONS: generations, _POLLS: polls, _SNAPSHOTS: snapshots}
        kept: dict[str, dict[str, str]] = {_GENERATIONS: {}, _POLLS: {}, _SNAPSHOTS: {}}
        for reason in _REASONS:
            frontier = [(table, key) for table, keys in seeds[reason].items() for key in sorted(keys)]
            while frontier:
                table, key = frontier.pop()
                if key not in known[table] or key in kept[table]:
                    continue
                kept[table][key] = reason
                row = known[table][key]
                if table == _GENERATIONS:
                    frontier.append((_POLLS, str(row["source_poll_id"])))
                    frontier.append((_SNAPSHOTS, str(row["snapshot_id"])))
                elif table == _POLLS:
                    if row["snapshot_id"] is not None:
                        frontier.append((_SNAPSHOTS, str(row["snapshot_id"])))
                    if row["generation_id"] is not None:
                        frontier.append((_GENERATIONS, str(row["generation_id"])))

        old = {
            _GENERATIONS: lambda row: assume_utc(row["created_at"]) < cutoff,
            _POLLS: lambda row: assume_utc(row["completed_at"]) < cutoff,
            _SNAPSHOTS: lambda row: assume_utc(row["retrieved_at"]) < cutoff,
        }
        protected: dict[str, dict[str, int]] = {}
        kept_bytes: dict[str, int] = {}
        for table, by_key in kept.items():
            for key, reason in by_key.items():
                row = known[table][key]
                if not old[table](row):
                    continue
                protected.setdefault(table, {})
                protected[table][reason] = protected[table].get(reason, 0) + 1
                if table == _SNAPSHOTS:
                    kept_bytes[reason] = kept_bytes.get(reason, 0) + int(row["size"] or 0)

        def doomed(table: str, at: str) -> tuple[str, ...]:
            return tuple(
                sorted(
                    (key for key in known[table] if key not in kept[table]),
                    key=lambda key: (assume_utc(known[table][key][at]), key),
                )
            )

        doomed_snapshots = doomed(_SNAPSHOTS, "retrieved_at")
        return _ParentPlan(
            generations=doomed(_GENERATIONS, "created_at"),
            polls=doomed(_POLLS, "completed_at"),
            snapshots=doomed_snapshots,
            protected=protected,
            delete_bytes=sum(int(snapshots[key]["size"] or 0) for key in doomed_snapshots),
            kept_bytes=kept_bytes,
        )


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


def _newest_complete_board(connection: Any, scope: _Scope) -> str | None:
    """The scope's newest Complete non-replay generation, which history compares the next one with."""

    value = connection.execute(
        select(_GEN.c.generation_id)
        .select_from(_GEN.join(_SNAP, _SNAP.c.snapshot_id == _GEN.c.snapshot_id))
        .where(
            *_in_scope(_GEN, scope),
            _GEN.c.is_replay.is_(False),
            _SNAP.c.snapshot_status == SnapshotStatus.COMPLETE.value,
        )
        .order_by(_GEN.c.retrieved_at.desc(), _GEN.c.created_at.desc(), _GEN.c.generation_id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return None if value is None else str(value)


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
            ~select(_LOCK.c.active_generation_id).where(_LOCK.c.active_generation_id == key).exists(),
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


def _assert_active_pointer_resolves(connection: Any, scope: _Scope) -> None:
    """A set active generation pointer must name a promoted generation whose snapshot exists.

    An absent pointer is the write path's fallback to the newest promoted
    generation; a pointer naming a missing generation or snapshot is damage
    no delete may build on.
    """

    pointer = connection.execute(
        select(_LOCK.c.active_generation_id).where(*_in_scope(_LOCK, scope))
    ).scalar_one_or_none()
    if pointer is None:
        return
    current = _current(connection, scope)
    if current is None or str(current["generation_id"]) != str(pointer):
        raise RetentionSafetyViolation(f"{scope.label}: the active generation pointer does not resolve")


def _assert_pointers_resolve(
    connection: Any,
    scope: _Scope,
    current: Any | None,
    *,
    observations: Iterable[str] = (),
    generations: Iterable[str] = (),
) -> None:
    """Before a batch commits: Latest, closing memberships and the active pointer resolve.

    Every closing membership resolved when the run began (the foreign keys
    guarantee it on PostgreSQL), so one can stop resolving only by naming a
    row this batch deleted; Latest and the active generation pointer are
    checked whole.
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
    _assert_active_pointer_resolves(connection, scope)
    if current is not None and _current(connection, scope) is None:
        raise RetentionSafetyViolation(f"{scope.label}: the active generation's snapshot would not resolve")


__all__ = [
    "EXEMPT_PROVIDERS",
    "RETENTION_WINDOW",
    "ProjectionArchiveRetention",
    "RetentionReport",
    "RetentionSafetyViolation",
]
