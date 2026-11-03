"""Stored NBA.com player-tracking values per player-game (crf04/statsplus#349).

Rebound chances and potential assists arrive separately from the box score and
can lag it by hours, so they live apart from the game ledger and are joined
onto game-log rows at read time.  Every value is nullable: ``NULL`` means not
collected or absent from its feed, never zero.
"""

from sqlalchemy import Column, Date, DateTime, Index, Integer, String

from . import Base


class PlayerGameTracking(Base):
    """One player's tracked rebound chances and potential assists in one game."""

    __tablename__ = "player_game_tracking"

    game_id = Column(String(10), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    season = Column(String(7), nullable=False)
    game_date = Column(Date, nullable=False)
    reb_chances = Column(Integer, nullable=True)
    oreb_chances = Column(Integer, nullable=True)
    dreb_chances = Column(Integer, nullable=True)
    potential_ast = Column(Integer, nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_player_game_tracking_season_player", "season", "player_id"),
        Index("ix_player_game_tracking_season_game", "season", "game_id"),
    )


class PlayerTrackingAttempt(Base):
    """The last unsuccessful collection attempt for a game with no stored tracking.

    Orders retries least-recently-tried first, so games whose tracking never
    publishes cannot starve newer ones under a bounded run.
    """

    __tablename__ = "player_tracking_attempts"

    game_id = Column(String(10), primary_key=True)
    season = Column(String(7), nullable=False)
    outcome = Column(String(16), nullable=False)
    last_attempt_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_player_tracking_attempts_season", "season"),)
