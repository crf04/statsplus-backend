"""Persisted, governed Player Pool snapshots and refresh leases."""

from sqlalchemy import Column, DateTime, Integer, String, Text

from . import Base


class PlayerPoolSnapshot(Base):
    """One atomic Player Pool observation for a season and exact game set."""

    __tablename__ = "player_pool_snapshots"

    season = Column(String(7), primary_key=True)
    game_ids = Column(Text, primary_key=True)
    payload = Column(Text, nullable=True)
    retrieved_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    lease_owner = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    refresh_version = Column(Integer, nullable=False, default=0, server_default="0")
    refresh_outcome = Column(String(16), nullable=True)


__all__ = ["PlayerPoolSnapshot"]
