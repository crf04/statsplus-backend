"""Application-owned persistence models for provider athlete identity.

The provider athlete ID is evidence, not a canonical identity.  Mapping rows
therefore retain the source labels and team facts that led to a decision while
the append-only decision log records every automatic or operator action.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import expression

from . import Base


#: Closed set of mapping states one row may hold as current state.
MAPPING_STATES = (
    "auto",
    "manual_approved",
    "manual_override",
    "mapping_conflict",
    "rejected",
)

#: Closed set of append-only decision states.  This mirrors
#: ``app.services.athlete_resolver.MappingResolutionState`` and is duplicated
#: here because models must not import services.
MAPPING_DECISION_STATES = frozenset(
    MAPPING_STATES
    + (
        "ambiguous",
        "inactive_only",
        "team_conflict",
        "unmatched",
        "missing_identity",
        "rejection_cleared",
    )
)


def _quoted_states(states) -> str:
    return ", ".join(f"'{state}'" for state in sorted(states))


class ProviderAthleteMapping(Base):
    """Current mapping state for one provider athlete identity."""

    __tablename__ = "provider_athlete_mappings"

    provider = Column(String(32), primary_key=True)
    provider_athlete_id = Column(String(128), primary_key=True)
    mapping_state = Column(String(32), nullable=False)
    is_active = Column(
        Boolean, nullable=False, default=False, server_default=expression.false()
    )

    season = Column(String(7), nullable=True)
    canonical_player_id = Column(Integer, nullable=True)
    canonical_name = Column(String(255), nullable=True)
    canonical_team_id = Column(Integer, nullable=True)
    canonical_team_name = Column(String(255), nullable=True)
    canonical_team_abbreviation = Column(String(16), nullable=True)

    provider_name = Column(String(255), nullable=True)
    provider_team_id = Column(String(128), nullable=True)
    provider_team_canonical_id = Column(Integer, nullable=True)
    provider_team_name = Column(String(255), nullable=True)
    provider_team_abbreviation = Column(String(16), nullable=True)

    conflict_canonical_player_id = Column(Integer, nullable=True)
    conflict_canonical_name = Column(String(255), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"mapping_state IN ({_quoted_states(MAPPING_STATES)})",
            name="ck_provider_mapping_state",
        ),
        # ``true``/``false`` keep the comparison valid on PostgreSQL, where
        # a boolean column cannot be compared with an integer literal.
        CheckConstraint(
            "(is_active = true AND mapping_state IN "
            "('auto', 'manual_approved', 'manual_override')) OR "
            "(is_active = false AND mapping_state IN ('mapping_conflict', 'rejected'))",
            name="ck_provider_mapping_active_state",
        ),
        Index(
            "ix_provider_athlete_mappings_active",
            "provider",
            "provider_athlete_id",
            "is_active",
        ),
    )


class AthleteMappingDecision(Base):
    """Append-only decision/audit record for one provider athlete."""

    __tablename__ = "athlete_mapping_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(32), nullable=False)
    provider_athlete_id = Column(String(128), nullable=False)
    requested_season = Column(String(7), nullable=True)
    decision_state = Column(String(32), nullable=False)
    canonical_player_id = Column(Integer, nullable=True)
    canonical_name = Column(String(255), nullable=True)
    canonical_team_id = Column(Integer, nullable=True)
    canonical_team_name = Column(String(255), nullable=True)
    canonical_team_abbreviation = Column(String(16), nullable=True)

    provider_name = Column(String(255), nullable=True)
    provider_team_id = Column(String(128), nullable=True)
    provider_team_canonical_id = Column(Integer, nullable=True)
    provider_team_name = Column(String(255), nullable=True)
    provider_team_abbreviation = Column(String(16), nullable=True)

    operator_id = Column(String(128), nullable=True)
    reason = Column(Text, nullable=True)
    idempotency_key = Column(String(128), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"decision_state IN ({_quoted_states(MAPPING_DECISION_STATES)})",
            name="ck_athlete_mapping_decision_state",
        ),
        Index(
            "ix_athlete_mapping_decisions_identity",
            "provider",
            "provider_athlete_id",
            "created_at",
        ),
    )


class AthleteMappingDecisionCandidate(Base):
    """One canonical athlete an unresolved observation could not choose between.

    Ambiguous and inactive-only evidence is only actionable if the operator can
    see which catalog rows were considered, so the candidates are stored as
    typed rows rather than as an opaque blob on the decision.
    """

    __tablename__ = "athlete_mapping_decision_candidates"

    decision_id = Column(
        Integer,
        ForeignKey("athlete_mapping_decisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    canonical_player_id = Column(Integer, primary_key=True)
    canonical_name = Column(String(255), nullable=True)
    canonical_team_id = Column(Integer, nullable=True)
    canonical_team_name = Column(String(255), nullable=True)
    canonical_team_abbreviation = Column(String(16), nullable=True)
    is_active_for_season = Column(
        Boolean, nullable=False, default=False, server_default=expression.false()
    )


class AthleteMappingRejection(Base):
    """Durable suppression state for one provider athlete identity."""

    __tablename__ = "athlete_mapping_rejections"

    provider = Column(String(32), primary_key=True)
    provider_athlete_id = Column(String(128), primary_key=True)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=expression.true()
    )
    reason = Column(Text, nullable=False)
    operator_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    cleared_at = Column(DateTime(timezone=True), nullable=True)
    cleared_by = Column(String(128), nullable=True)
    clear_reason = Column(Text, nullable=True)

    __table_args__ = (
        # A cleared rejection must carry its clearing evidence, and the
        # boolean literals stay valid on both PostgreSQL and SQLite.
        CheckConstraint(
            "(is_active = true AND cleared_at IS NULL) OR "
            "(is_active = false AND cleared_at IS NOT NULL)",
            name="ck_mapping_rejection_active",
        ),
        Index(
            "ix_athlete_mapping_rejections_active",
            "provider",
            "provider_athlete_id",
            "is_active",
        ),
    )


class AthleteMappingLock(Base):
    """Stable row used to serialize mutations for an identity."""

    __tablename__ = "athlete_mapping_locks"

    provider = Column(String(32), primary_key=True)
    provider_athlete_id = Column(String(128), primary_key=True)


__all__ = [
    "MAPPING_DECISION_STATES",
    "MAPPING_STATES",
    "AthleteMappingDecision",
    "AthleteMappingDecisionCandidate",
    "AthleteMappingRejection",
    "AthleteMappingLock",
    "ProviderAthleteMapping",
]
