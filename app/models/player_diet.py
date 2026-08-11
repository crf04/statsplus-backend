"""Durable Season-only player Diet Share facts and Base observations."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
)

from . import Base


class PlayerDietFactRow(Base):
    """One canonical player's raw share and volume for one offensive slice."""

    __tablename__ = "player_diet_facts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season = Column(String(7), nullable=False)
    player_id = Column(Integer, nullable=False)
    base = Column(String(32), nullable=False)
    slice_key = Column(String(128), nullable=False)
    share = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    games_played = Column(Integer, nullable=False)
    volume_unit = Column(String(32), nullable=False)
    provider = Column(String(32), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("player_id > 0", name="ck_player_diet_player_id"),
        CheckConstraint("share >= 0 AND share <= 1", name="ck_player_diet_share"),
        CheckConstraint("volume >= 0", name="ck_player_diet_volume"),
        CheckConstraint("games_played > 0", name="ck_player_diet_games_played"),
        CheckConstraint(
            "base IN ('play_types', 'shot_zones', 'shot_types', "
            "'assist_locations')",
            name="ck_player_diet_base",
        ),
        CheckConstraint(
            "(base = 'play_types' AND volume_unit = 'possessions') OR "
            "(base IN ('shot_zones', 'shot_types') AND "
            "volume_unit = 'field_goal_attempts') OR "
            "(base = 'assist_locations' AND volume_unit = 'assists')",
            name="ck_player_diet_volume_unit",
        ),
        UniqueConstraint(
            "season",
            "player_id",
            "base",
            "slice_key",
            name="uq_player_diet_fact_identity",
        ),
    )


class PlayerDietSurfaceObservationRow(Base):
    """Latest collection status for one Season player Diet Base."""

    __tablename__ = "player_diet_surface_observations"

    season = Column(String(7), primary_key=True)
    base = Column(String(32), primary_key=True)
    status = Column(String(16), nullable=False)
    unavailable_reason = Column(String(64), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "base IN ('play_types', 'shot_zones', 'shot_types', "
            "'assist_locations')",
            name="ck_player_diet_observation_base",
        ),
        CheckConstraint(
            "status IN ('available', 'unavailable', 'missing')",
            name="ck_player_diet_observation_status",
        ),
        CheckConstraint(
            "(status = 'available' AND unavailable_reason IS NULL) OR "
            "(status IN ('unavailable', 'missing') AND "
            "unavailable_reason IS NOT NULL)",
            name="ck_player_diet_observation_reason",
        ),
    )


__all__ = ["PlayerDietFactRow", "PlayerDietSurfaceObservationRow"]
