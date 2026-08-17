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
    ordinal = Column(Integer, nullable=False)
    provider = Column(String(64), nullable=False)
    provider_market_id = Column(String(255), nullable=True)
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
    targetable = Column(Boolean, nullable=False, default=False, server_default="0")
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
        unique=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    materialization_checksum = Column(String(64), nullable=False)
    outcome = Column(String(32), nullable=False)

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


__all__ = [
    "LatestPlayerProjection",
    "ProjectionArchiveScopeLock",
    "ProjectionMaterializationGeneration",
    "ProjectionObservation",
    "ProjectionProviderSnapshot",
    "ProviderPoll",
]
