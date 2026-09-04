"""Persistence models for the Railway collection control plane.

The control plane is deliberately separate from the legacy refresh queue.  A
collector is allowed to write only immutable observations; publications are
advanced by the server under a database fence.
"""

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, Index, text

from . import Base


class ActiveSeason(Base):
    __tablename__ = "active_seasons"

    season = Column(String(7), primary_key=True)
    phase = Column(String(32), nullable=False, default="Regular Season")
    status = Column(String(16), nullable=False, default="active")
    cutoff = Column(DateTime(timezone=True), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    activated_by = Column(String(128), nullable=False)

    __table_args__ = (
        CheckConstraint("phase = 'Regular Season'", name="ck_active_season_phase"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_active_season_status"),
    )


class BootstrapRequest(Base):
    __tablename__ = "collection_bootstrap_requests"

    request_id = Column(String(36), primary_key=True)
    season = Column(String(7), nullable=False)
    catalog_type = Column(String(16), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    catalog_version = Column(String(128), nullable=True)
    failure_reason = Column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint("catalog_type IN ('event', 'athlete')", name="ck_bootstrap_catalog_type"),
        CheckConstraint("status IN ('pending', 'succeeded', 'failed', 'expired')", name="ck_bootstrap_status"),
        Index("ix_bootstrap_requests_season_cutoff", "season", "cutoff"),
    )


class CatalogPublication(Base):
    __tablename__ = "collection_catalog_publications"

    publication_id = Column(String(36), primary_key=True)
    season = Column(String(7), nullable=False)
    catalog_type = Column(String(16), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    version = Column(String(128), nullable=False)
    checksum = Column(String(64), nullable=False)
    payload = Column(Text, nullable=False)
    complete = Column(Boolean, nullable=False, default=True)
    published_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("catalog_type IN ('event', 'athlete')", name="ck_catalog_publication_type"),
        Index("ix_catalog_publications_lookup", "season", "catalog_type", "cutoff"),
    )


class CollectionManifest(Base):
    __tablename__ = "collection_manifests"

    manifest_id = Column(String(36), primary_key=True)
    season = Column(String(7), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    collect_before = Column(DateTime(timezone=True), nullable=False)
    accepted_versions = Column(Text, nullable=False)
    scopes = Column(Text, nullable=False)
    checksum = Column(String(64), nullable=False, unique=True)
    event_catalog_publication_id = Column(
        String(36),
        ForeignKey(
            "collection_catalog_publications.publication_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    event_catalog_checksum = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by = Column(String(36), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'superseded', 'expired')", name="ck_manifest_status"),
        Index("ix_manifests_season_cutoff", "season", "cutoff"),
    )


class CollectorIdentity(Base):
    __tablename__ = "collector_identities"

    identity_id = Column(String(64), primary_key=True)
    label = Column(String(128), nullable=False)
    environment = Column(String(32), nullable=False)
    audience = Column(String(128), nullable=False)
    secret_hash = Column(String(128), nullable=False)
    previous_secret_hash = Column(String(128), nullable=True)
    previous_secret_expires_at = Column(DateTime(timezone=True), nullable=True)
    scopes = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    release_version = Column(String(64), nullable=True)
    release_checksum = Column(String(64), nullable=True)
    # Authorization is bound to the machine owner and the provider/surface
    # registry, not inferred from the generic operation scopes in ``scopes``.
    owner = Column(String(64), nullable=False, default="residential_collector")
    providers = Column(Text, nullable=False, default="[]")
    surfaces = Column(Text, nullable=False, default="[]")

    __table_args__ = (Index("ix_collector_identity_environment", "environment"),)


class CollectorStatusTransition(Base):
    __tablename__ = "collector_status_transitions"

    transition_id = Column(String(36), primary_key=True)
    collector_id = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False)
    reason = Column(String(80), nullable=False)
    release_version = Column(String(64), nullable=False)
    release_checksum = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "state IN ('running','no_work','complete','retry','non_retryable','busy')",
            name="ck_collector_status_transition_state",
        ),
        Index("ix_collector_status_transition_machine", "collector_id", "created_at"),
    )


class CollectionObservation(Base):
    __tablename__ = "collection_observations"

    observation_id = Column(String(36), primary_key=True)
    client_observation_id = Column(String(128), nullable=False)
    collector_id = Column(String(64), nullable=False)
    # The manifest is part of observation provenance.  It is nullable only for
    # pre-control-plane rows upgraded in place; new ingestion always supplies
    # it and publication completeness never mixes manifests.
    manifest_id = Column(String(36), nullable=True)
    environment = Column(String(32), nullable=False)
    provider = Column(String(64), nullable=False)
    observation_type = Column(String(64), nullable=False)
    scope = Column(Text, nullable=False)
    season = Column(String(7), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    schema_version = Column(Integer, nullable=False)
    checksum = Column(String(64), nullable=False)
    payload = Column(Text, nullable=False)
    payload_bytes = Column(Integer, nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("uq_observation_collector_client_id", "collector_id", "client_observation_id", unique=True),
        Index("ix_observations_scope", "season", "observation_type", "cutoff"),
    )


class PublicationStream(Base):
    __tablename__ = "publication_streams"

    stream_key = Column(String(96), primary_key=True)
    provider = Column(String(64), nullable=False)
    owner = Column(String(64), nullable=False)
    required_observations = Column(Text, nullable=False)
    publication_strategy = Column(String(64), nullable=False)
    supported_windows = Column(Text, nullable=False)
    schema_versions = Column(Text, nullable=False, default="[1, 2]")
    completeness_rule = Column(String(128), nullable=False, default="base_complete")
    freshness_rule = Column(String(128), nullable=False, default="cutoff_current")
    enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class PublicationVersion(Base):
    __tablename__ = "publication_versions"

    publication_id = Column(String(36), primary_key=True)
    stream_key = Column(String(96), nullable=False)
    season = Column(String(7), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="candidate")
    checksum = Column(String(64), nullable=False)
    payload = Column(Text, nullable=False)
    manifest_id = Column(
        String(36),
        ForeignKey("collection_manifests.manifest_id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_catalog_publication_id = Column(
        String(36),
        ForeignKey(
            "collection_catalog_publications.publication_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    event_catalog_checksum = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(255), nullable=True)
    fence = Column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('candidate', 'active', 'rollback', 'superseded')", name="ck_publication_version_status"),
        Index("ix_publication_versions_stream_cutoff", "stream_key", "season", "cutoff"),
    )


class PublicationObservation(Base):
    """Immutable normalized provenance for one publication version.

    A publication names the exact accepted observations that supplied its
    completeness evidence.  It is intentionally separate from the rendered
    publication payload so retention never has to search arbitrary JSON for
    identifiers.
    """

    __tablename__ = "publication_observations"

    publication_id = Column(
        String(36),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        primary_key=True,
    )
    observation_id = Column(
        String(36),
        ForeignKey("collection_observations.observation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role = Column(String(64), nullable=False, default="source")
    slice_key = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_publication_observations_observation", "observation_id"),
    )


class PublicationPointer(Base):
    __tablename__ = "publication_pointers"

    stream_key = Column(String(96), primary_key=True)
    active_publication_id = Column(String(36), nullable=True)
    previous_publication_id = Column(String(36), nullable=True)
    fence = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class PublicationActivation(Base):
    """Immutable evidence for one explicit publication-stream activation.

    The stream's ``enabled`` flag is the executable gate.  This append-only
    record supplies the operator-facing reason and exact candidate that made
    the gate true, so a later read never has to infer activation from an
    arbitrary rendered payload.
    """

    __tablename__ = "publication_activations"

    activation_id = Column(String(36), primary_key=True)
    stream_key = Column(String(96), nullable=False)
    publication_id = Column(
        String(36),
        ForeignKey("publication_versions.publication_id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor = Column(String(128), nullable=False)
    reason = Column(String(255), nullable=False)
    fence = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_publication_activations_stream_created", "stream_key", "created_at"),
        Index(
            "uq_publication_activations_stream_publication",
            "stream_key",
            "publication_id",
            unique=True,
        ),
    )


class CompositionJob(Base):
    __tablename__ = "composition_jobs"

    job_id = Column(String(36), primary_key=True)
    stream_key = Column(String(96), nullable=False)
    manifest_id = Column(String(36), nullable=True)
    season = Column(String(7), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(String(64), nullable=True)
    # Correction propagation keeps the exact invalidation request beside the
    # idempotent job.  The JSON columns are intentionally bounded metadata,
    # not a second copy of the ledger payload.
    # Legacy singular field remains readable; new writes use the plural JSON
    # text field so coalesced invalidations are not truncated.
    trigger_game_id = Column(String(64), nullable=True)
    trigger_game_ids = Column(Text, nullable=False, default="[]", server_default="[]")
    affected_team_ids = Column(Text, nullable=False, default="[]", server_default="[]")
    source_observation_ids = Column(Text, nullable=False, default="[]", server_default="[]")
    recomposition_reason = Column(String(128), nullable=True)
    ledger_checksum = Column(String(64), nullable=True)
    game_set_checksum = Column(String(64), nullable=True)
    ledger_evidence = Column(Text, nullable=False, default="{}", server_default="{}")
    # ``generation`` versions the complete queued lineage.  A worker records
    # the generation it claimed; completion is accepted only when that exact
    # generation is still running, so a correction accepted mid-composition
    # remains queued for the next worker pass.
    generation = Column(Integer, nullable=False, default=1, server_default="1")
    claimed_generation = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_composition_job_status"),
        Index("uq_composition_job_active", "stream_key", "season", "cutoff", unique=True),
    )


class CollectorTokenReplay(Base):
    __tablename__ = "collector_token_replays"

    token_id = Column(String(64), primary_key=True)
    collector_id = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class CollectorLease(Base):
    """Database-backed per-identity ingestion lease.

    PostgreSQL workers serialize acquisition with ``FOR UPDATE``.  An
    expired owner can be recovered by the next worker, so the lease is not
    tied to a process surviving or to a process-local semaphore.
    """

    __tablename__ = "collector_ingestion_leases"

    collector_id = Column(String(64), primary_key=True)
    lease_owner = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    fence = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class CollectionCycle(Base):
    """One immutable cutoff collection attempt."""

    __tablename__ = "collection_cycles"

    cycle_id = Column(String(36), primary_key=True)
    season = Column(String(7), nullable=False)
    manifest_id = Column(String(36), nullable=False, unique=True)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, default="collecting")
    completed_game_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    attention_reason = Column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('collecting', 'complete', 'no_game', 'attention', 'failed', 'superseded')", name="ck_collection_cycle_status"),
    )


class AuditEvent(Base):
    __tablename__ = "collection_audit_events"

    event_id = Column(String(36), primary_key=True)
    actor = Column(String(128), nullable=False)
    action = Column(String(64), nullable=False)
    resource = Column(String(128), nullable=False)
    reason = Column(String(255), nullable=False)
    details = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False)


class ReconciliationItem(Base):
    __tablename__ = "collection_reconciliation_items"

    item_id = Column(String(36), primary_key=True)
    season = Column(String(7), nullable=False)
    kind = Column(String(64), nullable=False)
    reason = Column(String(64), nullable=False)
    details = Column(Text, nullable=False, default="{}")
    # Stable bounded dedupe identity for repeated rejected evidence.  It is
    # nullable for rows created before migration 022.
    dedupe_key = Column(String(128), nullable=True, unique=True)
    status = Column(String(16), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint("status IN ('open', 'resolved')", name="ck_reconciliation_status"),)


class CollectionAlert(Base):
    __tablename__ = "collection_alerts"

    alert_id = Column(String(36), primary_key=True)
    cycle_id = Column(String(36), nullable=True)
    severity = Column(String(16), nullable=False)
    code = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint("severity IN ('warning', 'critical')", name="ck_collection_alert_severity"),)


class CollectorUsage(Base):
    __tablename__ = "collector_usage"

    collector_id = Column(String(64), primary_key=True)
    window_started_at = Column(DateTime(timezone=True), nullable=False)
    poll_count = Column(Integer, nullable=False, default=0)
    envelope_count = Column(Integer, nullable=False, default=0)
    byte_count = Column(Integer, nullable=False, default=0)


class ValidationSummary(Base):
    __tablename__ = "collection_validation_summaries"

    summary_id = Column(String(36), primary_key=True)
    cycle_id = Column(String(36), nullable=False)
    status = Column(String(16), nullable=False)
    counts = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False)


class GovernedNotApplicable(Base):
    __tablename__ = "governed_not_applicable"

    cycle_id = Column(String(36), primary_key=True)
    stream_key = Column(String(96), primary_key=True)
    actor = Column(String(128), nullable=False)
    reason = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class OperatorJob(Base):
    __tablename__ = "collection_operator_jobs"

    job_id = Column(String(36), primary_key=True)
    actor = Column(String(128), nullable=False)
    action = Column(String(64), nullable=False)
    resource = Column(String(128), nullable=False)
    reason = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    created_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)

    __table_args__ = (CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_operator_job_status"),)


class PublicationRebuild(Base):
    """One durable, restart-safe rebuild of a publication family's format.

    A rebuild regenerates every window of one family from ledger facts that
    did not change, because the *rendered format* changed.  It is deliberately
    not a composition job (those are created by new or corrected observations),
    not failed-data repair, and not initial parity activation: each of those
    already means something else, and overloading one of them would make
    operational history describe an event that never happened.

    The row is the operation.  It carries the expected active pair and their
    fences so a concurrent correction cannot be silently overwritten, the
    shared immutable authority it must reuse, a worker lease and a generation
    for restart-safe claiming, the staged and promoted identities and
    checksums that are its audit evidence, and one bounded failure code.
    """

    __tablename__ = "publication_rebuilds"

    rebuild_id = Column(String(36), primary_key=True)
    family = Column(String(64), nullable=False)
    #: The format the *deployed code* owns, recorded with a fingerprint of its
    #: exact taxonomy and invariants.  A request never names a target.
    target_format = Column(String(64), nullable=False)
    target_fingerprint = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    reason = Column(String(255), nullable=False)
    #: A digest of the request's full identity.  Two starts that agree on it
    #: are the same operation; one that differs while another is active is a
    #: conflicting request rather than a retry.
    request_checksum = Column(String(64), nullable=False)
    expected_season_publication_id = Column(String(36), nullable=False)
    expected_season_fence = Column(Integer, nullable=False)
    expected_l15_publication_id = Column(String(36), nullable=False)
    expected_l15_fence = Column(Integer, nullable=False)
    #: The active pair's own authority, reused rather than re-supplied.
    season = Column(String(7), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    manifest_id = Column(String(36), nullable=True)
    event_catalog_publication_id = Column(String(36), nullable=True)
    event_catalog_checksum = Column(String(64), nullable=True)
    #: The accepted ledger provenance the candidates must still rest on.  A
    #: correction that changes it makes the rebuild terminate as stale.
    source_checksum = Column(String(64), nullable=True)
    state = Column(String(16), nullable=False, default="queued")
    attempts = Column(Integer, nullable=False, default=0)
    generation = Column(Integer, nullable=False, default=1)
    claimed_generation = Column(Integer, nullable=True)
    lease_owner = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    staged_season_publication_id = Column(String(36), nullable=True)
    staged_season_checksum = Column(String(64), nullable=True)
    staged_l15_publication_id = Column(String(36), nullable=True)
    staged_l15_checksum = Column(String(64), nullable=True)
    promoted_season_publication_id = Column(String(36), nullable=True)
    promoted_season_checksum = Column(String(64), nullable=True)
    promoted_l15_publication_id = Column(String(36), nullable=True)
    promoted_l15_checksum = Column(String(64), nullable=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'composing', 'validating', 'promoting',"
            " 'succeeded', 'failed')",
            name="ck_publication_rebuild_state",
        ),
        # One family may have at most one rebuild in flight.  Enforced in the
        # database on both dialects so two concurrent operators cannot both
        # observe an empty table and both insert.
        Index(
            "uq_publication_rebuild_active_family",
            "family",
            unique=True,
            sqlite_where=text(
                "state IN ('queued', 'composing', 'validating', 'promoting')"
            ),
            postgresql_where=text(
                "state IN ('queued', 'composing', 'validating', 'promoting')"
            ),
        ),
        Index("ix_publication_rebuilds_family_created", "family", "created_at"),
    )


class CredentialDelivery(Base):
    __tablename__ = "collector_credential_deliveries"

    delivery_id = Column(String(36), primary_key=True)
    identity_id = Column(String(64), nullable=False)
    encrypted_secret = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=True)


__all__ = [
    "ActiveSeason", "BootstrapRequest", "CatalogPublication", "CollectionManifest",
    "CollectorIdentity", "CollectorStatusTransition", "CollectionObservation", "PublicationStream",
    "PublicationVersion", "PublicationObservation", "PublicationPointer", "PublicationActivation", "CompositionJob", "CollectorTokenReplay",
    "CollectorLease",
    "CollectionCycle", "AuditEvent", "ReconciliationItem", "CollectionAlert",
    "CollectorUsage", "ValidationSummary",
    "GovernedNotApplicable", "OperatorJob", "PublicationRebuild", "CredentialDelivery",
]
