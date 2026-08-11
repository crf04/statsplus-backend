"""Persisted raw and normalized injury-report observations."""

from sqlalchemy import Column, DateTime, String, Text

from . import Base


class InjurySnapshot(Base):
    """One final-or-refreshable injury observation for a canonical game."""

    __tablename__ = "injury_snapshots"

    season = Column(String(7), primary_key=True)
    game_id = Column(String(32), primary_key=True)
    raw_payload = Column(Text, nullable=False)
    normalized_entries = Column(Text, nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


__all__ = ["InjurySnapshot"]
