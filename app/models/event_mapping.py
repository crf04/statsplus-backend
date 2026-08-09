"""Application-owned persistence models for provider event identity.

A provider event ID is evidence, not a canonical identity.  Mapping rows
therefore retain the provider's own event label, start time, and home/away team
facts beside the canonical NBA game the evidence resolved to, while the
append-only decision log records every automatic or operator action.

Governance mirrors ``app.models.athlete_mapping`` because the rules are the
same: one current row per provider identity, an append-only audit, durable
rejections, and a per-identity lock row carrying the identity's observation
clock.  The typed evidence differs, so the columns do.
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
#: ``ambiguous``, ``unmatched``, and ``replacement_pending`` are observation
#: states that also became current state: the schedule offers two equally near
#: events, offers none at all, or no longer lists the canonical game the
#: identity was mapped to.  Each withdraws the claim from board comparisons
#: without retracting it.
EVENT_MAPPING_STATES = (
    "ambiguous",
    "auto",
    "manual_approved",
    "manual_override",
    "mapping_conflict",
    "rejected",
    "replacement_pending",
    "unmatched",
)

#: Closed set of append-only decision states.  This mirrors
#: ``app.services.event_resolver.EventResolutionState`` and is duplicated here
#: because models must not import services.  ``event_catalog_unavailable`` is a
#: resolution outcome that is never durable -- there is no comparison identity
#: to record -- so it is absent from both sets, as is every observation of an
#: identity the provider named no stable event ID for.
EVENT_MAPPING_DECISION_STATES = frozenset(
    EVENT_MAPPING_STATES + ("rejection_cleared",)
)


def _quoted_states(states) -> str:
    return ", ".join(f"'{state}'" for state in sorted(states))


class ProviderEventMapping(Base):
    """Current mapping state for one provider event identity."""

    __tablename__ = "provider_event_mappings"

    provider = Column(String(32), primary_key=True)
    provider_event_id = Column(String(128), primary_key=True)
    mapping_state = Column(String(32), nullable=False)
    is_active = Column(
        Boolean, nullable=False, default=False, server_default=expression.false()
    )

    season = Column(String(7), nullable=True)
    canonical_event_id = Column(String(32), nullable=True)
    canonical_scheduled_at = Column(DateTime(timezone=True), nullable=True)
    canonical_home_team_id = Column(Integer, nullable=True)
    canonical_home_team_name = Column(String(128), nullable=True)
    canonical_home_team_tricode = Column(String(8), nullable=True)
    canonical_away_team_id = Column(Integer, nullable=True)
    canonical_away_team_name = Column(String(128), nullable=True)
    canonical_away_team_tricode = Column(String(8), nullable=True)

    provider_event_label = Column(String(255), nullable=True)
    provider_starts_at = Column(DateTime(timezone=True), nullable=True)
    provider_status_label = Column(String(128), nullable=True)
    provider_home_team_id = Column(String(128), nullable=True)
    provider_home_team_canonical_id = Column(Integer, nullable=True)
    provider_home_team_name = Column(String(255), nullable=True)
    provider_home_team_abbreviation = Column(String(16), nullable=True)
    provider_away_team_id = Column(String(128), nullable=True)
    provider_away_team_canonical_id = Column(Integer, nullable=True)
    provider_away_team_name = Column(String(255), nullable=True)
    provider_away_team_abbreviation = Column(String(16), nullable=True)

    conflict_canonical_event_id = Column(String(32), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"mapping_state IN ({_quoted_states(EVENT_MAPPING_STATES)})",
            name="ck_provider_event_mapping_state",
        ),
        # ``true``/``false`` keep the comparison valid on PostgreSQL, where a
        # boolean column cannot be compared with an integer literal.
        CheckConstraint(
            "(is_active = true AND mapping_state IN "
            "('auto', 'manual_approved', 'manual_override')) OR "
            "(is_active = false AND mapping_state IN "
            "('ambiguous', 'mapping_conflict', 'rejected', "
            "'replacement_pending', 'unmatched'))",
            name="ck_provider_event_mapping_active_state",
        ),
        # The conflicting canonical game is evidence for one state only.  A row
        # that has left ``mapping_conflict`` -- mapped automatically again,
        # remapped by an operator, or rejected -- has no conflict left to
        # review, so it may not keep naming one.
        CheckConstraint(
            "mapping_state = 'mapping_conflict' OR "
            "conflict_canonical_event_id IS NULL",
            name="ck_provider_event_mapping_conflict_fields",
        ),
        Index(
            "ix_provider_event_mappings_active",
            "provider",
            "provider_event_id",
            "is_active",
        ),
    )


class EventMappingDecision(Base):
    """Append-only decision/audit record for one provider event."""

    __tablename__ = "event_mapping_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(32), nullable=False)
    provider_event_id = Column(String(128), nullable=False)
    requested_season = Column(String(7), nullable=True)
    decision_state = Column(String(32), nullable=False)

    canonical_event_id = Column(String(32), nullable=True)
    canonical_scheduled_at = Column(DateTime(timezone=True), nullable=True)
    canonical_home_team_id = Column(Integer, nullable=True)
    canonical_home_team_name = Column(String(128), nullable=True)
    canonical_home_team_tricode = Column(String(8), nullable=True)
    canonical_away_team_id = Column(Integer, nullable=True)
    canonical_away_team_name = Column(String(128), nullable=True)
    canonical_away_team_tricode = Column(String(8), nullable=True)

    provider_event_label = Column(String(255), nullable=True)
    provider_starts_at = Column(DateTime(timezone=True), nullable=True)
    provider_status_label = Column(String(128), nullable=True)
    provider_home_team_id = Column(String(128), nullable=True)
    provider_home_team_canonical_id = Column(Integer, nullable=True)
    provider_home_team_name = Column(String(255), nullable=True)
    provider_home_team_abbreviation = Column(String(16), nullable=True)
    provider_away_team_id = Column(String(128), nullable=True)
    provider_away_team_canonical_id = Column(Integer, nullable=True)
    provider_away_team_name = Column(String(255), nullable=True)
    provider_away_team_abbreviation = Column(String(16), nullable=True)

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
            f"decision_state IN ({_quoted_states(EVENT_MAPPING_DECISION_STATES)})",
            name="ck_event_mapping_decision_state",
        ),
        Index(
            "ix_event_mapping_decisions_identity",
            "provider",
            "provider_event_id",
            "created_at",
        ),
    )


class EventMappingDecisionCandidate(Base):
    """One canonical NBA game an unresolved observation could not choose.

    Ambiguous, unmatched, replacement, and conflicting evidence is only
    actionable if the operator can see which scheduled games were considered
    and how far each one was from the provider's start time, so the candidates
    are typed rows rather than an opaque blob on the decision.
    """

    __tablename__ = "event_mapping_decision_candidates"

    decision_id = Column(
        Integer,
        ForeignKey("event_mapping_decisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    canonical_event_id = Column(String(32), primary_key=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    home_team_id = Column(Integer, nullable=True)
    home_team_name = Column(String(128), nullable=True)
    home_team_tricode = Column(String(8), nullable=True)
    away_team_id = Column(Integer, nullable=True)
    away_team_name = Column(String(128), nullable=True)
    away_team_tricode = Column(String(8), nullable=True)
    #: Signed distance from the provider's reported start time, in seconds.
    #: ``None`` when the provider reported no start time to compare with.
    start_offset_seconds = Column(Integer, nullable=True)
    is_postponed = Column(
        Boolean, nullable=False, default=False, server_default=expression.false()
    )
    #: Whether the requested season's catalog still lists this game.  A claim
    #: whose canonical game was replaced is recorded as a candidate that is no
    #: longer scheduled, which is what distinguishes a withdrawn claim from an
    #: identity that simply matched nothing.
    is_scheduled = Column(
        Boolean, nullable=False, default=True, server_default=expression.true()
    )


class EventMappingRejection(Base):
    """Durable suppression state for one provider event identity."""

    __tablename__ = "event_mapping_rejections"

    provider = Column(String(32), primary_key=True)
    provider_event_id = Column(String(128), primary_key=True)
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
        # and an active one none of it, so a half-written clear can never look
        # like a governed decision.  The boolean literals stay valid on both
        # PostgreSQL and SQLite.
        CheckConstraint(
            "(is_active = true AND cleared_at IS NULL AND cleared_by IS NULL "
            "AND clear_reason IS NULL) OR "
            "(is_active = false AND cleared_at IS NOT NULL "
            "AND cleared_by IS NOT NULL AND clear_reason IS NOT NULL)",
            name="ck_event_mapping_rejection_active",
        ),
        Index(
            "ix_event_mapping_rejections_active",
            "provider",
            "provider_event_id",
            "is_active",
        ),
    )


class EventMappingLock(Base):
    """Stable row used to serialize mutations for one provider event identity.

    It also carries that identity's observation clock: the newest instant the
    provider was read for it.  The mark cannot be derived from the decision log
    because the log deliberately suppresses a repeated observation, which would
    lose the instant the repeat was taken at.  The row is already locked by
    every mutation, so raising the mark is part of the same transaction.
    """

    __tablename__ = "event_mapping_locks"

    provider = Column(String(32), primary_key=True)
    provider_event_id = Column(String(128), primary_key=True)
    last_observed_at = Column(DateTime(timezone=True), nullable=True)


__all__ = [
    "EVENT_MAPPING_DECISION_STATES",
    "EVENT_MAPPING_STATES",
    "EventMappingDecision",
    "EventMappingDecisionCandidate",
    "EventMappingLock",
    "EventMappingRejection",
    "ProviderEventMapping",
]
