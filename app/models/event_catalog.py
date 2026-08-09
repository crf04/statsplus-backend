"""Application-owned canonical NBA event catalog tables.

The event catalog is deliberately separate from bundled NBA demo tables and
from any athlete catalog.  ``EventCatalogEntry`` stores the provider's stable
NBA game identity and the facts needed by later event mapping work.  Audit
fields are owned by the catalog and are never overwritten by a schedule
refresh, so a replacement schedule cannot silently erase a mapping-needed
review.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import synonym

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

    # These columns are local audit state.  A schedule refresh may update the
    # provider-owned facts above, but never these fields.
    mapping_needed = Column(Boolean, nullable=False, default=False, server_default="0")
    audit_status = Column(String(32), nullable=False, default="unreviewed")
    audit_note = Column(Text, nullable=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_event_catalog_season_scheduled_at", "season", "scheduled_at"),
    )

    game_id = synonym("nba_game_id")

    @property
    def event_classification(self) -> str:
        """Compatibility spelling for the canonical classification."""

        return self.classification

    @property
    def scheduled_time_utc(self):
        """Compatibility spelling for the UTC schedule timestamp."""

        return self.scheduled_at

    @property
    def game_status_text(self) -> str:
        """Compatibility spelling for the provider status text."""

        return self.status_text

    @property
    def home_team_abbreviation(self) -> str:
        return self.home_team_tricode

    @property
    def away_team_abbreviation(self) -> str:
        return self.away_team_tricode


class EventCatalogRefresh(Base):
    """Per-season event-catalog attempt/success/failure state."""

    __tablename__ = "event_catalog_refreshes"

    season = Column(String(7), primary_key=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    failure_summary = Column(Text, nullable=True)
    event_count = Column(Integer, nullable=False, default=0, server_default="0")

    @property
    def last_refresh_at(self):
        """Compatibility spelling for the last successful publication."""

        return self.last_success_at

    @property
    def last_error(self):
        return self.failure_summary

__all__ = ["EventCatalogEntry", "EventCatalogRefresh"]
