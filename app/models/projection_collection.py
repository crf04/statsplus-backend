"""Durable coordination state for scheduled projection collection."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, String

from . import Base


class ProjectionCollectionLease(Base):
    """The singleton database lease fencing projection collector runs."""

    __tablename__ = "projection_collection_leases"

    lease_key = Column(String(32), primary_key=True)
    owner = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=False)
    fence = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime(timezone=True), nullable=False)


class ProjectionCollectionProviderState(Base):
    """Last poll, cadence, and bounded failure state for one provider scope."""

    __tablename__ = "projection_collection_provider_states"

    provider = Column(String(64), primary_key=True)
    season = Column(String(7), primary_key=True)
    query_key = Column(String(72), primary_key=True)
    last_poll_at = Column(DateTime(timezone=True), nullable=True)
    last_changed_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_reason = Column(String(64), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    next_poll_at = Column(DateTime(timezone=True), nullable=True)
    backoff_until = Column(DateTime(timezone=True), nullable=True)
    active_count = Column(Integer, nullable=False, default=0, server_default="0")
    unresolved_count = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_projection_collection_provider_state_next_poll",
            "season",
            "query_key",
            "next_poll_at",
        ),
    )


__all__ = ["ProjectionCollectionLease", "ProjectionCollectionProviderState"]
