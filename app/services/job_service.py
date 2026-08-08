"""Durable SQL-backed orchestration for data-refresh jobs.

The database row is the queue.  A worker claims a row with a short lease,
executes the operation resolved from the process' closed handler registry, and
renews that lease until the operation publishes its result.  No executable
callback is serialized in the database, so a fresh process can recover queued
or abandoned work deterministically.
"""

from __future__ import annotations

import atexit
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.errors import (
    AppError,
    DuplicateOperationError,
    InvalidInputError,
    OperationFailedError,
    ResourceNotFoundError,
    _sanitize_diagnostic_detail,
)
from app.models.job import (
    ACTIVE_JOB_STATUSES,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    DataRefreshJob,
    utcnow,
)
from app.utils.telemetry import (
    clear_request_id,
    current_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)

# These are the only operations accepted by the refresh API.  Keeping the
# names closed prevents a typo in a route from creating a job that no process
# can execute after a restart.
KNOWN_REFRESH_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "update_database",
        "player_pbp",
        "opponent_pbp",
        "fetch_players_with_teams",
        "fetch_players",
    }
)

#: Stable, non-exposing summary written when a job fails without a safe reason.
DEFAULT_FAILURE_SUMMARY: Final[str] = (
    "The data refresh operation could not complete."
)

RefreshCallable = Callable[..., Any]
ProgressCallback = Callable[[float, str | None], None]

__all__ = [
    "DataRefreshJobService",
    "SynchronousExecutor",
    "DEFAULT_FAILURE_SUMMARY",
    "KNOWN_REFRESH_OPERATIONS",
    "build_default_refresh_handlers",
    "build_data_refresh_job_service",
    "adapt_zero_arg_handler",
]


def _failure_summary(error: BaseException) -> str:
    """Return a stable, safe summary for a job failure."""

    if isinstance(error, AppError):
        return error.public_message
    return DEFAULT_FAILURE_SUMMARY


class SynchronousExecutor:
    """Run submitted callables inline for deterministic tests."""

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)


def adapt_zero_arg_handler(handler: Callable[[], Any]) -> RefreshCallable:
    """Adapt a deliberately tiny test fake to the one worker protocol.

    Production handlers are registered with the explicit keyword-only
    ``progress_callback`` signature.  A unit test that does not need progress
    can opt into this adapter when constructing its complete registry; the
    worker itself never inspects or branches on handler signatures.
    """

    def wrapped(*, progress_callback: ProgressCallback) -> Any:
        del progress_callback
        return handler()

    return wrapped


def build_default_refresh_handlers(engine, settings) -> Mapping[str, RefreshCallable]:
    """Build the complete operation registry for one application instance.

    The registry is assembled at app startup from stable operation names and
    service methods.  The resulting mapping is copied and frozen by
    :class:`DataRefreshJobService`; only those operations can be recovered by a
    later process.
    """

    from app.services.data_service import DataService
    from app.services.player_service import PlayerService

    data_service = DataService(engine, settings=settings)
    player_service = PlayerService(engine, settings=settings)

    def player_pbp(*, progress_callback: ProgressCallback) -> Any:
        return data_service.fetch_PBP_data(
            "player", progress_callback=progress_callback
        )

    def opponent_pbp(*, progress_callback: ProgressCallback) -> Any:
        return data_service.fetch_PBP_data(
            "opponent", progress_callback=progress_callback
        )

    return {
        "update_database": data_service.update_all_data,
        "player_pbp": player_pbp,
        "opponent_pbp": opponent_pbp,
        "fetch_players_with_teams": data_service.fetch_players_with_teams,
        "fetch_players": player_service.store_player_information,
    }


def build_data_refresh_job_service(engine, settings) -> "DataRefreshJobService":
    """Build the canonical app-scoped durable refresh coordinator."""

    return DataRefreshJobService(
        engine,
        settings=settings,
        handlers=build_default_refresh_handlers(engine, settings),
    )


class DataRefreshJobService:
    """Create, observe, claim, and run durable data-refresh jobs."""

    def __init__(
        self,
        engine,
        executor: object | None = None,
        clock: Callable[[], Any] | None = None,
        handlers: Mapping[str, RefreshCallable] | None = None,
        settings: Any | None = None,
        lease_seconds: float = 60.0,
        poll_interval: float = 1.0,
        max_workers: int = 2,
        dispatch_on_startup: bool = True,
        start_poller: bool | None = None,
    ) -> None:
        self._engine = engine
        self.settings = settings
        self._clock = clock or utcnow
        self._session_factory = sessionmaker(bind=engine)
        self._handlers = MappingProxyType(dict(handlers or {}))
        self._lease_seconds = max(float(lease_seconds), 0.1)
        self._poll_interval = max(float(poll_interval), 0.05)
        self._owner = f"{uuid.uuid4().hex}:{threading.get_ident()}"
        self._worker_limit = max(int(max_workers), 1)
        self._submitted = 0
        self._submitted_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poller: threading.Thread | None = None
        self._shutdown = False

        self._executor_owned = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=self._worker_limit,
            thread_name_prefix="data-refresh",
        )

        if dispatch_on_startup:
            # The app factory normally migrates before constructing this
            # service.  The defensive catch keeps read-only/demo app startup
            # usable when a test intentionally skips table creation.
            try:
                self.dispatch_once()
            except SQLAlchemyError:
                logger.debug("Data refresh queue is not available yet", exc_info=True)

        if start_poller is None:
            start_poller = not isinstance(self._executor, SynchronousExecutor)
        if start_poller:
            self._poller = threading.Thread(
                target=self._poll_loop,
                name="data-refresh-dispatcher",
                daemon=True,
            )
            self._poller.start()
            if self._executor_owned:
                atexit.register(self.shutdown)

    def start(
        self,
        operation: str,
        *,
        request_id: str | None = None,
    ) -> dict:
        """Persist and dispatch one operation, returning its queued state.

        The operation name is the only executable identity persisted in SQL.
        Every process resolves it from the immutable startup registry, so a
        queued row can be recovered after a restart without request closures.
        """

        if operation not in KNOWN_REFRESH_OPERATIONS:
            raise InvalidInputError("Unsupported data refresh operation.")
        if self._handler_for(operation) is None:
            raise InvalidInputError("The data refresh operation is unavailable.")

        job_id = uuid.uuid4().hex
        now = self._clock()
        correlation_id = request_id or current_request_id()

        active = self._active_job(operation)
        if active is not None:
            raise DuplicateOperationError(
                "An identical operation is already running or queued.",
                detail={
                    "operation": operation,
                    "active_job_id": active.job_id,
                },
            )

        with self._session_factory() as session:
            job = DataRefreshJob(
                job_id=job_id,
                operation=operation,
                status=JOB_STATUS_QUEUED,
                created_at=now,
                progress=0.0,
                request_id=correlation_id,
                attempt_count=0,
            )
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise DuplicateOperationError(
                    "An identical operation is already running or queued.",
                    detail={
                        "operation": operation,
                        "reason": "database duplicate-activity constraint",
                    },
                ) from None
            queued_state = job.to_state()

        # A request should wake a worker immediately; the poller is only the
        # restart/stale-lease recovery path.
        self.dispatch_once()
        return queued_state

    def get(self, job_id: str) -> dict:
        """Return the durable record state for one job."""

        with self._session_factory() as session:
            job = session.get(DataRefreshJob, job_id)
            if job is None:
                raise ResourceNotFoundError(
                    "The requested job was not found.",
                    detail=f"job_id={job_id!r}",
                )
            return job.to_state()

    def update_progress(
        self,
        job_id: str,
        progress: float,
        note: str | None = None,
        *,
        lease_owner: str | None = None,
    ) -> None:
        """Record monotonic coarse progress and renew the worker lease."""

        if lease_owner:
            self._update_owned_progress(job_id, lease_owner, progress, note)
        else:
            # Retain the small public test seam used by older callers.  Worker
            # callbacks always use the owner-checked path above.
            self._update_job(
                job_id,
                progress=max(0.0, min(float(progress), 1.0)),
                progress_note=note,
            )

    def dispatch_once(self, limit: int | None = None) -> int:
        """Claim and submit a bounded batch of queued/stale jobs.

        Claiming is a conditional SQL ``UPDATE`` rather than a read-then-write
        sequence.  Multiple processes can therefore inspect the same queued
        row, but only one can change it to a lease owned by that process.
        """

        capacity = self._available_capacity()
        if limit is not None:
            capacity = min(capacity, max(int(limit), 0))
        if capacity <= 0:
            return 0

        now = self._clock()
        stale_running = (
            (DataRefreshJob.status == JOB_STATUS_RUNNING)
            & (
                DataRefreshJob.lease_expires_at.is_(None)
                | (DataRefreshJob.lease_expires_at <= now)
            )
        )
        candidate_filter = or_(
            DataRefreshJob.status == JOB_STATUS_QUEUED,
            stale_running,
        )
        try:
            with self._session_factory() as session:
                candidates = session.execute(
                    select(DataRefreshJob.job_id)
                    .where(candidate_filter)
                    .order_by(DataRefreshJob.created_at)
                    .limit(capacity)
                ).scalars().all()
        except OperationalError:
            logger.debug("Could not inspect the refresh queue", exc_info=True)
            return 0

        claimed = 0
        for job_id in candidates:
            if not self._claim(job_id, now):
                continue
            claimed += 1
            self._submit_claimed(job_id)
        return claimed

    def shutdown(self, wait: bool = False) -> None:
        """Stop polling and optionally wait for owned workers to finish."""

        if self._shutdown:
            return
        self._shutdown = True
        self._stop_event.set()
        poller = self._poller
        if poller is not None and poller is not threading.current_thread():
            poller.join(timeout=max(self._poll_interval * 2, 0.2))
        if self._executor_owned and hasattr(self._executor, "shutdown"):
            self._executor.shutdown(wait=wait)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.dispatch_once()
            except Exception:
                logger.exception("Refresh queue dispatch failed")
            self._stop_event.wait(self._poll_interval)

    def _available_capacity(self) -> int:
        with self._submitted_lock:
            return max(self._worker_limit - self._submitted, 0)

    def _release_slot(self, _future: Any = None) -> None:
        with self._submitted_lock:
            self._submitted = max(self._submitted - 1, 0)

    def _submit_claimed(self, job_id: str) -> None:
        with self._submitted_lock:
            self._submitted += 1
        try:
            result = self._executor.submit(self._run_claimed, job_id, self._owner)
        except BaseException:
            self._release_slot()
            raise
        if hasattr(result, "add_done_callback"):
            result.add_done_callback(self._release_slot)
        else:
            # SynchronousExecutor has no Future and has already completed.
            self._release_slot()

    def _claim(self, job_id: str, now: Any) -> bool:
        lease_expires = now + timedelta(seconds=self._lease_seconds)
        stale_filter = (
            (DataRefreshJob.status == JOB_STATUS_RUNNING)
            & (
                DataRefreshJob.lease_expires_at.is_(None)
                | (DataRefreshJob.lease_expires_at <= now)
            )
        )
        condition = (
            (DataRefreshJob.job_id == job_id)
            & (
                (DataRefreshJob.status == JOB_STATUS_QUEUED)
                | stale_filter
            )
        )
        try:
            with self._session_factory() as session:
                result = session.execute(
                    update(DataRefreshJob)
                    .where(condition)
                    .values(
                        status=JOB_STATUS_RUNNING,
                        started_at=case(
                            (DataRefreshJob.started_at.is_(None), now),
                            else_=DataRefreshJob.started_at,
                        ),
                        lease_owner=self._owner,
                        lease_expires_at=lease_expires,
                        heartbeat_at=now,
                        attempt_count=DataRefreshJob.attempt_count + 1,
                        progress_note="Running",
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    return False
                session.commit()
                return True
        except OperationalError:
            logger.debug("Could not claim refresh job %s", job_id, exc_info=True)
            return False

    def _run_claimed(self, job_id: str, owner: str) -> None:
        job = self._owned_job(job_id, owner)
        if job is None:
            return

        request_id = job.request_id or "-"
        set_request_id(request_id)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, owner, stop_heartbeat),
            name=f"data-refresh-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            handler = self._handler_for(job.operation)
            if handler is None:
                raise RuntimeError("No registered handler for refresh operation")
            result = handler(
                progress_callback=lambda progress, note=None: self.update_progress(
                    job_id, progress, note, lease_owner=owner
                )
            )
            if result is False:
                self._fail(job_id, owner, OperationFailedError())
            else:
                self._finish(job_id, owner)
        except BaseException as error:
            self._fail(job_id, owner, error)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(self._poll_interval, 0.1))
            clear_request_id()


    def _heartbeat_loop(
        self,
        job_id: str,
        owner: str,
        stop_event: threading.Event,
    ) -> None:
        interval = min(max(self._lease_seconds / 3.0, 0.05), 5.0)
        while not stop_event.wait(interval):
            if not self._renew_lease(job_id, owner):
                return

    def _renew_lease(self, job_id: str, owner: str) -> bool:
        now = self._clock()
        try:
            with self._session_factory() as session:
                result = session.execute(
                    update(DataRefreshJob)
                    .where(
                        DataRefreshJob.job_id == job_id,
                        DataRefreshJob.status == JOB_STATUS_RUNNING,
                        DataRefreshJob.lease_owner == owner,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                        heartbeat_at=now,
                    )
                )
                session.commit()
                return result.rowcount == 1
        except OperationalError:
            logger.debug("Could not renew refresh job %s", job_id, exc_info=True)
            return False

    def _owned_job(self, job_id: str, owner: str):
        with self._session_factory() as session:
            return session.execute(
                select(DataRefreshJob).where(
                    DataRefreshJob.job_id == job_id,
                    DataRefreshJob.status == JOB_STATUS_RUNNING,
                    DataRefreshJob.lease_owner == owner,
                )
            ).scalars().first()

    def _finish(self, job_id: str, owner: str) -> None:
        self._update_owned(
            job_id,
            owner,
            status=JOB_STATUS_SUCCEEDED,
            finished_at=self._clock(),
            progress=1.0,
            progress_note="Completed",
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=None,
            error_summary=None,
        )

    def _fail(self, job_id: str, owner: str, error: BaseException) -> None:
        """Record failure, clear the lease, and keep retry possible."""

        summary = _failure_summary(error)
        try:
            self._update_owned(
                job_id,
                owner,
                status=JOB_STATUS_FAILED,
                finished_at=self._clock(),
                error_summary=summary,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
        except Exception:
            logger.exception("Could not record failure for job %s", job_id)
            return
        logger.error(
            "Data refresh job %s failed: %s",
            job_id,
            _sanitize_diagnostic_detail(str(error)) or summary,
        )

    def _update_owned(self, job_id: str, owner: str, **fields: Any) -> None:
        with self._session_factory() as session:
            session.execute(
                update(DataRefreshJob).where(
                    DataRefreshJob.job_id == job_id,
                    DataRefreshJob.status == JOB_STATUS_RUNNING,
                    DataRefreshJob.lease_owner == owner,
                ).values(**fields)
            )
            session.commit()

    def _update_owned_progress(
        self,
        job_id: str,
        owner: str,
        progress: float,
        note: str | None,
    ) -> None:
        now = self._clock()
        bounded = max(0.0, min(float(progress), 1.0))
        with self._session_factory() as session:
            session.execute(
                update(DataRefreshJob)
                .where(
                    DataRefreshJob.job_id == job_id,
                    DataRefreshJob.status == JOB_STATUS_RUNNING,
                    DataRefreshJob.lease_owner == owner,
                )
                .values(
                    progress=case(
                        (DataRefreshJob.progress < bounded, bounded),
                        else_=DataRefreshJob.progress,
                    ),
                    progress_note=note,
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                )
            )
            session.commit()

    def _handler_for(self, operation: str) -> RefreshCallable | None:
        return self._handlers.get(operation)

    def _active_job(self, operation: str):
        with self._session_factory() as session:
            return session.execute(
                select(DataRefreshJob).where(
                    DataRefreshJob.operation == operation,
                    DataRefreshJob.status.in_(ACTIVE_JOB_STATUSES),
                )
            ).scalars().first()

    def _update_job(self, job_id: str, **fields: Any) -> None:
        with self._session_factory() as session:
            job = session.get(DataRefreshJob, job_id)
            if job is None:
                logger.warning("Skipping update for unknown job %s", job_id)
                return
            for field, value in fields.items():
                setattr(job, field, value)
            session.commit()
