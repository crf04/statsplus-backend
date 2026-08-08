"""Durable, SQL-backed orchestration for data-refresh job records.

Mutating data routes return immediately (HTTP 202) with a ``job_id`` and leave
the refresh to a background executor.  Every transition is written to the
``data_refresh_jobs`` table, so status, progress, timestamps, and a sanitized
failure summary survive a process restart.

At most one queued or running job may exist per ``operation``.  The partial
unique index on the model enforces that at the database level; this service
also pre-checks before inserting so callers get a predictable ``409`` without
relying on a thrown ``IntegrityError``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.errors import (
    AppError,
    DuplicateOperationError,
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

logger = logging.getLogger(__name__)

#: Stable, non-exposing summary written when a job fails without a safe reason.
DEFAULT_FAILURE_SUMMARY: Final[str] = (
    "The data refresh operation could not complete."
)

RefreshCallable = Callable[[], Any]

__all__ = ["DataRefreshJobService", "SynchronousExecutor", "DEFAULT_FAILURE_SUMMARY"]


def _failure_summary(error: BaseException) -> str:
    """Return a stable, safe summary for a job failure.

    ``AppError.public_message`` values are already the application's public,
    sanitized contract.  Other exceptions may contain provider bodies or
    traceback text, so they collapse to one fixed message.
    """

    if isinstance(error, AppError):
        return error.public_message
    return DEFAULT_FAILURE_SUMMARY


class SynchronousExecutor:
    """Run submitted callables inline for deterministic tests."""

    def submit(self, fn: RefreshCallable) -> Any:
        return fn()


class DataRefreshJobService:
    """Create, observe, and run durable data-refresh jobs."""

    def __init__(
        self,
        engine,
        executor: object | None = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._engine = engine
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="data-refresh",
        )
        self._clock = clock or utcnow
        self._session_factory = sessionmaker(bind=engine)

    def start(self, operation: str, refresh: RefreshCallable) -> dict:
        """Enqueue one job and run ``refresh`` on the configured executor.

        Returns the durable record as it was queued (``job_id``, ``operation``,
        ``status``).  A concurrent active job for the same ``operation`` is
        rejected with ``DuplicateOperationError``.
        """

        job_id = uuid.uuid4().hex
        now = self._clock()

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

        self._executor.submit(lambda: self._run(job_id, refresh))
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

    def update_progress(self, job_id: str, progress: float, note: str | None = None) -> None:
        """Record coarse progress while a long refresh is running."""

        self._update_job(job_id, progress=float(progress), progress_note=note)

    def _run(self, job_id: str, refresh: RefreshCallable) -> None:
        """Transition one job through running/succeeded/failed on the executor."""

        self._update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            started_at=self._clock(),
        )
        try:
            result = refresh()
        except BaseException as error:
            self._fail(job_id, error)
            return

        if result is False:
            self._fail(job_id, OperationFailedError())
            return

        self._update_job(
            job_id,
            status=JOB_STATUS_SUCCEEDED,
            finished_at=self._clock(),
            progress=1.0,
            progress_note="Completed",
        )

    def _fail(self, job_id: str, error: BaseException) -> None:
        """Record a failure and log it without leaking implementation detail."""

        summary = _failure_summary(error)
        try:
            self._update_job(
                job_id,
                status=JOB_STATUS_FAILED,
                finished_at=self._clock(),
                error_summary=summary,
            )
        except Exception:
            logger.exception("Could not record failure for job %s", job_id)
            return
        logger.error(
            "Data refresh job %s failed: %s",
            job_id,
            _sanitize_diagnostic_detail(str(error)) or summary,
        )

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