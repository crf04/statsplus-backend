"""Application-owned canonical NBA event catalog tables.

The event catalog is deliberately separate from bundled NBA demo tables and
from any athlete catalog. ``EventCatalogEntry`` stores provider-owned facts
keyed by the stable NBA game identity. Mapping and audit state belongs to the
event-mapping work that consumes this catalog.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from . import Base


class EventCatalogEntry(Base):
    """One canonical NBA game keyed only by the NBA game ID."""

    __tablename__ = "event_catalog"

    nba_game_id = Column(String(32), primary_key=True)
    season = Column(String(7), nullable=False, index=True)

    home_team_id = Column(Integer, nullable=False)
    home_team_name = Column(String(128), nullable=False)
    home_team_tricode = Column(String(8), nullable=False)
    away_team_id = Column(Integer, nullable=False)
    away_team_name = Column(String(128), nullable=False)
    away_team_tricode = Column(String(8), nullable=False)

    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    status_text = Column(String(128), nullable=False)
    status_code = Column(Integer, nullable=True)
    postponed_status = Column(String(128), nullable=True)
    postponement_evidence = Column(Text, nullable=True)
    classification = Column(String(128), nullable=False)

    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    first_observed_started_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_event_catalog_season_scheduled_at", "season", "scheduled_at"),
    )



class EventCatalogRefresh(Base):
    """Per-season event-catalog attempt/success/failure state."""

    __tablename__ = "event_catalog_refreshes"

    season = Column(String(7), primary_key=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    failure_summary = Column(Text, nullable=True)
    event_count = Column(Integer, nullable=False, default=0, server_default="0")


__all__ = ["EventCatalogEntry", "EventCatalogRefresh"]
