"""
Durable data-refresh job records for the application database.

Each mutating data refresh is recorded here before it starts so start
requests can return immediately (HTTP 202) and administrators can observe
status, progress, timestamps, and a sanitized failure summary later.  The
table is part of the application schema, not the read-only demo fixture.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Optional

from sqlalchemy import Column, DateTime, Float, Index, String, Text, text

from . import Base


JOB_STATUS_QUEUED: Final[str] = "queued"
JOB_STATUS_RUNNING: Final[str] = "running"
JOB_STATUS_SUCCEEDED: Final[str] = "succeeded"
JOB_STATUS_FAILED: Final[str] = "failed"

ACTIVE_JOB_STATUSES: Final[tuple[str, ...]] = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used for durable job clocks."""
    return datetime.now(__import__("datetime").timezone.utc)


class DataRefreshJob(Base):
    """One durable attempt at a mutating NBA data refresh."""

    __tablename__ = "data_refresh_jobs"

    job_id = Column(String(36), primary_key=True)
    operation = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default=JOB_STATUS_QUEUED)
    progress = Column(Float, nullable=False, default=0.0)
    progress_note = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    error_summary = Column(Text, nullable=True)

    __table_args__ = (
        # A database-enforced partial unique index: at most one active job
        # (queued or running) may exist per operation, on both SQLite and
        # PostgreSQL.
        Index(
            "uq_data_refresh_jobs_active_operation",
            "operation",
            unique=True,
            sqlite_where=text(
                f"status IN ('{JOB_STATUS_QUEUED}', '{JOB_STATUS_RUNNING}')"
            ),
            postgresql_where=text(
                f"status IN ('{JOB_STATUS_QUEUED}', '{JOB_STATUS_RUNNING}')"
            ),
        ),
    )

    def to_state(self) -> dict:
        """Serialize a durable, transport-neutral snapshot of this job."""
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "progress": self.progress,
            "progress_note": self.progress_note,
            "created_at": _isoformat(self.created_at),
            "started_at": _isoformat(self.started_at),
            "finished_at": _isoformat(self.finished_at),
            "failure_summary": self.error_summary,
        }


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()