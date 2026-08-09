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
#: ``inactive_only``, ``ambiguous``, and ``unmatched`` are decision states that
#: also became current state: the catalog lists the mapped athlete as inactive
#: for the requested season, names two equally exact athletes, or no longer
#: lists the mapped athlete at all, so the claim is withdrawn from comparisons
#: without being retracted.
MAPPING_STATES = (
    "ambiguous",
    "auto",
    "inactive_only",
    "manual_approved",
    "manual_override",
    "mapping_conflict",
    "rejected",
    "unmatched",
)

#: Closed set of append-only decision states.  This mirrors
#: ``app.services.athlete_resolver.MappingResolutionState`` and is duplicated
#: here because models must not import services.
MAPPING_DECISION_STATES = frozenset(
    MAPPING_STATES
    + (
        "team_conflict",
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
            "(is_active = false AND mapping_state IN "
            "('ambiguous', 'inactive_only', 'mapping_conflict', 'rejected', "
            "'unmatched'))",
            name="ck_provider_mapping_active_state",
        ),
        # The conflicting canonical athlete is evidence for one state only.  A
        # row that has left ``mapping_conflict`` -- reactivated automatically,
        # remapped by an operator, or rejected -- has no conflict left to
        # review, so it may not keep naming one.
        CheckConstraint(
            "mapping_state = 'mapping_conflict' OR "
            "(conflict_canonical_player_id IS NULL AND conflict_canonical_name IS NULL)",
            name="ck_provider_mapping_conflict_fields",
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
    # Repeated evidence is suppressed per transition rather than for the
    # lifetime of an identity, so the same fingerprint may legitimately
    # reappear after a different observation or decision.  The repository
    # compares it with the latest decision for the identity instead.
    idempotency_key = Column(String(128), nullable=True)
    # When the provider was observed, as opposed to when the observation was
    # persisted.  Only board observations carry one; an operator decision is
    # its own event and is ordered by ``created_at``.
    observed_at = Column(DateTime(timezone=True), nullable=True)
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


class AthleteMappingDecisionContradiction(Base):
    """One typed provider evidence a contradictory observation asserted.

    A fail-closed conflict is recorded on one representative evidence, but what
    the operator has to review is every athlete the markets named.  The rest of
    the contradiction is therefore stored as typed rows beside the decision,
    exactly like its candidates, rather than surviving only in the observation
    that produced it.
    """

    __tablename__ = "athlete_mapping_decision_contradictions"

    decision_id = Column(
        Integer,
        ForeignKey("athlete_mapping_decisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Position in the decision's own deterministic evidence order.
    evidence_index = Column(Integer, primary_key=True)
    provider_athlete_id = Column(String(128), nullable=True)
    provider_canonical_id = Column(Integer, nullable=True)
    provider_name = Column(String(255), nullable=True)
    provider_team_id = Column(String(128), nullable=True)
    provider_team_canonical_id = Column(Integer, nullable=True)
    provider_team_name = Column(String(255), nullable=True)
    provider_team_abbreviation = Column(String(16), nullable=True)


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
        # A cleared rejection must carry every part of its clearing evidence,
        # and an active one must carry none of it, so a half-written clear can
        # never look like a governed decision.  The boolean literals stay valid
        # on both PostgreSQL and SQLite.
        CheckConstraint(
            "(is_active = true AND cleared_at IS NULL AND cleared_by IS NULL "
            "AND clear_reason IS NULL) OR "
            "(is_active = false AND cleared_at IS NOT NULL "
            "AND cleared_by IS NOT NULL AND clear_reason IS NOT NULL)",
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
    """Stable row used to serialize mutations for an identity.

    It also carries that identity's observation clock: the newest instant the
    provider was read for it.  The mark cannot be derived from the decision log
    because the log deliberately suppresses a repeated observation, which would
    lose the instant the repeat was taken at.  The row is already locked by
    every mutation, so raising the mark is part of the same transaction.
    """

    __tablename__ = "athlete_mapping_locks"

    provider = Column(String(32), primary_key=True)
    provider_athlete_id = Column(String(128), primary_key=True)
    last_observed_at = Column(DateTime(timezone=True), nullable=True)


__all__ = [
    "MAPPING_DECISION_STATES",
    "MAPPING_STATES",
    "AthleteMappingDecision",
    "AthleteMappingDecisionCandidate",
    "AthleteMappingDecisionContradiction",
    "AthleteMappingRejection",
    "AthleteMappingLock",
    "ProviderAthleteMapping",
]
