"""Scheduled, database-first collection of normalized DFS projections."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
import time
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.config.settings import ProjectionCollectionSettings
from app.domain.nba_events import is_final_event, is_postponed_event
from app.domain.utc import assume_utc
from app.models.projection_collection import (
    ProjectionCollectionLease,
    ProjectionCollectionProviderState,
)
from app.providers.dfs import NBAMarketQuery, ProviderSnapshot
from app.services.event_catalog_repository import EventCatalogRepository
from app.services.projection_archive import projection_query_key


LEASE_KEY = "projection"
_MAX_DIAGNOSTIC_SECONDS = 7 * 24 * 60 * 60
_SCHEDULED_STATUSES = frozenset(
    {
        "scheduled",
        "not started",
        "not_started",
        "pregame",
        "pre game",
        "pre-game",
        "upcoming",
        "delayed",
        "warmup",
        "warm-up",
    }
)
_LIVE_STATUS = re.compile(r"^(?:q[1-4]|ot\d*|halftime|in progress)\b")


@dataclass(frozen=True, slots=True)
class CollectionRunResult:
    """Bounded, operator-safe result of one one-shot collection attempt."""

    status: str
    reason: str
    providers: tuple[str, ...] = ()
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class _ScheduleDecision:
    interval: timedelta
    providers: tuple[str, ...]


class ProjectionCollectionCoordinator:
    """Coordinate one scheduled poll without owning a request-time path."""

    def __init__(
        self,
        engine: Engine,
        *,
        board_service: Any,
        recording_service: Any,
        event_reader: Callable[[str], Iterable[Mapping[str, Any]]] | None = None,
        season: str,
        settings: ProjectionCollectionSettings | None = None,
        providers: Iterable[str] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        owner: str = "projection-collector",
    ) -> None:
        self.engine = engine
        self.board_service = board_service
        self.recording_service = recording_service
        self.event_reader = event_reader or EventCatalogRepository(engine).list_events
        self.season = str(season)
        self.settings = settings or ProjectionCollectionSettings()
        configured = providers
        if configured is None:
            configured = getattr(board_service, "enabled_providers", ())
        self.providers = self._normalize_providers(configured)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.owner = owner.strip() or "projection-collector"

    @staticmethod
    def _normalize_providers(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(sorted(str(value).strip().casefold() for value in values if str(value).strip())))

    @staticmethod
    def _utc(value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return assume_utc(value)

    @classmethod
    def _event_started(cls, event: Mapping[str, Any]) -> bool:
        if is_postponed_event(event) or is_final_event(event):
            return True
        status_code = event.get("status_code")
        try:
            status_code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        if status_code in {2, 3}:
            return True
        status = str(event.get("status_text", event.get("status", ""))).strip().casefold()
        if not status or status in _SCHEDULED_STATUSES or status_code == 1:
            return False
        return status_code is not None or _LIVE_STATUS.match(status) is not None

    def _schedule(self, now: datetime, providers: tuple[str, ...]) -> _ScheduleDecision | None:
        events = []
        for event in self.event_reader(self.season):
            if self._event_started(event):
                continue
            scheduled_at = event.get("scheduled_at")
            if scheduled_at is None:
                continue
            scheduled = self._utc(scheduled_at)
            if (
                now - self.settings.pregame_horizon
                <= scheduled
                <= now + self.settings.pregame_horizon
            ):
                events.append(scheduled)
        if not events or not providers:
            return None
        earliest = min(events)
        if now < earliest - self.settings.pregame_horizon:
            return None
        interval = (
            self.settings.fast_interval
            if now >= earliest - self.settings.fast_window
            else self.settings.slow_interval
        )
        query = NBAMarketQuery(season=self.season)
        query_key = projection_query_key(query)
        due: list[str] = []
        state_table = ProjectionCollectionProviderState.__table__
        with self.engine.connect() as connection:
            rows = {
                row["provider"]: row
                for row in connection.execute(
                    select(state_table).where(
                        state_table.c.season == self.season,
                        state_table.c.query_key == query_key,
                        state_table.c.provider.in_(providers),
                    )
                ).mappings()
            }
        for provider in providers:
            row = rows.get(provider)
            if row is None or row["last_poll_at"] is None:
                due.append(provider)
                continue
            backoff_until = row["backoff_until"]
            if backoff_until is not None and self._utc(backoff_until) > now:
                continue
            if self._utc(row["last_poll_at"]) + interval <= now:
                due.append(provider)
        return _ScheduleDecision(interval=interval, providers=tuple(due)) if due else None

    @contextmanager
    def _transaction(self):
        if self.engine.dialect.name == "sqlite":
            connection = self.engine.connect()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
            finally:
                connection.close()
            return
        with self.engine.begin() as connection:
            yield connection

    def _database_now(self, connection: Any) -> datetime:
        return self._utc(
            connection.execute(select(func.current_timestamp())).scalar_one()
        )

    def _acquire_lease(self, _process_now: datetime | None = None) -> tuple[int, int] | None:
        table = ProjectionCollectionLease.__table__
        with self._transaction() as connection:
            now = self._database_now(connection)
            row = connection.execute(
                select(table).where(table.c.lease_key == LEASE_KEY).with_for_update()
            ).mappings().one_or_none()
            if row is None:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(table).values(
                                lease_key=LEASE_KEY,
                                owner=None,
                                lease_expires_at=now,
                                fence=0,
                                updated_at=now,
                            )
                        )
                except IntegrityError:
                    pass
                row = connection.execute(
                    select(table).where(table.c.lease_key == LEASE_KEY).with_for_update()
                ).mappings().one()
            expires = self._utc(row["lease_expires_at"])
            if row["owner"] and expires > now:
                return None
            fence = int(row["fence"] or 0) + 1
            connection.execute(
                update(table)
                .where(table.c.lease_key == LEASE_KEY)
                .values(
                    owner=self.owner,
                    lease_expires_at=now + self.settings.lease_duration,
                    fence=fence,
                    updated_at=now,
                )
            )
            return fence, int(math.ceil(self.settings.lease_duration.total_seconds()))

    def _renew_lease(self, fence: int) -> bool:
        table = ProjectionCollectionLease.__table__
        with self._transaction() as connection:
            now = self._database_now(connection)
            row = connection.execute(
                select(table)
                .where(table.c.lease_key == LEASE_KEY)
                .with_for_update()
            ).mappings().one_or_none()
            if (
                row is None
                or row["owner"] != self.owner
                or int(row["fence"]) != fence
                or self._utc(row["lease_expires_at"]) <= now
            ):
                return False
            connection.execute(
                update(table)
                .where(
                    table.c.lease_key == LEASE_KEY,
                    table.c.owner == self.owner,
                    table.c.fence == fence,
                )
                .values(
                    lease_expires_at=now + self.settings.lease_duration,
                    updated_at=now,
                )
            )
            return True

    def _release_lease(self, _process_now: datetime, fence: int) -> None:
        table = ProjectionCollectionLease.__table__
        with self._transaction() as connection:
            now = self._database_now(connection)
            connection.execute(
                update(table)
                .where(table.c.lease_key == LEASE_KEY, table.c.owner == self.owner, table.c.fence == fence)
                .values(owner=None, lease_expires_at=now, updated_at=now)
            )

    def _update_state(
        self,
        *,
        provider: str,
        interval: timedelta,
        success: bool,
        changed_at: datetime | None = None,
        failure_reason: str | None = None,
        counts: tuple[int, int] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        del duration_ms  # Poll duration is stored on ProviderPoll, not state.
        query_key = projection_query_key(NBAMarketQuery(season=self.season))
        table = ProjectionCollectionProviderState.__table__
        with self._transaction() as connection:
            now = self._database_now(connection)
            row = connection.execute(
                select(table).where(
                    table.c.provider == provider,
                    table.c.season == self.season,
                    table.c.query_key == query_key,
                ).with_for_update()
            ).mappings().one_or_none()
            previous_failures = int(row["consecutive_failures"] or 0) if row else 0
            if success:
                values = {
                    "last_poll_at": now,
                    "last_failure_at": None,
                    "last_failure_reason": None,
                    "consecutive_failures": 0,
                    "backoff_until": None,
                    "updated_at": now,
                }
                if changed_at is not None:
                    values["last_changed_at"] = changed_at
                if counts is not None:
                    values["active_count"], values["unresolved_count"] = counts
            else:
                failures = previous_failures + 1
                delay = max(
                    interval,
                    min(
                        self.settings.backoff_max,
                        self.settings.backoff_base * (2 ** min(failures - 1, 20)),
                    ),
                )
                values = {
                    "last_poll_at": now,
                    "last_failure_at": now,
                    "last_failure_reason": failure_reason,
                    "consecutive_failures": failures,
                    "backoff_until": now + delay,
                    "updated_at": now,
                }
            if row is None:
                initial_values = {
                    "active_count": 0,
                    "unresolved_count": 0,
                    **values,
                }
                connection.execute(
                    insert(table).values(
                        provider=provider,
                        season=self.season,
                        query_key=query_key,
                        **initial_values,
                    )
                )
            else:
                connection.execute(
                    update(table)
                    .where(
                        table.c.provider == provider,
                        table.c.season == self.season,
                        table.c.query_key == query_key,
                    )
                    .values(**values)
                )

    @staticmethod
    def _outcome_status(outcome: Any) -> str:
        status = getattr(outcome, "status", "failed")
        return str(getattr(status, "value", status)).casefold()

    @staticmethod
    def _failure_reason(outcome: Any) -> str:
        reason = getattr(outcome, "cache_failure_reason", None) or getattr(
            outcome, "reason", None
        )
        value = str(getattr(reason, "value", reason) or "upstream_error").casefold()
        return value if value.replace("_", "").isalnum() and len(value) <= 64 else "upstream_error"

    @staticmethod
    def _has_cache_failure(outcome: Any) -> bool:
        return bool(
            getattr(outcome, "cache_status", None) == "stale"
            or getattr(outcome, "cache_failure_reason", None)
        )

    @staticmethod
    def _snapshot_counts(snapshot: ProviderSnapshot) -> tuple[int, int]:
        active = len(snapshot.markets)
        unresolved = sum(
            1
            for market in snapshot.markets
            if market.event is None or market.athlete is None or market.statistic is None
        )
        return active, unresolved

    def _lease_lost_result(
        self,
        *,
        started: float,
        providers: Iterable[str],
    ) -> CollectionRunResult:
        return CollectionRunResult(
            "busy",
            "lease_lost",
            tuple(providers),
            max(0, int(round((self.monotonic() - started) * 1000))),
        )

    def run(self, *, providers: Iterable[str] | None = None) -> CollectionRunResult:
        started = self.monotonic()
        now = self._utc(self.clock())
        selected = self._normalize_providers(providers or self.providers)
        decision = self._schedule(now, selected)
        if decision is None:
            return CollectionRunResult("no_work", "not_due")
        lease = self._acquire_lease(now)
        if lease is None:
            return CollectionRunResult("busy", "lease_held")
        fence, _lease_seconds = lease
        try:
            # The lease acquisition may have waited behind another writer; make
            # the cadence decision again while this worker is the fenced owner.
            decision = self._schedule(self._utc(self.clock()), selected)
            if decision is None:
                return CollectionRunResult("no_work", "not_due")
            poll_started = self._utc(self.clock())
            board_started = self.monotonic()
            if not self._renew_lease(fence):
                return self._lease_lost_result(
                    started=started, providers=decision.providers
                )
            try:
                board = self.board_service.get_board(
                    NBAMarketQuery(season=self.season),
                    providers=decision.providers,
                )
            except Exception:
                duration_ms = max(
                    0, int(round((self.monotonic() - board_started) * 1000))
                )
                for provider in decision.providers:
                    if not self._renew_lease(fence):
                        return self._lease_lost_result(
                            started=started, providers=decision.providers
                        )
                    completed_at = self._utc(self.clock())
                    self.recording_service.record_failed_poll(
                        provider=provider,
                        query=NBAMarketQuery(season=self.season),
                        completed_at=completed_at,
                        poll_started_at=poll_started,
                        failure_reason="upstream_error",
                        duration_ms=duration_ms,
                    )
                    self._update_state(
                        provider=provider,
                        interval=decision.interval,
                        success=False,
                        failure_reason="upstream_error",
                        duration_ms=duration_ms,
                    )
                return CollectionRunResult(
                    "partial",
                    "provider_collection_failed",
                    tuple(decision.providers),
                    max(0, int(round((self.monotonic() - started) * 1000))),
                )
            duration_ms = max(0, int(round((self.monotonic() - board_started) * 1000)))
            outcomes = tuple(getattr(board, "provider_outcomes", ()))
            successes = 0
            failures = 0
            observed_providers: set[str] = set()
            for outcome in outcomes:
                provider = str(getattr(outcome, "provider", "")).strip().casefold()
                if provider not in decision.providers:
                    continue
                observed_providers.add(provider)
                if not self._renew_lease(fence):
                    return self._lease_lost_result(
                        started=started, providers=decision.providers
                    )
                if (
                    self._outcome_status(outcome) in {"complete", "partial"}
                    and getattr(outcome, "snapshot", None) is not None
                    and not self._has_cache_failure(outcome)
                ):
                    snapshot = outcome.snapshot
                    try:
                        result = self.recording_service.record_snapshot(
                            snapshot,
                            query=NBAMarketQuery(season=self.season),
                            accepted_at=self._utc(self.clock()),
                            poll_started_at=poll_started,
                            duration_ms=duration_ms,
                        )
                    except Exception:
                        failures += 1
                        if not self._renew_lease(fence):
                            return self._lease_lost_result(
                                started=started, providers=decision.providers
                            )
                        self.recording_service.record_failed_poll(
                            provider=provider,
                            query=NBAMarketQuery(season=self.season),
                            completed_at=self._utc(self.clock()),
                            poll_started_at=poll_started,
                            failure_reason="persistence_error",
                            duration_ms=duration_ms,
                        )
                        self._update_state(
                            provider=provider,
                            interval=decision.interval,
                            success=False,
                            failure_reason="persistence_error",
                        )
                        continue
                    if not self._renew_lease(fence):
                        return self._lease_lost_result(
                            started=started, providers=decision.providers
                        )
                    successes += 1
                    retrieved_at = self._utc(snapshot.retrieved_at)
                    self._update_state(
                        provider=provider,
                        interval=decision.interval,
                        success=True,
                        changed_at=retrieved_at if bool(getattr(result, "changed", False)) else None,
                        counts=self._snapshot_counts(snapshot),
                        duration_ms=duration_ms,
                    )
                else:
                    failures += 1
                    reason = self._failure_reason(outcome)
                    self.recording_service.record_failed_poll(
                        provider=provider,
                        query=NBAMarketQuery(season=self.season),
                        completed_at=self._utc(self.clock()),
                        poll_started_at=poll_started,
                        failure_reason=reason,
                        duration_ms=duration_ms,
                    )
                    if not self._renew_lease(fence):
                        return self._lease_lost_result(
                            started=started, providers=decision.providers
                        )
                    self._update_state(
                        provider=provider,
                        interval=decision.interval,
                        success=False,
                        failure_reason=reason,
                        duration_ms=duration_ms,
                    )
            for provider in decision.providers:
                if provider in observed_providers:
                    continue
                failures += 1
                if not self._renew_lease(fence):
                    return self._lease_lost_result(
                        started=started, providers=decision.providers
                    )
                completed_at = self._utc(self.clock())
                self.recording_service.record_failed_poll(
                    provider=provider,
                    query=NBAMarketQuery(season=self.season),
                    completed_at=completed_at,
                    poll_started_at=poll_started,
                    failure_reason="missing_outcome",
                    duration_ms=duration_ms,
                )
                self._update_state(
                    provider=provider,
                    interval=decision.interval,
                    success=False,
                    failure_reason="missing_outcome",
                    duration_ms=duration_ms,
                )
            status = "complete" if failures == 0 and successes == len(decision.providers) else "partial"
            return CollectionRunResult(
                status,
                "collected",
                tuple(decision.providers),
                max(0, int(round((self.monotonic() - started) * 1000))),
            )
        finally:
            self._release_lease(self._utc(self.clock()), fence)

    def diagnostics(self, *, limit: int = 50) -> dict[str, Any]:
        """Return bounded collector health without payloads or source IDs."""

        bounded = max(1, min(int(limit), 100))
        now = self._utc(self.clock())
        query_key = projection_query_key(NBAMarketQuery(season=self.season))
        with self.engine.connect() as connection:
            database_now = self._database_now(connection)
            states = list(
                connection.execute(
                    select(ProjectionCollectionProviderState.__table__)
                    .where(
                        ProjectionCollectionProviderState.__table__.c.season == self.season,
                        ProjectionCollectionProviderState.__table__.c.query_key == query_key,
                    )
                    .order_by(ProjectionCollectionProviderState.__table__.c.provider)
                    .limit(bounded)
                ).mappings()
            )
            lease = connection.execute(
                select(ProjectionCollectionLease.__table__).where(
                    ProjectionCollectionLease.__table__.c.lease_key == LEASE_KEY
                )
            ).mappings().one_or_none()
        provider_diagnostics = []
        for state in states:
            freshness = None
            if state["last_poll_at"] is not None:
                freshness = min(
                    _MAX_DIAGNOSTIC_SECONDS,
                    max(0, math.floor((now - self._utc(state["last_poll_at"])).total_seconds())),
                )
            provider_diagnostics.append(
                {
                    "provider": state["provider"],
                    "last_poll_at": self._utc(state["last_poll_at"]).isoformat() if state["last_poll_at"] else None,
                    "last_changed_snapshot_at": self._utc(state["last_changed_at"]).isoformat() if state["last_changed_at"] else None,
                    "freshness_seconds": freshness,
                    "failure": {
                        "last_at": self._utc(state["last_failure_at"]).isoformat() if state["last_failure_at"] else None,
                        "reason": state["last_failure_reason"],
                        "consecutive": min(100, max(0, int(state["consecutive_failures"] or 0))),
                    },
                    "backoff": {
                        "active": bool(state["backoff_until"] and self._utc(state["backoff_until"]) > now),
                        "until": self._utc(state["backoff_until"]).isoformat() if state["backoff_until"] else None,
                    },
                    "active_count": min(100000, max(0, int(state["active_count"] or 0))),
                    "unresolved_count": min(100000, max(0, int(state["unresolved_count"] or 0))),
                }
            )
        lease_active = bool(
            lease
            and lease["owner"]
            and self._utc(lease["lease_expires_at"]) > database_now
        )
        return {
            "providers": provider_diagnostics,
            "active_count": sum(item["active_count"] for item in provider_diagnostics),
            "unresolved_count": sum(item["unresolved_count"] for item in provider_diagnostics),
            "lease": {
                "active": lease_active,
                "fence": min(2**31 - 1, max(0, int(lease["fence"]))) if lease else 0,
                "expires_at": self._utc(lease["lease_expires_at"]).isoformat() if lease and lease_active else None,
            },
        }


__all__ = [
    "CollectionRunResult",
    "ProjectionCollectionCoordinator",
    "ProjectionCollectionSettings",
]
