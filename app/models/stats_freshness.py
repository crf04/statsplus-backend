"""Durable completion state for the atomically published stats tables."""

from sqlalchemy import Column, DateTime, String

from . import Base


class StatsRefresh(Base):
    """The latest successful publication for one governed stats surface."""

    __tablename__ = "stats_refreshes"

    surface = Column(String(32), primary_key=True)
    last_success_at = Column(DateTime(timezone=True), nullable=False)
