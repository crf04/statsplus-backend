"""Stored per-game rotations derived from NBA LiveData (crf04/statsplus#223).

A game is stored whole or not at all: ``rotation_games`` heads one game,
``rotation_player_games`` holds one row per box-score player (DNPs included),
and ``rotation_stints`` holds his on-court stints.  Times are integer
centiseconds so SQLite and PostgreSQL agree exactly; the clock columns are the
feed's clock *remaining* in the period.
"""

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)

from . import Base


class RotationGame(Base):
    """One accepted regular-season game."""

    __tablename__ = "rotation_games"

    game_id = Column(String(10), primary_key=True)
    season = Column(String(7), nullable=False)
    game_date = Column(Date, nullable=False)
    home_tricode = Column(String(8), nullable=False)
    away_tricode = Column(String(8), nullable=False)
    periods = Column(Integer, nullable=False)
    collected_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_rotation_games_season_date", "season", "game_date"),)


class RotationPlayerGame(Base):
    """One box-score player's game: starter flag, minutes, closing, periods."""

    __tablename__ = "rotation_player_games"

    game_id = Column(
        String(10),
        ForeignKey("rotation_games.game_id", ondelete="CASCADE"),
        primary_key=True,
    )
    player_id = Column(Integer, primary_key=True)
    team_tricode = Column(String(8), nullable=False)
    player_name = Column(String(128), nullable=False)
    started = Column(Boolean, nullable=False)
    minutes_cs = Column(Integer, nullable=False)
    closed = Column(Boolean, nullable=False)
    q1_cs = Column(Integer, nullable=False)
    q2_cs = Column(Integer, nullable=False)
    q3_cs = Column(Integer, nullable=False)
    q4_cs = Column(Integer, nullable=False)
    ot_cs = Column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_rotation_player_games_player", "player_id"),
        Index("ix_rotation_player_games_team", "team_tricode", "game_id"),
    )


class RotationStint(Base):
    """One continuous stay on court, in the feed's period and clock terms."""

    __tablename__ = "rotation_stints"

    game_id = Column(String(10), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    seq = Column(Integer, primary_key=True)
    in_period = Column(Integer, nullable=False)
    in_clock_cs = Column(Integer, nullable=False)
    out_period = Column(Integer, nullable=False)
    out_clock_cs = Column(Integer, nullable=False)
    minutes_cs = Column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["game_id", "player_id"],
            ["rotation_player_games.game_id", "rotation_player_games.player_id"],
            ondelete="CASCADE",
        ),
    )


class RotationAttempt(Base):
    """A game collection tried and did not store: why, and how often.

    ``rejections`` counts attempts whose documents were read but yielded no
    trustworthy rotation; collection gives a game up at
    :data:`MAX_REJECTIONS`.  A fetch failure only refreshes ``last_outcome``
    and ``last_attempt_at`` (so the game queues behind never-tried ones) and
    never consumes that budget.  The row is deleted when the game is stored.
    """

    __tablename__ = "rotation_attempts"

    game_id = Column(String(10), primary_key=True)
    season = Column(String(7), nullable=False)
    rejections = Column(Integer, nullable=False)
    last_outcome = Column(String(16), nullable=False)
    last_reason = Column(Text, nullable=False)
    last_attempt_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_rotation_attempts_season", "season"),)


#: Rejections after which automatic collection stops retrying a game.
MAX_REJECTIONS = 3

__all__ = [
    "MAX_REJECTIONS",
    "RotationAttempt",
    "RotationGame",
    "RotationPlayerGame",
    "RotationStint",
]
