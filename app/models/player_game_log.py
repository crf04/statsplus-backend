"""Durable canonical player game-log facts and publication freshness."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class PlayerGameLog(Base):
    """One normalized NBA box-score row for one player and game."""

    __tablename__ = "player_game_logs"

    season = Column(String(7), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    game_id = Column(String(32), primary_key=True)
    season_type = Column(String(20), nullable=False)

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
    free_throws_made = Column(Integer, nullable=False)
    free_throws_attempted = Column(Integer, nullable=False)
    offensive_rebounds = Column(Integer, nullable=False)
    defensive_rebounds = Column(Integer, nullable=False)
    turnovers = Column(Integer, nullable=False)
    steals = Column(Integer, nullable=False)
    blocks = Column(Integer, nullable=False)
    personal_fouls = Column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "season_type IN ('Regular Season', 'Playoffs')",
            name="ck_player_game_logs_season_type",
        ),
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
    source_row_count = Column(Integer, nullable=False)
    identity_source_row_count = Column(Integer, nullable=False)
    #: ``complete`` means every governed completed event through the observation
    #: time is durably covered by PBP-derived facts; anything else — including
    #: legacy NBA-derived publications — stays ``in_progress`` so a
    #: database-first read never mistakes unverified data for a complete one.
    publication_status = Column(
        String(16), nullable=False, server_default="in_progress"
    )

    __table_args__ = (
        CheckConstraint(
            "source_row_count >= identity_source_row_count "
            "AND identity_source_row_count >= row_count "
            "AND row_count >= 0",
            name="ck_player_game_log_refresh_counts",
        ),
    )


class PlayerGameLogSync(Base):
    """Bounded per-game synchronization evidence for one season.

    Each row records the normalized row count, a checksum of the game's facts,
    the source provider, and the retrieval time for one governed game.  The
    checksum makes reconciliation idempotent: an unchanged game is not
    rewritten, while a stat correction changes the checksum and replaces the
    affected game's rows atomically.
    """

    __tablename__ = "player_game_log_sync"

    season = Column(String(7), primary_key=True)
    game_id = Column(String(32), primary_key=True)
    season_type = Column(String(20), nullable=False)
    status = Column(String(16), nullable=False)
    checksum = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False)
    source_provider = Column(String(32), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('complete', 'failed')",
            name="ck_player_game_log_sync_status",
        ),
        Index("ix_player_game_log_sync_season", "season"),
    )


class PublicationPlayerGameLog(Base):
    """One indexed row from an immutable player-game-log publication."""

    __tablename__ = "publication_player_game_logs"

    publication_id = Column(
        String(36),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        primary_key=True,
    )
    player_id = Column(Integer, primary_key=True)
    game_id = Column(String(32), primary_key=True)
    game_date = Column(Date, nullable=False)
    opponent_team_id = Column(Integer, nullable=False)
    row_payload = Column(Text, nullable=False)

    __table_args__ = (
        Index(
            "ix_publication_player_game_logs_player_date",
            "publication_id",
            "player_id",
            "game_date",
            "game_id",
        ),
        Index(
            "ix_publication_player_game_logs_opponent",
            "publication_id",
            "opponent_team_id",
            "player_id",
            "game_date",
        ),
    )


__all__ = [
    "PlayerGameLog",
    "PlayerGameLogRefresh",
    "PlayerGameLogSync",
    "PublicationPlayerGameLog",
]
