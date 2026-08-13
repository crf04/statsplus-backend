"""Repeatable migrations for the application's own database schema.

The bundled NBA data is a public read-only fixture and is not managed by
these migrations.  Migrations only create or upgrade tables owned by the
application, starting with the ``users`` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql import func


MIGRATION_TABLE_NAME: Final[str] = "schema_migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    """A single ordered schema migration."""

    version: int
    name: str
    upgrade: Callable[[Connection], None]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """The result of applying zero or more pending migrations."""

    applied: tuple[str, ...]
    current_version: int


_migration_metadata = MetaData()
_schema_migrations = Table(
    MIGRATION_TABLE_NAME,
    _migration_metadata,
    Column("version", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def _create_users_table(connection: Connection) -> None:
    """Create the application-owned ``users`` table."""
    from app.models.user import User

    User.__table__.create(connection, checkfirst=True)


def _create_data_refresh_jobs_table(connection: Connection) -> None:
    """Create the durable ``data_refresh_jobs`` table."""
    from app.models.job import DataRefreshJob

    DataRefreshJob.__table__.create(connection, checkfirst=True)


def _upgrade_data_refresh_jobs_queue(connection: Connection) -> None:
    """Add lease metadata to databases created by migration 002.

    Migration 002 created the first version of the table.  ``ALTER TABLE`` is
    used here instead of recreating it so existing queued/running rows and the
    partial active-operation index survive an upgrade.  Fresh databases see
    all columns in the model-created table and therefore simply skip the
    additions.
    """
    table_name = "data_refresh_jobs"
    existing = {
        column["name"]
        for column in inspect(connection).get_columns(table_name)
    }
    additions = {
        "request_id": "VARCHAR(128)",
        "lease_owner": "VARCHAR(128)",
        "lease_expires_at": "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME",
        "heartbeat_at": "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    }
    preparer = connection.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    for name, type_sql in additions.items():
        if name in existing:
            continue
        connection.execute(
            text(
                f"ALTER TABLE {quoted_table} ADD COLUMN "
                f"{preparer.quote(name)} {type_sql}"
            )
        )

    # Old application databases may have had the table but not the index
    # (e.g. an interrupted 002 migration).  Keep the same database-enforced
    # duplicate-operation guarantee on every supported dialect.
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_data_refresh_jobs_active_operation "
            f"ON {quoted_table} (operation) "
            "WHERE status IN ('queued', 'running')"
        )
    )


def _create_event_catalog_tables(connection: Connection) -> None:
    """Create the writable canonical event and refresh-state tables.

    Version 005 is intentionally reserved for the event catalog.  Issue #25
    owns migration 004; the two migrations are expected to be adjacent after
    merge even though this branch is runnable on its own with a gap.
    """
    from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh

    EventCatalogEntry.__table__.create(connection, checkfirst=True)
    EventCatalogRefresh.__table__.create(connection, checkfirst=True)


def _create_athlete_catalog_tables(connection: Connection) -> None:
    """Create the application-owned canonical athlete catalog tables."""
    from app.models.athlete_catalog import AthleteCatalog, AthleteCatalogFreshness

    AthleteCatalog.__table__.create(connection, checkfirst=True)
    AthleteCatalogFreshness.__table__.create(connection, checkfirst=True)


def _create_athlete_mapping_tables(connection: Connection) -> None:
    """Create mapping state, decisions, decision candidates, and rejections."""
    from app.models.athlete_mapping import (
        AthleteMappingDecision,
        AthleteMappingDecisionCandidate,
        AthleteMappingLock,
        AthleteMappingRejection,
        ProviderAthleteMapping,
    )

    ProviderAthleteMapping.__table__.create(connection, checkfirst=True)
    AthleteMappingDecision.__table__.create(connection, checkfirst=True)
    # The candidate table references the decision it belongs to, so it is
    # created after the decision table.
    AthleteMappingDecisionCandidate.__table__.create(connection, checkfirst=True)
    AthleteMappingRejection.__table__.create(connection, checkfirst=True)
    AthleteMappingLock.__table__.create(connection, checkfirst=True)


def _create_athlete_mapping_contradictions_table(connection: Connection) -> None:
    """Create the typed contradictory evidence retained beside a decision.

    Migration 006 created the decision table this one references, so the child
    table is added on its own rather than by recreating it.  Existing decisions
    keep the representative evidence they were recorded on; only contradictions
    observed after the upgrade have rows here.
    """
    from app.models.athlete_mapping import AthleteMappingDecisionContradiction

    AthleteMappingDecisionContradiction.__table__.create(connection, checkfirst=True)


def _create_event_mapping_tables(connection: Connection) -> None:
    """Create event mapping state, decisions, candidates, and rejections.

    Migration 005 created the canonical event catalog these rows resolve
    against; the mapping state itself is owned here rather than there, exactly
    as migration 006 owns provider athlete mapping state.
    """
    from app.models.event_mapping import (
        EventMappingDecision,
        EventMappingDecisionCandidate,
        EventMappingDecisionContradiction,
        EventMappingLock,
        EventMappingRejection,
        ProviderEventMapping,
    )

    ProviderEventMapping.__table__.create(connection, checkfirst=True)
    EventMappingDecision.__table__.create(connection, checkfirst=True)
    # The candidate and contradiction tables reference the decision they belong
    # to, so they are created after the decision table.
    EventMappingDecisionCandidate.__table__.create(connection, checkfirst=True)
    EventMappingDecisionContradiction.__table__.create(connection, checkfirst=True)
    EventMappingRejection.__table__.create(connection, checkfirst=True)
    EventMappingLock.__table__.create(connection, checkfirst=True)


def _create_stats_freshness_table(connection: Connection) -> None:
    """Create governed completion records for stats-surface publications."""
    from app.models.stats_freshness import StatsRefresh

    StatsRefresh.__table__.create(connection, checkfirst=True)


def _create_player_pool_snapshot_table(connection: Connection) -> None:
    """Create persisted governed Player Pool observations."""
    from app.models.player_pool_snapshot import PlayerPoolSnapshot

    PlayerPoolSnapshot.__table__.create(connection, checkfirst=True)


def _create_player_game_log_tables(connection: Connection) -> None:
    """Create canonical player-game facts and season publication metadata."""
    from app.models.player_game_log import PlayerGameLog, PlayerGameLogRefresh

    PlayerGameLog.__table__.create(connection, checkfirst=True)
    PlayerGameLogRefresh.__table__.create(connection, checkfirst=True)


def _create_team_matchup_fact_tables(connection: Connection) -> None:
    """Create raw team-window facts and per-surface observations.

    Migration 012 is reserved for issue #57 and follows the player-log
    prerequisite's migration 011 (#56) in integrated history.
    """
    from app.models.team_matchup import (
        TeamMatchupFactRow,
        TeamMatchupSurfaceObservationRow,
    )

    TeamMatchupFactRow.__table__.create(connection, checkfirst=True)
    TeamMatchupSurfaceObservationRow.__table__.create(connection, checkfirst=True)


def _create_player_diet_fact_tables(connection: Connection) -> None:
    """Create Season player Diet facts and per-Base observations."""
    from app.models.player_diet import (
        PlayerDietFactRow,
        PlayerDietSurfaceObservationRow,
    )

    PlayerDietFactRow.__table__.create(connection, checkfirst=True)
    PlayerDietSurfaceObservationRow.__table__.create(connection, checkfirst=True)


def _create_injury_snapshot_table(connection: Connection) -> None:
    """Create raw and normalized matchup injury snapshots."""
    from app.models.injury_snapshot import InjurySnapshot

    InjurySnapshot.__table__.create(connection, checkfirst=True)


def _share_injury_source_snapshots(connection: Connection) -> None:
    """Add append-only league evidence and references from per-game snapshots."""
    from app.models.injury_snapshot import InjurySourceSnapshot

    InjurySourceSnapshot.__table__.create(connection, checkfirst=True)
    existing = {
        column["name"]
        for column in inspect(connection).get_columns("injury_snapshots")
    }
    preparer = connection.dialect.identifier_preparer
    table = preparer.quote("injury_snapshots")
    if "source_snapshot_id" not in existing:
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN source_snapshot_id INTEGER")
        )
    if "unresolved_team_entry_count" not in existing:
        connection.execute(
            text(
                f"ALTER TABLE {table} ADD COLUMN unresolved_team_entry_count "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )


def _upgrade_player_game_log_primitives(connection: Connection) -> None:
    """Expand durable game logs to the full legacy primitive set (#66).

    Migration 011 created the player-game facts with the subset the Matchups
    surface consumed.  The staged PBP migration adds every primitive the legacy
    endpoint needs, plus per-game synchronization evidence and an explicit
    complete/in-progress publication status on the season sidecar.  Existing
    rows are retained with zero counts for the new fields; a fresh database
    already carries the full model schema and skips the additions.
    """
    from app.models.player_game_log import PlayerGameLogSync

    PlayerGameLogSync.__table__.create(connection, checkfirst=True)
    preparer = connection.dialect.identifier_preparer
    logs_table = preparer.quote("player_game_logs")
    existing_logs = {
        column["name"]
        for column in inspect(connection).get_columns("player_game_logs")
    }
    additions = {
        "free_throws_made": "INTEGER NOT NULL DEFAULT 0",
        "free_throws_attempted": "INTEGER NOT NULL DEFAULT 0",
        "offensive_rebounds": "INTEGER NOT NULL DEFAULT 0",
        "defensive_rebounds": "INTEGER NOT NULL DEFAULT 0",
        "personal_fouls": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, type_sql in additions.items():
        if name in existing_logs:
            continue
        connection.execute(
            text(
                f"ALTER TABLE {logs_table} ADD COLUMN "
                f"{preparer.quote(name)} {type_sql}"
            )
        )
    refreshes_table = preparer.quote("player_game_log_refreshes")
    existing_refreshes = {
        column["name"]
        for column in inspect(connection).get_columns("player_game_log_refreshes")
    }
    if "publication_status" not in existing_refreshes:
        connection.execute(
            text(
                f"ALTER TABLE {refreshes_table} ADD COLUMN "
                "publication_status VARCHAR(16) NOT NULL DEFAULT 'in_progress'"
            )
        )


def _create_collection_control_plane_tables(connection: Connection) -> None:
    """Create the additive Railway collection-control schema (#84)."""
    from app.models.collection_control import (
        ActiveSeason,
        BootstrapRequest,
        CatalogPublication,
        CollectionManifest,
        CollectorIdentity,
        CollectionObservation,
        PublicationStream,
        PublicationVersion,
        PublicationObservation,
        PublicationPointer,
        CompositionJob,
        CollectorTokenReplay,
        CollectorLease,
        CollectionCycle,
        AuditEvent,
        ReconciliationItem,
        CollectionAlert,
        CollectorUsage,
        ValidationSummary,
    )

    # Creation order is intentionally explicit for PostgreSQL deployments and
    # keeps this migration usable on SQLite temporary databases.
    for model in (
        ActiveSeason,
        BootstrapRequest,
        CatalogPublication,
        CollectionManifest,
        CollectorIdentity,
        CollectionObservation,
        PublicationStream,
        PublicationVersion,
        PublicationObservation,
        PublicationPointer,
        CompositionJob,
        CollectorTokenReplay,
        CollectorLease,
        CollectionCycle,
        AuditEvent,
        ReconciliationItem,
        CollectionAlert,
        CollectorUsage,
        ValidationSummary,
    ):
        model.__table__.create(connection, checkfirst=True)


def _upgrade_collection_operations(connection: Connection) -> None:
    """Add cycle, audit, alert, usage, and reconciliation evidence (#84)."""
    from app.models.collection_control import (
        CollectionCycle,
        AuditEvent,
        ReconciliationItem,
        CollectionAlert,
        CollectorUsage,
        ValidationSummary,
    )
    for model in (
        CollectionCycle,
        AuditEvent,
        ReconciliationItem,
        CollectionAlert,
        CollectorUsage,
        ValidationSummary,
    ):
        model.__table__.create(connection, checkfirst=True)


def _upgrade_surface_registry(connection: Connection) -> None:
    """Add explicit schema/completeness/freshness registry metadata."""
    table = "publication_streams"
    existing = {column["name"] for column in inspect(connection).get_columns(table)}
    preparer = connection.dialect.identifier_preparer
    quoted = preparer.quote(table)
    additions = {
        "schema_versions": "TEXT NOT NULL DEFAULT '[1, 2]'",
        "completeness_rule": "VARCHAR(128) NOT NULL DEFAULT 'base_complete'",
        "freshness_rule": "VARCHAR(128) NOT NULL DEFAULT 'cutoff_current'",
    }
    for name, type_sql in additions.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE {quoted} ADD COLUMN {preparer.quote(name)} {type_sql}"))


def _upgrade_operator_control(connection: Connection) -> None:
    from app.models.collection_control import GovernedNotApplicable, OperatorJob, CredentialDelivery
    for model in (GovernedNotApplicable, OperatorJob, CredentialDelivery):
        model.__table__.create(connection, checkfirst=True)
    # Preserve provenance for databases that applied the original control
    # plane before manifest-bound completeness was introduced.  Fresh
    # databases receive these columns from the model-driven create above.
    preparer = connection.dialect.identifier_preparer
    for table, column in (("collection_observations", "manifest_id"), ("composition_jobs", "manifest_id")):
        existing = {item["name"] for item in inspect(connection).get_columns(table)}
        if column not in existing:
            connection.execute(text(
                f"ALTER TABLE {preparer.quote(table)} ADD COLUMN "
                f"{preparer.quote(column)} VARCHAR(36)"
            ))


def _upgrade_collector_surface_authorization(connection: Connection) -> None:
    """Bind collector identities to owner/provider/surface and add leases."""

    from app.models.collection_control import CollectorLease

    CollectorLease.__table__.create(connection, checkfirst=True)
    table = "collector_identities"
    existing = {column["name"] for column in inspect(connection).get_columns(table)}
    preparer = connection.dialect.identifier_preparer
    additions = {
        "owner": "VARCHAR(64) NOT NULL DEFAULT 'residential_collector'",
        "providers": "TEXT NOT NULL DEFAULT '[]'",
        "surfaces": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, type_sql in additions.items():
        if name not in existing:
            connection.execute(text(
                f"ALTER TABLE {preparer.quote(table)} ADD COLUMN "
                f"{preparer.quote(name)} {type_sql}"
            ))


def _upgrade_provenance_and_reconciliation(connection: Connection) -> None:
    """Normalize publication provenance and dedupe unresolved identities."""

    from app.models.collection_control import PublicationObservation

    PublicationObservation.__table__.create(connection, checkfirst=True)
    table = "collection_reconciliation_items"
    existing = {column["name"] for column in inspect(connection).get_columns(table)}
    preparer = connection.dialect.identifier_preparer
    if "dedupe_key" not in existing:
        connection.execute(text(
            f"ALTER TABLE {preparer.quote(table)} ADD COLUMN "
            f"{preparer.quote('dedupe_key')} VARCHAR(128)"
        ))
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        f"{preparer.quote('uq_reconciliation_dedupe_key')} ON "
        f"{preparer.quote(table)} ({preparer.quote('dedupe_key')}) "
        "WHERE dedupe_key IS NOT NULL"
    ))


def _create_canonical_game_ledger_tables(connection: Connection) -> None:
    """Create the inactive Canonical Game Ledger publication family (#86)."""

    from app.models.canonical_game_ledger import (
        CanonicalGameLedgerGame,
        CanonicalGameLedgerPlayerFact,
        CanonicalGameLedgerTeamFact,
        LedgerBackfillState,
        LedgerPublication,
    )

    # The game row is the parent for both complete fact sets.  Explicit order
    # keeps PostgreSQL foreign-key additions safe when the models gain them.
    for model in (
        CanonicalGameLedgerGame,
        CanonicalGameLedgerTeamFact,
        CanonicalGameLedgerPlayerFact,
        LedgerBackfillState,
        LedgerPublication,
    ):
        model.__table__.create(connection, checkfirst=True)


def _upgrade_collector_release_status(connection: Connection) -> None:
    """Persist bounded machine release evidence for operator diagnostics."""

    table = "collector_identities"
    existing = {column["name"] for column in inspect(connection).get_columns(table)}
    preparer = connection.dialect.identifier_preparer
    additions = {
        "release_version": "VARCHAR(64)",
        "release_checksum": "VARCHAR(64)",
    }
    for name, type_sql in additions.items():
        if name not in existing:
            connection.execute(text(
                f"ALTER TABLE {preparer.quote(table)} ADD COLUMN "
                f"{preparer.quote(name)} {type_sql}"
            ))


def _create_ledger_parity_artifacts(connection: Connection) -> None:
    """Persist required semantic parity adjudication evidence (#86)."""

    from app.models.canonical_game_ledger import LedgerParityArtifact

    LedgerParityArtifact.__table__.create(connection, checkfirst=True)


def _repair_publication_provenance_foreign_keys(connection: Connection) -> None:
    """Repair the transient #86 self-FK and add both provenance FKs."""

    from app.models.collection_control import PublicationObservation, PublicationVersion

    inspector = inspect(connection)
    parity_table = "canonical_game_ledger_parity_artifacts"
    parity_columns = {column["name"] for column in inspector.get_columns(parity_table)}
    additions = {
        "decision": "VARCHAR(16)",
        "adjudicated_by": "VARCHAR(128)",
        "adjudicated_at": "TIMESTAMP",
        "adjudication_reason": "VARCHAR(255)",
    }
    for name, type_sql in additions.items():
        if name not in parity_columns:
            connection.execute(text(f"ALTER TABLE {parity_table} ADD COLUMN {name} {type_sql}"))
    if connection.dialect.name == "sqlite":
        version_rows = connection.execute(text("SELECT * FROM publication_versions")).mappings().all()
        provenance_rows = connection.execute(text("SELECT * FROM publication_observations")).mappings().all()
        connection.execute(text("DROP TABLE publication_observations"))
        connection.execute(text("DROP TABLE publication_versions"))
        PublicationVersion.__table__.create(connection)
        PublicationObservation.__table__.create(connection)
        if version_rows:
            connection.execute(PublicationVersion.__table__.insert(), [dict(row) for row in version_rows])
        if provenance_rows:
            accepted = {
                row["observation_id"]
                for row in connection.execute(text("SELECT observation_id FROM collection_observations")).mappings()
            }
            versions = {row["publication_id"] for row in version_rows}
            valid = [
                dict(row) for row in provenance_rows
                if row["publication_id"] in versions and row["observation_id"] in accepted
            ]
            if valid:
                connection.execute(PublicationObservation.__table__.insert(), valid)
        return
    for foreign_key in inspector.get_foreign_keys("publication_versions"):
        if foreign_key.get("referred_table") == "publication_versions" and foreign_key.get("name"):
            connection.execute(text(
                f'ALTER TABLE publication_versions DROP CONSTRAINT "{foreign_key["name"]}"'
            ))
    existing = {
        (tuple(foreign_key.get("constrained_columns") or ()), foreign_key.get("referred_table"))
        for foreign_key in inspector.get_foreign_keys("publication_observations")
    }
    if (("publication_id",), "publication_versions") not in existing:
        connection.execute(text(
            "ALTER TABLE publication_observations ADD CONSTRAINT "
            "fk_publication_observations_publication FOREIGN KEY (publication_id) "
            "REFERENCES publication_versions(publication_id) ON DELETE CASCADE"
        ))
    if (("observation_id",), "collection_observations") not in existing:
        connection.execute(text(
            "ALTER TABLE publication_observations ADD CONSTRAINT "
            "fk_publication_observations_observation FOREIGN KEY (observation_id) "
            "REFERENCES collection_observations(observation_id) ON DELETE RESTRICT"
        ))


def _bind_ledger_parity_to_publications(connection: Connection) -> None:
    """Bind new parity evidence to the exact candidate payload it rehearsed."""

    from app.models.canonical_game_ledger import LedgerParityArtifact

    table = "canonical_game_ledger_parity_artifacts"
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns(table)}
    if connection.dialect.name == "sqlite":
        legacy_rows = connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
        connection.execute(text(f"DROP TABLE {table}"))
        LedgerParityArtifact.__table__.create(connection)
        # Evidence produced before candidate binding cannot authorize a
        # publication.  Deliberately do not copy it into the stricter table.
        del legacy_rows
        return
    preparer = connection.dialect.identifier_preparer
    if "publication_id" not in columns:
        connection.execute(text(
            f"ALTER TABLE {preparer.quote(table)} ADD COLUMN publication_id VARCHAR(36)"
        ))
    if "payload_checksum" not in columns:
        connection.execute(text(
            f"ALTER TABLE {preparer.quote(table)} ADD COLUMN payload_checksum VARCHAR(64)"
        ))
    # Old unbound evidence is intentionally retired because it cannot prove
    # the semantics of a current candidate.
    connection.execute(text(f"DELETE FROM {preparer.quote(table)} WHERE publication_id IS NULL"))
    connection.execute(text(
        f"ALTER TABLE {preparer.quote(table)} ALTER COLUMN publication_id SET NOT NULL"
    ))
    connection.execute(text(
        f"ALTER TABLE {preparer.quote(table)} ALTER COLUMN payload_checksum SET NOT NULL"
    ))
    foreign_keys = inspect(connection).get_foreign_keys(table)
    if not any(key.get("referred_table") == "publication_versions" for key in foreign_keys):
        connection.execute(text(
            f"ALTER TABLE {preparer.quote(table)} ADD CONSTRAINT "
            "fk_ledger_parity_publication FOREIGN KEY (publication_id) "
            "REFERENCES publication_versions(publication_id) ON DELETE CASCADE"
        ))


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "001_create_users", _create_users_table),
    Migration(2, "002_create_data_refresh_jobs", _create_data_refresh_jobs_table),
    Migration(3, "003_durable_data_refresh_queue", _upgrade_data_refresh_jobs_queue),
    Migration(4, "004_create_athlete_catalog", _create_athlete_catalog_tables),
    Migration(5, "005_create_event_catalog", _create_event_catalog_tables),
    Migration(6, "006_create_athlete_mappings", _create_athlete_mapping_tables),
    Migration(
        7,
        "007_create_athlete_mapping_contradictions",
        _create_athlete_mapping_contradictions_table,
    ),
    Migration(8, "008_create_event_mappings", _create_event_mapping_tables),
    Migration(9, "009_create_stats_freshness", _create_stats_freshness_table),
    Migration(10, "010_create_player_pool_snapshots", _create_player_pool_snapshot_table),
    Migration(11, "011_create_player_game_logs", _create_player_game_log_tables),
    Migration(12, "012_create_team_matchup_facts", _create_team_matchup_fact_tables),
    Migration(13, "013_create_player_diet_facts", _create_player_diet_fact_tables),
    Migration(14, "014_create_injury_snapshots", _create_injury_snapshot_table),
    Migration(15, "015_share_injury_source_snapshots", _share_injury_source_snapshots),
    Migration(
        16,
        "016_pbp_game_log_primitives",
        _upgrade_player_game_log_primitives,
    ),
    Migration(17, "017_collection_control_plane", _create_collection_control_plane_tables),
    Migration(18, "018_collection_operations", _upgrade_collection_operations),
    Migration(19, "019_surface_registry_metadata", _upgrade_surface_registry),
    Migration(20, "020_operator_control", _upgrade_operator_control),
    Migration(21, "021_collector_surface_authorization", _upgrade_collector_surface_authorization),
    Migration(22, "022_publication_provenance_reconciliation", _upgrade_provenance_and_reconciliation),
    Migration(23, "023_collector_release_status", _upgrade_collector_release_status),
    Migration(24, "024_canonical_game_ledger", _create_canonical_game_ledger_tables),
    Migration(25, "025_ledger_parity_artifacts", _create_ledger_parity_artifacts),
    Migration(26, "026_repair_publication_provenance_foreign_keys", _repair_publication_provenance_foreign_keys),
    Migration(27, "027_bind_ledger_parity_to_publications", _bind_ledger_parity_to_publications),
)


def run_migrations(engine: Engine) -> MigrationResult:
    """Apply pending application-schema migrations to ``engine``.

    Migrations are recorded in ``schema_migrations`` and are safe to run
    repeatedly.  An existing database without the bookkeeping table is
    treated as a pre-migration database: the current model schema is created
    if needed and then marked as applied without touching existing rows.
    """
    from app.utils.db import is_demo_database_url

    if is_demo_database_url(str(engine.url)):
        raise ValueError(
            "The tracked nba_play_types.db is a read-only demo database and "
            "cannot be an application migration target."
        )
    _validate_migration_order()

    with engine.begin() as connection:
        _migration_metadata.create_all(connection)
        applied_versions = {
            row.version
            for row in connection.execute(
                select(_schema_migrations.c.version)
            )
        }
        applied_names: list[str] = []

        for migration in MIGRATIONS:
            if migration.version in applied_versions:
                continue

            migration.upgrade(connection)
            connection.execute(
                insert(_schema_migrations).values(
                    version=migration.version,
                    name=migration.name,
                )
            )
            applied_versions.add(migration.version)
            applied_names.append(migration.name)

    return MigrationResult(
        applied=tuple(applied_names),
        current_version=max(applied_versions, default=0),
    )


def _validate_migration_order() -> None:
    """Reject duplicate or out-of-order migration definitions early."""
    versions = [migration.version for migration in MIGRATIONS]
    if versions != sorted(set(versions)):
        raise ValueError("Migrations must have unique, increasing versions")
