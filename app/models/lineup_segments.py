"""Stored lineup segments for on/off splits (crf04/statsplus#410).

A segment is one stretch of one period in which a team's five on-floor players
do not change.  ``lineup_segment_players`` holds one row per on-floor player
per segment: the lineup as a bitmask over the team-season's roster slots, the
segment's length, that player's counts, and the team's on-floor totals for the
segment (repeated on each of the five rows, so a split is one scan with no
join).  A split sums the rows whose mask holds every ``on`` slot and no
``off`` slot.

``lineup_segment_games`` heads each game the rotation job wrote or rejected,
and ``lineup_segment_seasons`` records which seasons are complete enough to
serve.  Times are integer centiseconds, as in ``rotation_stints``.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)

from . import Base

#: A lineup mask is a signed 64-bit integer, one bit per roster slot.
MAX_ROSTER_SLOTS = 64


class LineupRosterSlot(Base):
    """A player's bit in his team-season's lineup masks, assigned on first sight."""

    __tablename__ = "lineup_roster_slots"

    season = Column(String(7), primary_key=True)
    team_id = Column(Integer, primary_key=True)
    player_id = Column(Integer, primary_key=True)
    slot = Column(SmallInteger, nullable=False)
    player_name = Column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint("season", "team_id", "slot", name="uq_lineup_roster_slots_slot"),
        CheckConstraint(f"slot >= 0 AND slot < {MAX_ROSTER_SLOTS}", name="ck_lineup_roster_slots_slot"),
    )


class LineupSegmentGame(Base):
    """One game the rotation job wrote segments for, or rejected.

    ``rejected_reason`` is null for a stored game; a rejected game has no
    segment rows and is listed in its teams' ``games_missing``.
    """

    __tablename__ = "lineup_segment_games"

    game_id = Column(String(10), primary_key=True)
    season = Column(String(7), nullable=False)
    home_team_id = Column(Integer, nullable=False)
    away_team_id = Column(Integer, nullable=False)
    written_at = Column(DateTime(timezone=True), nullable=False)
    rejected_reason = Column(Text)

    __table_args__ = (Index("ix_lineup_segment_games_season", "season"),)


class LineupSegmentPlayer(Base):
    """One on-floor player in one segment, with the segment's team totals."""

    __tablename__ = "lineup_segment_players"

    game_id = Column(String(10), primary_key=True)
    team_id = Column(Integer, primary_key=True)
    seg = Column(SmallInteger, primary_key=True)
    player_id = Column(Integer, primary_key=True)
    season = Column(String(7), nullable=False)
    lineup_mask = Column(BigInteger, nullable=False)
    duration_cs = Column(Integer, nullable=False)
    pts = Column(SmallInteger, nullable=False)
    reb = Column(SmallInteger, nullable=False)
    ast = Column(SmallInteger, nullable=False)
    fg3m = Column(SmallInteger, nullable=False)
    fga = Column(SmallInteger, nullable=False)
    fg3a = Column(SmallInteger, nullable=False)
    tov = Column(SmallInteger, nullable=False)
    fta = Column(SmallInteger, nullable=False)
    team_fga = Column(SmallInteger, nullable=False)
    team_fta = Column(SmallInteger, nullable=False)
    team_tov = Column(SmallInteger, nullable=False)
    team_heaves = Column(SmallInteger, nullable=False)
    off_poss = Column(SmallInteger, nullable=False)

    __table_args__ = (Index("ix_lineup_segment_players_team_season", "team_id", "season"),)


class LineupSegmentSeason(Base):
    """A season whose segments are complete: only these serve on/off splits."""

    __tablename__ = "lineup_segment_seasons"

    season = Column(String(7), primary_key=True)
    complete = Column(Boolean, nullable=False)
    completed_at = Column(DateTime(timezone=True))


__all__ = [
    "MAX_ROSTER_SLOTS",
    "LineupRosterSlot",
    "LineupSegmentGame",
    "LineupSegmentPlayer",
    "LineupSegmentSeason",
]
