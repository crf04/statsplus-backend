"""Durable canonical player game-log facts and publication freshness."""

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Index, Integer, String

from . import Base


class PlayerGameLog(Base):
    """One normalized NBA box-score row for one player and game."""

    __tablename__ = "player_game_logs"

    season = Column(String(7), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    game_id = Column(String(32), primary_key=True)

    player_name = Column(String(255), nullable=False)
    game_date = Column(Date, nullable=False)
    team_id = Column(Integer, nullable=False)
    team_tricode = Column(String(8), nullable=False)
    opponent_team_id = Column(Integer, nullable=False)
    opponent_team_tricode = Column(String(8), nullable=False)
    is_home = Column(Boolean, nullable=False)

    minutes = Column(Float, nullable=False)
    points = Column(Integer, nullable=False)
    rebounds = Column(Integer, nullable=False)
    assists = Column(Integer, nullable=False)
    field_goals_made = Column(Integer, nullable=False)
    field_goals_attempted = Column(Integer, nullable=False)
    three_pointers_made = Column(Integer, nullable=False)
    three_pointers_attempted = Column(Integer, nullable=False)
    turnovers = Column(Integer, nullable=False)
    steals = Column(Integer, nullable=False)
    blocks = Column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "ix_player_game_logs_player_date",
            "season",
            "player_id",
            "game_date",
            "game_id",
        ),
        Index(
            "ix_player_game_logs_opponent_date",
            "season",
            "opponent_team_id",
            "game_date",
            "game_id",
        ),
        Index("ix_player_game_logs_game", "season", "game_id"),
    )


class PlayerGameLogRefresh(Base):
    """Latest complete player-game-log publication for one NBA season."""

    __tablename__ = "player_game_log_refreshes"

    season = Column(String(7), primary_key=True)
    source_provider = Column(String(32), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    row_count = Column(Integer, nullable=False)


__all__ = ["PlayerGameLog", "PlayerGameLogRefresh"]
