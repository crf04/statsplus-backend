"""Persisted raw and normalized injury-report observations."""

from sqlalchemy import Column, DateTime, Integer, String, Text

from . import Base


class InjurySourceSnapshot(Base):
    """One append-only league-wide provider observation."""

    __tablename__ = "injury_source_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(32), nullable=False, index=True)
    raw_payload = Column(Text, nullable=False)
    normalized_entries = Column(Text, nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False, index=True)


class InjurySnapshot(Base):
    """One final-or-refreshable injury observation for a canonical game."""

    __tablename__ = "injury_snapshots"

    season = Column(String(7), primary_key=True)
    game_id = Column(String(32), primary_key=True)
    source_snapshot_id = Column(Integer, nullable=True)
    # Legacy migration-014 rows retain raw evidence here. New rows reference
    # the append-only source table and store an empty JSON list in this column.
    raw_payload = Column(Text, nullable=False)
    normalized_entries = Column(Text, nullable=False)
    unresolved_team_entry_count = Column(Integer, nullable=False, default=0)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


__all__ = ["InjurySnapshot", "InjurySourceSnapshot"]
