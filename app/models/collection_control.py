"""Persistence models for the Railway collection control plane.

The control plane is deliberately separate from the legacy refresh queue.  A
collector is allowed to write only immutable observations; publications are
advanced by the server under a database fence.
"""

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Text, Index

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

    __table_args__ = (Index("ix_collector_identity_environment", "environment"),)


class CollectionObservation(Base):
    __tablename__ = "collection_observations"

    observation_id = Column(String(36), primary_key=True)
    client_observation_id = Column(String(128), nullable=False)
    collector_id = Column(String(64), nullable=False)
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
    created_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(255), nullable=True)
    fence = Column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('candidate', 'active', 'rollback', 'superseded')", name="ck_publication_version_status"),
        Index("ix_publication_versions_stream_cutoff", "stream_key", "season", "cutoff"),
    )


class PublicationPointer(Base):
    __tablename__ = "publication_pointers"

    stream_key = Column(String(96), primary_key=True)
    active_publication_id = Column(String(36), nullable=True)
    previous_publication_id = Column(String(36), nullable=True)
    fence = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class CompositionJob(Base):
    __tablename__ = "composition_jobs"

    job_id = Column(String(36), primary_key=True)
    stream_key = Column(String(96), nullable=False)
    season = Column(String(7), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_composition_job_status"),
        Index("uq_composition_job_active", "stream_key", "season", "cutoff", unique=True),
    )


class CollectorTokenReplay(Base):
    __tablename__ = "collector_token_replays"

    token_id = Column(String(64), primary_key=True)
    collector_id = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


__all__ = [
    "ActiveSeason", "BootstrapRequest", "CatalogPublication", "CollectionManifest",
    "CollectorIdentity", "CollectionObservation", "PublicationStream",
    "PublicationVersion", "PublicationPointer", "CompositionJob", "CollectorTokenReplay",
]
