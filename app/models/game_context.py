"""Stored per-game team context for the Matchup (crf04/statsplus#466).

``game_contexts`` heads one scheduled game: the record a later game-lines table
(spread, total, team totals) attaches to by ``game_id``.  ``game_context_teams``
holds one row per team in that game: rest before the game, Season and Last 10
pace and ratings, and the last five results, all from games strictly before the
game's Eastern date.  ``scripts/game_context_refresh.py`` writes both from the
Canonical Game Ledger and the Event Catalog; the Matchup only reads them.
"""

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from . import Base


class GameContext(Base):
    """One Event Catalog game whose team context is stored."""

    __tablename__ = "game_contexts"

    game_id = Column(String(32), primary_key=True)
    season = Column(String(7), nullable=False)
    game_date = Column(Date, nullable=False)
    away_team_id = Column(Integer, nullable=False)
    home_team_id = Column(Integer, nullable=False)
    #: When this game's stored values last changed.
    computed_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_game_contexts_season_date", "season", "game_date"),)


class GameContextTeam(Base):
    """One team's context before one game."""

    __tablename__ = "game_context_teams"

    game_id = Column(
        String(32),
        ForeignKey("game_contexts.game_id", ondelete="CASCADE"),
        primary_key=True,
    )
    team_id = Column(Integer, primary_key=True)
    tricode = Column(String(8), nullable=False)
    is_home = Column(Boolean, nullable=False)
    #: The newest ledger game counted, or NULL when the team has none yet.
    through_game_date = Column(Date, nullable=True)
    previous_game_date = Column(Date, nullable=True)
    rest_days = Column(Integer, nullable=True)
    back_to_back = Column(Boolean, nullable=False)
    season_games = Column(Integer, nullable=False)
    season_possession_games = Column(Integer, nullable=False)
    season_pace = Column(Float, nullable=True)
    season_offensive_rating = Column(Float, nullable=True)
    season_defensive_rating = Column(Float, nullable=True)
    season_net_rating = Column(Float, nullable=True)
    last_10_games = Column(Integer, nullable=False)
    last_10_possession_games = Column(Integer, nullable=False)
    last_10_pace = Column(Float, nullable=True)
    last_10_offensive_rating = Column(Float, nullable=True)
    last_10_defensive_rating = Column(Float, nullable=True)
    last_10_net_rating = Column(Float, nullable=True)
    #: JSON list, newest first, of up to five results.
    last_5 = Column(Text, nullable=False)
