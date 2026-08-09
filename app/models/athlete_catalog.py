"""Application-owned canonical athlete catalog schema.

The catalog is deliberately separate from the bundled NBA demo tables.  A
row's identity is the provider's stable NBA player ID plus an explicit season;
display names and team assignments can therefore change without rewriting
historical seasons.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from . import Base


class AthleteCatalog(Base):
    """One canonical NBA athlete observation for one requested season."""

    __tablename__ = "athlete_catalog"

    season = Column(String(7), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    display_name = Column(String(255), nullable=False)
    roster_status = Column(String(16), nullable=False)
    is_active = Column(Boolean, nullable=False)
    is_active_for_season = Column(Boolean, nullable=False)
    team_id = Column(Integer, nullable=True)
    team_name = Column(String(255), nullable=True)
    team_abbreviation = Column(String(16), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=False)


class AthleteCatalogFreshness(Base):
    """Durable success/failure state for each season catalog refresh."""

    __tablename__ = "athlete_catalog_freshness"

    season = Column(String(7), primary_key=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_success_row_count = Column(Integer, nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_summary = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


__all__ = [
    "AthleteCatalog",
    "AthleteCatalogFreshness",
]
