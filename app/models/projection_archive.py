"""Immutable projection evidence and the database-first live read model."""

from enum import StrEnum

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
    UniqueConstraint,
    func,
)

from . import Base


class ProviderPollOutcome(StrEnum):
    """Closed application and persistence vocabulary for provider poll outcomes."""

    CHANGED = "changed"
    PARTIAL = "partial"
    REMATERIALIZED = "rematerialized"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class MaterializationOutcome(StrEnum):
    """Closed application vocabulary for one fenced Latest-state decision."""

    ADVANCED = "advanced"
    OLDER_NOT_PROMOTED = "older_not_promoted"
    SAME_TIME_NOT_PROMOTED = "same_time_not_promoted"
    UNCHANGED = "unchanged"


_POLL_OUTCOME_SQL = ", ".join(
    f"'{outcome.value}'" for outcome in ProviderPollOutcome
)


class ProjectionArchiveScopeLock(Base):
    """Stable row used to serialize one provider/query materialization scope."""

    __tablename__ = "projection_archive_scope_locks"

    provider = Column(String(64), primary_key=True)
    season = Column(String(7), primary_key=True)
    query_key = Column(String(72), primary_key=True)
    active_generation_id = Column(String(72), nullable=True)
    mapping_replayed_at = Column(DateTime(timezone=True), nullable=True)
    mapping_replayed_retrieved_at = Column(DateTime(timezone=True), nullable=True)


class ProviderPoll(Base):
    """One accepted attempt to archive a normalized provider observation."""

    __tablename__ = "projection_provider_polls"

    poll_id = Column(String(72), primary_key=True)
    provider = Column(String(64), nullable=False)
    season = Column(String(7), nullable=False)
    query_key = Column(String(72), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String(24), nullable=False)
    promoted = Column(Boolean, nullable=False, default=False, server_default="false")
    failure_reason = Column(String(64), nullable=True)
    snapshot_id = Column(
        String(72),
        ForeignKey("projection_provider_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=True,
    )
    generation_id = Column(String(72), nullable=True)
    observation_count = Column(Integer, nullable=False, default=0, server_default="0")
    duration_ms = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"outcome IN ({_POLL_OUTCOME_SQL})",
            name="ck_projection_provider_poll_outcome",
        ),
        CheckConstraint(
            "(outcome = 'failed' AND promoted = FALSE AND snapshot_id IS NULL AND generation_id IS NULL "
            "AND retrieved_at IS NULL AND failure_reason IS NOT NULL) OR "
            "(outcome <> 'failed' AND snapshot_id IS NOT NULL AND generation_id IS NOT NULL "
            "AND retrieved_at IS NOT NULL AND failure_reason IS NULL)",
            name="ck_projection_provider_poll_payload",
        ),
        Index(
            "ix_projection_provider_polls_scope_completed",
            "provider",
            "season",
            "query_key",
            "completed_at",
        ),
    )


class ProjectionProviderSnapshot(Base):
    """One immutable, checksummed normalized Provider Snapshot document."""

    __tablename__ = "projection_provider_snapshots"

    snapshot_id = Column(String(72), primary_key=True)
    provider = Column(String(64), nullable=False)
    season = Column(String(7), nullable=False)
    query_key = Column(String(72), nullable=False)
    contract_version = Column(String(32), nullable=False)
    snapshot_status = Column(String(16), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False)
    checksum = Column(String(64), nullable=False, unique=True)
    content_checksum = Column(String(64), nullable=False)
    evidence_document = Column(Text, nullable=False)

    __table_args__ = (
        Index(
            "ix_projection_provider_snapshots_scope_retrieved",
            "provider",
            "season",
            "query_key",
            "retrieved_at",
        ),
        Index(
            "ix_projection_provider_snapshots_scope_content",
            "provider",
            "season",
            "query_key",
            "content_checksum",
        ),
    )


class ProjectionObservation(Base):
    """One immutable market observation linked to its source snapshot."""

    __tablename__ = "projection_observations"

    observation_id = Column(String(72), primary_key=True)
    snapshot_id = Column(
        String(72),
        ForeignKey("projection_provider_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation_id = Column(
        String(72),
        ForeignKey(
            "projection_materialization_generations.generation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source_poll_id = Column(
        String(72),
        ForeignKey("projection_provider_polls.poll_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_observation_id = Column(
        String(72), nullable=False, server_default=""
    )
    source_ordinal = Column(Integer, nullable=False, server_default="0")
    ordinal = Column(Integer, nullable=False)
    provider = Column(String(64), nullable=False)
    provider_market_id = Column(String(255), nullable=True)
    athlete_provider_id = Column(String(255), nullable=True)
    event_provider_id = Column(String(255), nullable=True)
    statistic_provider_id = Column(String(255), nullable=True)
    statistic_provider_label = Column(String(255), nullable=True)
    market_reference = Column(String(72), nullable=False)
    canonical_game_id = Column(String(32), nullable=True)
    canonical_player_id = Column(Integer, nullable=True)
    canonical_player_name = Column(String(255), nullable=True)
    canonical_team_id = Column(Integer, nullable=True)
    canonical_statistic_id = Column(String(128), nullable=True)
    market_category = Column(String(32), nullable=True)
    market_status = Column(String(32), nullable=False)
    market_variant = Column(String(32), nullable=False)
    scoring_period = Column(String(32), nullable=False)
    #: The canonical comparable price of this market's selections.  The exact
    #: published number is kept as text so no dialect rounds a provider's own
    #: scale away, and it is null whenever each side states its own number --
    #: those stay on the selections in the archived source document.
    price_kind = Column(
        String(16), nullable=False, default="unpriced", server_default="unpriced"
    )
    #: Wide enough for every price the normalized numeric domain admits: a
    #: value may occupy base-ten places from 1E+128 down to 1E-128, so its
    #: exact decimal text is at most 259 characters including a sign and a
    #: decimal point.
    price_value = Column(String(260), nullable=True)
    price_scope = Column(
        String(16), nullable=False, default="selection", server_default="selection"
    )
    targetable = Column(Boolean, nullable=False, default=False, server_default="0")
    resolution_state = Column(
        String(32), nullable=False, default="resolved", server_default="resolved"
    )
    unresolved_identities = Column(
        String(64), nullable=False, default="", server_default=""
    )
    observed_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "generation_id",
            "ordinal",
            name="uq_projection_observations_generation_ordinal",
        ),
        Index(
            "ix_projection_observations_governed_identity",
            "canonical_game_id",
            "canonical_player_id",
            "canonical_statistic_id",
        ),
        Index("ix_projection_observations_snapshot_id", "snapshot_id"),
        Index(
            "ix_projection_observations_provider_athlete",
            "provider",
            "athlete_provider_id",
        ),
        Index(
            "ix_projection_observations_provider_event",
            "provider",
            "event_provider_id",
        ),
        Index(
            "ix_projection_observations_provider_statistic_id",
            "provider",
            "statistic_provider_id",
        ),
        Index(
            "ix_projection_observations_provider_statistic_label",
            "provider",
            "statistic_provider_label",
        ),
        Index(
            "ix_projection_observations_provider_statistic_label_lower",
            "provider",
            func.lower(statistic_provider_label),
        ),
    )


class ProjectionMaterializationGeneration(Base):
    """One atomic materialization decision for changed snapshot evidence."""

    __tablename__ = "projection_materialization_generations"

    generation_id = Column(String(72), primary_key=True)
    provider = Column(String(64), nullable=False)
    season = Column(String(7), nullable=False)
    query_key = Column(String(72), nullable=False)
    snapshot_id = Column(
        String(72),
        ForeignKey("projection_provider_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_poll_id = Column(
        String(72),
        ForeignKey("projection_provider_polls.poll_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    materialization_checksum = Column(String(64), nullable=False)
    outcome = Column(String(32), nullable=False)
    is_replay = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "materialization_checksum",
            "retrieved_at",
            name="uq_projection_materialization_generation_identity",
        ),
    )


class LatestPlayerProjection(Base):
    """Current eligible projection pointer for one distinct provider offering."""

    __tablename__ = "latest_player_projections"

    provider = Column(String(64), primary_key=True)
    season = Column(String(7), primary_key=True)
    query_key = Column(String(72), primary_key=True)
    canonical_game_id = Column(String(32), primary_key=True)
    canonical_player_id = Column(Integer, primary_key=True)
    market_reference = Column(String(72), primary_key=True)
    observation_id = Column(
        String(72),
        ForeignKey("projection_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation_id = Column(
        String(72),
        ForeignKey(
            "projection_materialization_generations.generation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    canonical_team_id = Column(Integer, nullable=False)
    canonical_player_name = Column(String(255), nullable=False)
    canonical_statistic_id = Column(String(128), nullable=False)
    market_category = Column(String(32), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_latest_player_projections_game_observed",
            "season",
            "canonical_game_id",
            "observed_at",
        ),
    )


class ClosingProjectionSet(Base):
    """One immutable provider/game fence for post-start projection reads."""

    __tablename__ = "projection_closing_sets"

    closing_set_id = Column(String(72), primary_key=True)
    provider = Column(String(64), nullable=False)
    season = Column(String(7), nullable=False)
    query_key = Column(String(72), nullable=False)
    canonical_game_id = Column(String(32), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "season",
            "query_key",
            "canonical_game_id",
            name="uq_projection_closing_set_scope_game",
        ),
        Index(
            "ix_projection_closing_sets_season_game",
            "season",
            "canonical_game_id",
        ),
    )


class ClosingProjectionMembership(Base):
    """Immutable pointer from a closing set to one archived observation."""

    __tablename__ = "projection_closing_memberships"

    closing_set_id = Column(
        String(72),
        ForeignKey("projection_closing_sets.closing_set_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    market_reference = Column(String(72), primary_key=True)
    observation_id = Column(
        String(72),
        ForeignKey("projection_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation_id = Column(
        String(72),
        ForeignKey(
            "projection_materialization_generations.generation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_projection_closing_memberships_observation",
            "observation_id",
        ),
    )


__all__ = [
    "ClosingProjectionMembership",
    "ClosingProjectionSet",
    "LatestPlayerProjection",
    "ProjectionArchiveScopeLock",
    "ProjectionMaterializationGeneration",
    "ProjectionObservation",
    "ProjectionProviderSnapshot",
    "ProviderPoll",
]
