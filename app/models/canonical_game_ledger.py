"""Durable facts for the governed Canonical Game Ledger.

The ledger is deliberately additive to the ``player_game_logs`` tables from
#66.  A game is the unit of publication: the game identity, both team fact
sets, and every participating player fact are replaced together.  The tables
store count primitives only; rates and rolling windows belong to the
materialization service.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)

from . import Base


class CanonicalGameLedgerGame(Base):
    """One immutable identity/checksum record for a completed Regular Season game."""

    __tablename__ = "canonical_game_ledger_games"

    game_id = Column(String(32), primary_key=True)
    season = Column(String(7), nullable=False, index=True)
    season_type = Column(String(20), nullable=False, default="Regular Season")
    game_date = Column(Date, nullable=False)
    home_team_id = Column(Integer, nullable=False)
    home_team_tricode = Column(String(8), nullable=False)
    away_team_id = Column(Integer, nullable=False)
    away_team_tricode = Column(String(8), nullable=False)
    status = Column(String(32), nullable=False, default="final")
    source_observation_id = Column(String(128), nullable=False)
    checksum = Column(String(64), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "season_type = 'Regular Season'",
            name="ck_ledger_game_regular_season",
        ),
        CheckConstraint("home_team_id <> away_team_id", name="ck_ledger_game_distinct_teams"),
        Index("ix_ledger_games_season_date", "season", "game_date", "game_id"),
    )


class CanonicalGameLedgerTeamFact(Base):
    """Count primitives for one team in one governed game."""

    __tablename__ = "canonical_game_ledger_team_facts"

    game_id = Column(String(32), primary_key=True)
    team_id = Column(Integer, primary_key=True)
    team_tricode = Column(String(8), nullable=False)
    opponent_team_id = Column(Integer, nullable=False)
    opponent_team_tricode = Column(String(8), nullable=False)
    is_home = Column(Boolean, nullable=False)
    points = Column(Integer, nullable=False)
    field_goals_made = Column(Integer, nullable=False)
    field_goals_attempted = Column(Integer, nullable=False)
    two_pointers_made = Column(Integer, nullable=False)
    two_pointers_attempted = Column(Integer, nullable=False)
    three_pointers_made = Column(Integer, nullable=False)
    three_pointers_attempted = Column(Integer, nullable=False)
    free_throws_made = Column(Integer, nullable=False)
    free_throws_attempted = Column(Integer, nullable=False)
    offensive_rebounds = Column(Integer, nullable=False)
    defensive_rebounds = Column(Integer, nullable=False)
    rebounds = Column(Integer, nullable=False)
    assists = Column(Integer, nullable=False)
    turnovers = Column(Integer, nullable=False)
    steals = Column(Integer, nullable=False)
    blocks = Column(Integer, nullable=False)
    personal_fouls = Column(Integer, nullable=False)
    # Sum of the player minutes retained for this team-game.  A team rate can
    # therefore be normalized to a true 48-minute denominator without using a
    # provider percentage or silently assuming every game lasted regulation.
    team_minutes = Column(Float, nullable=False, default=0.0)
    possessions = Column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("team_id <> opponent_team_id", name="ck_ledger_team_distinct_opponent"),
        Index("ix_ledger_team_facts_team_game", "team_id", "game_id"),
    )


class CanonicalGameLedgerPlayerFact(Base):
    """Full-game count primitives for one participating player."""

    __tablename__ = "canonical_game_ledger_player_facts"

    game_id = Column(String(32), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    team_id = Column(Integer, primary_key=True)
    player_name = Column(String(255), nullable=False)
    team_tricode = Column(String(8), nullable=False)
    minutes = Column(Float, nullable=False)
    points = Column(Integer, nullable=False)
    field_goals_made = Column(Integer, nullable=False)
    field_goals_attempted = Column(Integer, nullable=False)
    two_pointers_made = Column(Integer, nullable=False)
    two_pointers_attempted = Column(Integer, nullable=False)
    three_pointers_made = Column(Integer, nullable=False)
    three_pointers_attempted = Column(Integer, nullable=False)
    free_throws_made = Column(Integer, nullable=False)
    free_throws_attempted = Column(Integer, nullable=False)
    offensive_rebounds = Column(Integer, nullable=False)
    defensive_rebounds = Column(Integer, nullable=False)
    rebounds = Column(Integer, nullable=False)
    assists = Column(Integer, nullable=False)
    turnovers = Column(Integer, nullable=False)
    steals = Column(Integer, nullable=False)
    blocks = Column(Integer, nullable=False)
    personal_fouls = Column(Integer, nullable=False)
    # PBP Stats exposes these only for providers that collect assist-location
    # evidence.  They are nullable so a FullGame-only observation remains a
    # valid ledger game while the derived assist stream can fail closed.
    two_point_assists = Column(Integer, nullable=True)
    three_point_assists = Column(Integer, nullable=True)
    arc3_assists = Column(Integer, nullable=True)
    corner3_assists = Column(Integer, nullable=True)
    at_rim_assists = Column(Integer, nullable=True)
    short_mid_range_assists = Column(Integer, nullable=True)
    long_mid_range_assists = Column(Integer, nullable=True)
    possessions = Column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("minutes >= 0", name="ck_ledger_player_minutes"),
        Index("ix_ledger_player_facts_player_date", "player_id", "game_id"),
        Index("ix_ledger_player_facts_team_game", "team_id", "game_id"),
    )


class LedgerBackfillState(Base):
    """Resumable progress and bounded failure evidence for one season."""

    __tablename__ = "canonical_game_ledger_backfill"

    season = Column(String(7), primary_key=True)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    cursor_game_id = Column(String(32), nullable=True)
    completed_game_ids = Column(Text, nullable=False, default="[]")
    failed_game_ids = Column(Text, nullable=False, default="[]")
    status = Column(String(24), nullable=False, default="in_progress")
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(String(128), nullable=True)


class LedgerPublication(Base):
    """Materialized ledger stream metadata; payload rows remain in owned tables."""

    __tablename__ = "canonical_game_ledger_publications"

    stream_key = Column(String(96), primary_key=True)
    season = Column(String(7), primary_key=True)
    window_kind = Column(String(16), primary_key=True)
    window_games = Column(Integer, primary_key=True, default=0)
    as_of = Column(Date, primary_key=True)
    status = Column(String(24), nullable=False)
    checksum = Column(String(64), nullable=False)
    payload = Column(Text, nullable=False)
    game_count = Column(Integer, nullable=False)
    team_count = Column(Integer, nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(128), nullable=True)


__all__ = [
    "CanonicalGameLedgerGame",
    "CanonicalGameLedgerPlayerFact",
    "CanonicalGameLedgerTeamFact",
    "LedgerBackfillState",
    "LedgerPublication",
]
