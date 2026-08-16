"""Window-aware raw team matchup facts and provider observations."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from . import Base


_WINDOW_CHECK = (
    "(window_kind = 'season' AND window_games = 0) OR "
    "(window_kind = 'rolling_games' AND window_games > 0)"
)


class TeamMatchupFactRow(Base):
    """One raw numerator and denominator for a team matchup metric."""

    __tablename__ = "team_matchup_facts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season = Column(String(7), nullable=False)
    as_of_date = Column(Date, nullable=False)
    window_kind = Column(String(16), nullable=False)
    window_games = Column(Integer, nullable=False)
    team_id = Column(Integer, nullable=False)
    base = Column(String(32), nullable=False)
    slice_key = Column(String(128), nullable=False)
    stat_key = Column(String(64), nullable=False)
    raw_value = Column(Float, nullable=True)
    denominator_value = Column(Float, nullable=True)
    denominator_unit = Column(String(16), nullable=True)
    provider = Column(String(32), nullable=False)
    window_start_date = Column(Date, nullable=True)
    window_end_date = Column(Date, nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    #: Exact governed game IDs this team's window aggregated (JSON text) and the
    #: deterministic ledger checksum of the selected game set.  ``NULL`` on
    #: provider-collected legacy facts; ledger-owned facts always carry both.
    game_ids = Column(Text, nullable=True)
    ledger_checksum = Column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(_WINDOW_CHECK, name="ck_team_matchup_facts_window"),
        CheckConstraint(
            "denominator_unit IS NULL OR denominator_unit IN ('minutes', 'seconds')",
            name="ck_team_matchup_facts_denominator_unit",
        ),
        UniqueConstraint(
            "season",
            "as_of_date",
            "window_kind",
            "window_games",
            "team_id",
            "base",
            "slice_key",
            "stat_key",
            name="uq_team_matchup_fact_identity",
        ),
    )


class TeamMatchupSurfaceObservationRow(Base):
    """Freshness and provider availability for one surface/window."""

    __tablename__ = "team_matchup_surface_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season = Column(String(7), nullable=False)
    as_of_date = Column(Date, nullable=False)
    window_kind = Column(String(16), nullable=False)
    window_games = Column(Integer, nullable=False)
    surface = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False)
    unavailable_reason = Column(String(64), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    #: Exact governed game IDs the window's surface observed (JSON text) plus the
    #: deterministic ledger checksum of that selected game set; ``NULL`` on
    #: provider-collected legacy observations.
    game_ids = Column(Text, nullable=True)
    ledger_checksum = Column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(_WINDOW_CHECK, name="ck_team_matchup_observations_window"),
        CheckConstraint(
            "status IN ('available', 'unavailable', 'missing')",
            name="ck_team_matchup_observations_status",
        ),
        UniqueConstraint(
            "season",
            "as_of_date",
            "window_kind",
            "window_games",
            "surface",
            name="uq_team_matchup_surface_observation_identity",
        ),
    )


__all__ = ["TeamMatchupFactRow", "TeamMatchupSurfaceObservationRow"]
