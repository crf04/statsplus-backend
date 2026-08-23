"""Repeatable migrations for the application's own database schema.

The bundled NBA data is a public read-only fixture and is not managed by
these migrations.  Migrations only create or upgrade tables owned by the
application, starting with the ``users`` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as PythonDateTime, timezone
import json
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
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql import func


MIGRATION_TABLE_NAME: Final[str] = "schema_migrations"
MIGRATION_ADVISORY_LOCK_ID: Final[int] = 0x5354415453504C55


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


def _add_team_matchup_ledger_lineage(connection: Connection) -> None:
    """Add ledger checksum and source-lineage columns to matchup read models.

    Migration 034 is reserved for issue #114.  Ledger-owned facts and surface
    observations record the exact governed game IDs they aggregated plus the
    deterministic ledger checksum of that selected game set.  Existing
    provider-collected rows keep ``NULL`` for both columns.
    """
    preparer = connection.dialect.identifier_preparer
    for table_name in ("team_matchup_facts", "team_matchup_surface_observations"):
        table = preparer.quote(table_name)
        existing = {
            column["name"]
            for column in inspect(connection).get_columns(table_name)
        }
        if "game_ids" not in existing:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN game_ids TEXT")
            )
        if "ledger_checksum" not in existing:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN ledger_checksum VARCHAR(64)")
            )
        if "source_observation_ids" not in existing:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN source_observation_ids TEXT")
            )
        if "game_set_checksum" not in existing:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN game_set_checksum VARCHAR(64)")
            )
        if "cutoff" not in existing:
            cutoff_type = (
                "TIMESTAMP WITH TIME ZONE"
                if connection.dialect.name == "postgresql"
                else "DATETIME"
            )
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN cutoff {cutoff_type}")
            )
        if "recomposition_reason" not in existing:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN recomposition_reason VARCHAR(128)")
            )


def _upgrade_correction_propagation(connection: Connection) -> None:
    """Add durable correction metadata without changing the migration head.

    The correction seam was added after migration 034 had already shipped in
    some environments.  Keeping this additive upgrade in migration 036 makes
    both fresh and upgraded temporary databases expose the same contract while
    preserving the repository's linear migration history.
    """
    preparer = connection.dialect.identifier_preparer
    timestamp_type = (
        "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME"
    )
    additions = {
        "composition_jobs": {
            "trigger_game_id": "VARCHAR(64)",
            "trigger_game_ids": "TEXT NOT NULL DEFAULT '[]'",
            "affected_team_ids": "TEXT NOT NULL DEFAULT '[]'",
            "source_observation_ids": "TEXT NOT NULL DEFAULT '[]'",
            "recomposition_reason": "VARCHAR(128)",
            "ledger_checksum": "VARCHAR(64)",
            "game_set_checksum": "VARCHAR(64)",
            "ledger_evidence": "TEXT NOT NULL DEFAULT '{}'",
            "generation": "INTEGER NOT NULL DEFAULT 1",
            "claimed_generation": "INTEGER",
        },
        "team_matchup_facts": {
            "source_observation_ids": "TEXT",
            "game_set_checksum": "VARCHAR(64)",
            "cutoff": timestamp_type,
            "recomposition_reason": "VARCHAR(128)",
        },
        "team_matchup_surface_observations": {
            "source_observation_ids": "TEXT",
            "game_set_checksum": "VARCHAR(64)",
            "cutoff": timestamp_type,
            "recomposition_reason": "VARCHAR(128)",
        },
    }
    correction_columns_added = False
    for table_name, table_additions in additions.items():
        existing = {
            column["name"]
            for column in inspect(connection).get_columns(table_name)
        }
        table = preparer.quote(table_name)
        for name, type_sql in table_additions.items():
            if name in existing:
                continue
            connection.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {preparer.quote(name)} {type_sql}"
            ))
            correction_columns_added |= table_name == "composition_jobs"

    # The compatibility backfill is only safe while the correction columns are
    # being introduced.  Re-running migrations is a normal startup operation;
    # once the schema is current, rewriting a live row could clear a worker's
    # claim while it is composing.
    if correction_columns_added:
        _backfill_correction_lineage(connection)


def _add_team_matchup_provider_provenance(connection: Connection) -> None:
    """Add immutable provider-window evidence to legacy matchup rows.

    Existing rows are deliberately not backfilled: a nullable/date-only row
    cannot prove its original authority or provider window and parity must
    reject it until a fresh materialization replaces it.
    """
    preparer = connection.dialect.identifier_preparer
    inspector = inspect(connection)
    for table_name in ("team_matchup_facts", "team_matchup_surface_observations"):
        if not inspector.has_table(table_name):
            continue
        existing = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        table = preparer.quote(table_name)
        for name, type_sql in {
            "manifest_id": "VARCHAR(128)",
            "event_catalog_publication_id": "VARCHAR(128)",
            "event_catalog_checksum": "VARCHAR(64)",
            "provider_window_identity": "TEXT",
        }.items():
            if name not in existing:
                connection.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {preparer.quote(name)} {type_sql}"
                ))


def _backfill_correction_lineage(connection: Connection) -> None:
    """Upgrade legacy singular/scalar queue lineage into keyed evidence.

    The additive columns intentionally had harmless defaults so old writes
    could continue during a rolling deploy.  A default ``[]``/``{}`` is not
    evidence, though: once the writer is upgraded, old queued rows must retain
    their singular trigger and checksum rather than silently becoming an
    unrelated empty job.
    """

    import hashlib
    import json

    table = Table("composition_jobs", MetaData(), autoload_with=connection)

    def parsed_list(value) -> list[str]:
        if value is None or value == "":
            return []
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if isinstance(parsed, list):
            return [
                str(item) for item in parsed
                if isinstance(item, (str, int)) and str(item)
            ]
        if isinstance(parsed, (str, int)) and not isinstance(parsed, bool):
            return [str(parsed)]
        return []

    def parsed_mapping(value) -> dict[str, str]:
        if value is None or value == "":
            return {}
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(game_id): str(checksum)
            for game_id, checksum in parsed.items()
            if isinstance(game_id, (str, int))
            and isinstance(checksum, (str, int))
            and str(game_id)
            and str(checksum)
        }

    rows = connection.execute(select(table)).mappings().all()
    for row in rows:
        trigger_ids = parsed_list(row.get("trigger_game_ids"))
        legacy_trigger = row.get("trigger_game_id")
        if not trigger_ids and legacy_trigger:
            trigger_ids = [str(legacy_trigger)]
        evidence = parsed_mapping(row.get("ledger_evidence"))
        if not evidence and trigger_ids and row.get("ledger_checksum"):
            # Legacy rows had only one checksum, so it can safely be bound to
            # the singular trigger.  For malformed multi-trigger legacy rows,
            # retain the evidence as one deterministic fallback rather than
            # inventing a checksum for an unknown game.
            if len(trigger_ids) == 1:
                evidence = {trigger_ids[0]: str(row["ledger_checksum"])}
        if not trigger_ids and evidence:
            trigger_ids = sorted(evidence)
        if not trigger_ids and not evidence and not row.get("trigger_game_id"):
            # There is no legacy evidence to recover; normalize only the
            # generation so future claims are still versioned.  A live claim
            # is not compatibility data and must survive this upgrade.
            values = {"generation": max(int(row.get("generation") or 0), 1)}
            connection.execute(table.update().where(table.c.job_id == row["job_id"]).values(**values))
            continue
        trigger_ids = sorted(set(trigger_ids) | set(evidence))
        if evidence:
            encoded_evidence = json.dumps(dict(sorted(evidence.items())), sort_keys=True, separators=(",", ":"))
            if len(evidence) == 1:
                checksum = next(iter(evidence.values()))
            else:
                checksum = hashlib.sha256(encoded_evidence.encode()).hexdigest()
        else:
            encoded_evidence = "{}"
            checksum = row.get("ledger_checksum")
        encoded_ids = json.dumps(trigger_ids, separators=(",", ":"))
        values = {
            "trigger_game_ids": encoded_ids,
            "trigger_game_id": trigger_ids[0] if len(trigger_ids) == 1 else None,
            "ledger_evidence": encoded_evidence,
            "ledger_checksum": checksum,
            "game_set_checksum": hashlib.sha256(
                json.dumps(trigger_ids, separators=(",", ":")).encode()
            ).hexdigest(),
            "generation": max(int(row.get("generation") or 0), 1),
        }
        connection.execute(table.update().where(table.c.job_id == row["job_id"]).values(**values))


def _add_team_matchup_publication_lineage(connection: Connection) -> None:
    """Add immutable NBA publication lineage to matchup read models.

    Publication-backed facts and observations retain the source publication,
    coverage cutoff, freshness classification, and version that were used at
    composition time.  Legacy and ledger-owned rows remain nullable.
    """
    preparer = connection.dialect.identifier_preparer
    additions = {
        "publication_id": "VARCHAR(128)",
        "publication_cutoff": "VARCHAR(64)",
        "publication_freshness": "VARCHAR(32)",
        "publication_version": "INTEGER",
    }
    for table_name in ("team_matchup_facts", "team_matchup_surface_observations"):
        table = preparer.quote(table_name)
        existing = {
            column["name"]
            for column in inspect(connection).get_columns(table_name)
        }
        for name, type_sql in additions.items():
            if name in existing:
                continue
            connection.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN "
                    f"{preparer.quote(name)} {type_sql}"
                )
            )


def _backfill_governed_catalog_freshness(connection: Connection) -> None:
    """Expose accepted governed catalogs through canonical freshness reads."""

    metadata = MetaData()
    publications = Table(
        "collection_catalog_publications", metadata, autoload_with=connection
    )
    events = Table("event_catalog", metadata, autoload_with=connection)
    event_freshness = Table(
        "event_catalog_refreshes", metadata, autoload_with=connection
    )
    athletes = Table("athlete_catalog", metadata, autoload_with=connection)
    athlete_freshness = Table(
        "athlete_catalog_freshness", metadata, autoload_with=connection
    )

    latest = connection.execute(
        select(
            publications.c.catalog_type,
            publications.c.season,
            func.max(publications.c.published_at).label("published_at"),
        )
        .where(publications.c.complete.is_(True))
        .group_by(publications.c.catalog_type, publications.c.season)
    ).mappings()
    for publication in latest:
        catalog_type = str(publication["catalog_type"])
        season = str(publication["season"])
        published_at = publication["published_at"]
        if catalog_type == "event":
            row_count = int(connection.scalar(
                select(func.count()).select_from(events).where(
                    events.c.season == season
                )
            ) or 0)
            existing = connection.scalar(
                select(event_freshness.c.season).where(
                    event_freshness.c.season == season
                )
            )
            values = {
                "last_attempt_at": published_at,
                "last_success_at": published_at,
                "failure_summary": None,
                "event_count": row_count,
            }
            if existing is None:
                connection.execute(
                    insert(event_freshness).values(season=season, **values)
                )
            else:
                connection.execute(
                    event_freshness.update()
                    .where(event_freshness.c.season == season)
                    .values(**values)
                )
            continue
        if catalog_type != "athlete":
            continue
        row_count = int(connection.scalar(
            select(func.count()).select_from(athletes).where(
                athletes.c.season == season
            )
        ) or 0)
        existing = connection.scalar(
            select(athlete_freshness.c.season).where(
                athlete_freshness.c.season == season
            )
        )
        values = {
            "last_success_at": published_at,
            "last_success_row_count": row_count,
            "last_failure_summary": None,
            "updated_at": published_at,
        }
        if existing is None:
            connection.execute(
                insert(athlete_freshness).values(season=season, **values)
            )
        else:
            connection.execute(
                athlete_freshness.update()
                .where(athlete_freshness.c.season == season)
                .values(**values)
            )


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


def _repair_canonical_game_ledger_tables(connection: Connection) -> None:
    """Recreate ledger tables missing from databases that recorded migration 024."""
    _create_canonical_game_ledger_tables(connection)


def _create_ledger_raw_row_evidence(connection: Connection) -> None:
    """Create the immutable complete PBP row archive for accepted games (#112).

    Games accepted before this migration were archived only as typed facts:
    they carry ``raw_checksum`` NULL and no ``canonical_game_ledger_raw_rows``
    rows.  The backfill re-fetches and re-archives them
    (``game_ids_without_raw_evidence``) before a season reports complete, so
    every accepted governed game eventually retains both team-summary and every
    player-row evidence.
    """

    from app.models.canonical_game_ledger import LedgerGameRowEvidence

    LedgerGameRowEvidence.__table__.create(connection, checkfirst=True)
    # A corrected source observation can change only the raw archived rows
    # while typed primitives remain identical.  The game identity row carries
    # a separate raw-evidence checksum so such a correction still atomically
    # replaces evidence instead of replaying as an idempotent no-op.
    table = "canonical_game_ledger_games"
    columns = {column["name"] for column in inspect(connection).get_columns(table)}
    if "raw_checksum" not in columns:
        connection.execute(text(
            f"ALTER TABLE {connection.dialect.identifier_preparer.quote(table)} "
            "ADD COLUMN raw_checksum VARCHAR(64)"
        ))


def _create_ledger_observation_evidence(connection: Connection) -> None:
    """Persist a durable reference for every accepted ledger observation (#113).

    A corrected game atomically replaces its typed facts and its archived raw
    rows, so the game row's current ``source_observation_id`` no longer names
    superseded observations.  This reference table keeps the observation ID of
    every observation that ever supplied an accepted game durable and
    queryable, so canonical-ledger evidence is exempt from the generic
    observation retention window and stays replayable and auditable
    indefinitely.  Existing accepted games are backfilled so historical
    evidence is protected immediately; only observations that still exist in
    ``collection_observations`` are referenced (the repair seam writes
    candidates without a staged observation).
    """

    from app.models.canonical_game_ledger import LedgerObservationEvidence

    LedgerObservationEvidence.__table__.create(connection, checkfirst=True)
    connection.execute(text(
        "INSERT INTO canonical_game_ledger_observation_evidence "
        "(observation_id, game_id, created_at) "
        "SELECT DISTINCT source_observation_id, game_id, CURRENT_TIMESTAMP "
        "FROM canonical_game_ledger_games "
        "WHERE source_observation_id IS NOT NULL "
        "AND EXISTS ("
        "  SELECT 1 FROM collection_observations "
        "  WHERE collection_observations.observation_id = "
        "    canonical_game_ledger_games.source_observation_id"
        ") "
        "ON CONFLICT (observation_id) DO NOTHING"
    ))


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


def _create_collector_status_transitions(connection: Connection) -> None:
    """Create append-only bounded machine lifecycle evidence."""

    from app.models.collection_control import CollectorStatusTransition

    CollectorStatusTransition.__table__.create(connection, checkfirst=True)


def _create_publication_activations(connection: Connection) -> None:
    """Persist explicit per-stream database-first activation evidence."""

    from app.models.collection_control import PublicationActivation

    PublicationActivation.__table__.create(connection, checkfirst=True)


def _upgrade_publication_activation_constraints(connection: Connection) -> None:
    """Bind activation evidence to an immutable candidate exactly once.

    Migration 029 intentionally introduced the additive evidence table without
    a relationship so it could be replayed alongside the parallel #85/#86
    histories.  This follow-up adds the durable foreign key and uniqueness
    rule.  PostgreSQL can alter the table in place; SQLite needs the small
    table-rebuild form because it cannot add a table-level foreign key.
    """

    from app.models.collection_control import PublicationActivation

    table_name = PublicationActivation.__tablename__
    inspector = inspect(connection)
    foreign_keys = inspector.get_foreign_keys(table_name)
    has_publication_fk = any(
        key.get("referred_table") == "publication_versions"
        and "publication_id" in set(key.get("constrained_columns") or ())
        for key in foreign_keys
    )
    unique_indexes = inspector.get_indexes(table_name)
    has_unique_activation = any(
        index.get("name") == "uq_publication_activations_stream_publication"
        and index.get("unique")
        and tuple(index.get("column_names") or ()) == ("stream_key", "publication_id")
        for index in unique_indexes
    )

    if connection.dialect.name == "sqlite" and not has_publication_fk:
        preparer = connection.dialect.identifier_preparer
        old = preparer.quote(table_name)
        rebuilt_name = f"{table_name}__030"
        rebuilt = preparer.quote(rebuilt_name)
        # The migration runner executes each migration in a transaction.  A
        # SQLite foreign_keys pragma change inside that transaction is a no-op
        # (and would make the rebuild appear to work while leaving the
        # connection in an ambiguous integrity state).  The activation table
        # has no inbound references, so it can be rebuilt with enforcement on.
        # Fail closed if a caller supplied a connection that did not preserve
        # the application's invariant.
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
            raise RuntimeError(
                "migration 030 requires SQLite foreign_keys enforcement"
            )
        connection.execute(text(
            f"CREATE TABLE {rebuilt} ("
            "activation_id VARCHAR(36) NOT NULL PRIMARY KEY, "
            "stream_key VARCHAR(96) NOT NULL, "
            "publication_id VARCHAR(36) NOT NULL, "
            "actor VARCHAR(128) NOT NULL, "
            "reason VARCHAR(255) NOT NULL, "
            "fence INTEGER NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "CONSTRAINT fk_publication_activation_publication "
            "FOREIGN KEY(publication_id) REFERENCES publication_versions(publication_id) "
            "ON DELETE RESTRICT"
            ")"
        ))
        connection.execute(text(
            f"INSERT INTO {rebuilt} "
            "(activation_id, stream_key, publication_id, actor, reason, fence, created_at) "
            f"SELECT activation_id, stream_key, publication_id, actor, reason, fence, created_at FROM {old}"
        ))
        connection.execute(text(f"DROP TABLE {old}"))
        connection.execute(text(f"ALTER TABLE {rebuilt} RENAME TO {old}"))
        connection.execute(text(
            f"CREATE INDEX ix_publication_activations_stream_created "
            f"ON {old} (stream_key, created_at)"
        ))
        connection.execute(text(
            f"CREATE UNIQUE INDEX uq_publication_activations_stream_publication "
            f"ON {old} (stream_key, publication_id)"
        ))
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
            raise RuntimeError(
                "migration 030 left SQLite foreign_keys enforcement disabled"
            )
        return

    if not has_publication_fk:
        connection.execute(text(
            f"ALTER TABLE {connection.dialect.identifier_preparer.quote(table_name)} "
            "ADD CONSTRAINT fk_publication_activation_publication "
            "FOREIGN KEY (publication_id) REFERENCES publication_versions(publication_id) "
            "ON DELETE RESTRICT"
        ))
    if not has_unique_activation:
        connection.execute(text(
            f"CREATE UNIQUE INDEX uq_publication_activations_stream_publication "
            f"ON {connection.dialect.identifier_preparer.quote(table_name)} "
            "(stream_key, publication_id)"
        ))


def _create_publication_player_game_log_projection(
    connection: Connection,
) -> None:
    """Create and backfill immutable player-log rows keyed by publication."""

    _upgrade_correction_propagation(connection)

    from app.models.collection_control import PublicationVersion
    from app.models.player_game_log import PublicationPlayerGameLog
    from app.services.database_first_activation import PublicationPayloadError
    from app.services.player_game_log_projection import (
        write_player_game_log_projection,
    )

    projection_table = PublicationPlayerGameLog.__table__
    projection_table.create(connection, checkfirst=True)
    projected_ids = set(
        connection.execute(
            select(projection_table.c.publication_id).distinct()
        ).scalars()
    )
    publication_table = PublicationVersion.__table__
    publications = connection.execute(
        select(
            publication_table.c.publication_id,
            publication_table.c.season,
            publication_table.c.payload,
        ).where(publication_table.c.stream_key == "player_game_logs")
    ).mappings()
    for publication in publications:
        publication_id = str(publication["publication_id"])
        if publication_id in projected_ids:
            continue
        try:
            write_player_game_log_projection(
                connection,
                publication_id,
                publication["payload"],
                season=str(publication["season"]),
            )
        except PublicationPayloadError:
            # Existing malformed publications already read as unavailable.
            # Preserve that fail-closed state instead of blocking deployment.
            continue


def _create_projection_archive_tables(connection: Connection) -> None:
    """Create immutable projection evidence and its live read model."""

    from app.models.projection_archive import (
        LatestPlayerProjection,
        ProjectionArchiveScopeLock,
        ProjectionMaterializationGeneration,
        ProjectionObservation,
        ProjectionProviderSnapshot,
        ProviderPoll,
    )

    # Foreign-key dependencies determine the portable creation order.
    ProjectionArchiveScopeLock.__table__.create(connection, checkfirst=True)
    ProjectionProviderSnapshot.__table__.create(connection, checkfirst=True)
    ProviderPoll.__table__.create(connection, checkfirst=True)
    ProjectionMaterializationGeneration.__table__.create(
        connection, checkfirst=True
    )
    ProjectionObservation.__table__.create(connection, checkfirst=True)
    LatestPlayerProjection.__table__.create(connection, checkfirst=True)


def _create_projection_collection_tables(connection: Connection) -> None:
    """Create the fenced projection collector lease and provider state."""

    from app.models.projection_archive import ProviderPoll
    from app.models.projection_collection import (
        ProjectionCollectionLease,
        ProjectionCollectionProviderState,
    )

    ProjectionCollectionLease.__table__.create(connection, checkfirst=True)
    ProjectionCollectionProviderState.__table__.create(connection, checkfirst=True)
    existing = {column["name"] for column in inspect(connection).get_columns(ProviderPoll.__tablename__)}
    if "duration_ms" not in existing:
        preparer = connection.dialect.identifier_preparer
        type_sql = "INTEGER"
        connection.execute(
            text(
                f"ALTER TABLE {preparer.quote(ProviderPoll.__tablename__)} "
                f"ADD COLUMN {preparer.quote('duration_ms')} {type_sql}"
            )
        )


def _v40_projection_poll_promoted_predicate(
    *,
    poll_ref: str,
    generation_ref: str,
    poll_table: str,
    generation_table: str,
) -> str:
    """Reconstruct whether one v40 poll crossed its temporal promotion fence."""

    return (
        f"({generation_ref}.source_poll_id = {poll_ref}.poll_id OR ("
        f"{poll_ref}.outcome = 'unchanged' "
        f"AND {poll_ref}.retrieved_at >= {generation_ref}.retrieved_at "
        "AND NOT EXISTS (SELECT 1 "
        f"FROM {poll_table} AS newer_poll "
        f"JOIN {generation_table} AS newer_generation "
        "ON newer_generation.generation_id = newer_poll.generation_id "
        f"WHERE newer_poll.provider = {poll_ref}.provider "
        f"AND newer_poll.season = {poll_ref}.season "
        f"AND newer_poll.query_key = {poll_ref}.query_key "
        "AND newer_generation.outcome IN ('advanced', 'rematerialized') "
        f"AND newer_poll.completed_at < {poll_ref}.completed_at "
        f"AND newer_poll.retrieved_at > {poll_ref}.retrieved_at)))"
    )


def _upgrade_projection_archive_transitions(connection: Connection) -> None:
    """Upgrade migration-40 projection tables for truthful poll transitions."""

    from app.models.projection_archive import (
        LatestPlayerProjection,
        ProjectionMaterializationGeneration,
        ProjectionObservation,
        ProviderPoll,
    )

    inspector = inspect(connection)
    poll_columns = {
        column["name"]: column
        for column in inspector.get_columns("projection_provider_polls")
    }
    latest_columns = {
        column["name"]: column
        for column in inspector.get_columns("latest_player_projections")
    }
    already_current = (
        {"failure_reason", "promoted"} <= set(poll_columns)
        and poll_columns["retrieved_at"]["nullable"]
        and poll_columns["generation_id"]["nullable"]
        and "confirmed_at" in latest_columns
        and not latest_columns["confirmed_at"]["nullable"]
    )
    if already_current:
        return

    if connection.dialect.name == "sqlite":
        _rebuild_projection_transition_tables_sqlite(
            connection,
            (
                ProviderPoll.__table__,
                ProjectionMaterializationGeneration.__table__,
                ProjectionObservation.__table__,
                LatestPlayerProjection.__table__,
            ),
        )
        return

    poll_name = connection.dialect.identifier_preparer.quote(
        "projection_provider_polls"
    )
    generation_name = connection.dialect.identifier_preparer.quote(
        "projection_materialization_generations"
    )
    latest_name = connection.dialect.identifier_preparer.quote(
        "latest_player_projections"
    )
    promotion_predicate = _v40_projection_poll_promoted_predicate(
        poll_ref="poll",
        generation_ref="generation",
        poll_table=poll_name,
        generation_table=generation_name,
    )
    if "failure_reason" not in poll_columns:
        connection.execute(text(
            f"ALTER TABLE {poll_name} ADD COLUMN failure_reason VARCHAR(64)"
        ))
    if "promoted" not in poll_columns:
        connection.execute(text(
            f"ALTER TABLE {poll_name} ADD COLUMN promoted BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        connection.execute(text(
            f"UPDATE {poll_name} AS poll SET promoted = EXISTS ("
            f"SELECT 1 FROM {generation_name} AS generation "
            "WHERE generation.generation_id = poll.generation_id "
            "AND generation.outcome IN ('advanced', 'rematerialized') "
            f"AND {promotion_predicate})"
        ))
    connection.execute(text(
        f"ALTER TABLE {poll_name} ALTER COLUMN retrieved_at DROP NOT NULL"
    ))
    connection.execute(text(
        f"ALTER TABLE {poll_name} ALTER COLUMN generation_id DROP NOT NULL"
    ))
    if "confirmed_at" not in latest_columns:
        connection.execute(text(
            f"ALTER TABLE {latest_name} ADD COLUMN confirmed_at TIMESTAMP WITH TIME ZONE"
        ))
        connection.execute(text(
            f"UPDATE {latest_name} AS latest SET confirmed_at = COALESCE (("
            f"SELECT MAX(poll.retrieved_at) FROM {poll_name} AS poll "
            f"JOIN {generation_name} AS generation "
            "ON generation.generation_id = poll.generation_id "
            "WHERE poll.generation_id = latest.generation_id "
            "AND generation.outcome IN ('advanced', 'rematerialized') "
            f"AND {promotion_predicate}"
            "), latest.observed_at)"
        ))
        connection.execute(text(
            f"ALTER TABLE {latest_name} ALTER COLUMN confirmed_at SET NOT NULL"
        ))
    constraint_names = {
        constraint["name"]
        for constraint in inspect(connection).get_check_constraints(
            "projection_provider_polls"
        )
    }
    if "ck_projection_provider_poll_outcome" not in constraint_names:
        connection.execute(text(
            f"ALTER TABLE {poll_name} ADD CONSTRAINT ck_projection_provider_poll_outcome "
            "CHECK (outcome IN ('changed', 'partial', 'rematerialized', 'unchanged', 'failed'))"
        ))
    if "ck_projection_provider_poll_payload" not in constraint_names:
        connection.execute(text(
            f"ALTER TABLE {poll_name} ADD CONSTRAINT ck_projection_provider_poll_payload CHECK ("
            "(outcome = 'failed' AND promoted = FALSE AND snapshot_id IS NULL "
            "AND generation_id IS NULL AND retrieved_at IS NULL AND failure_reason IS NOT NULL) OR "
            "(outcome <> 'failed' AND snapshot_id IS NOT NULL AND generation_id IS NOT NULL "
            "AND retrieved_at IS NOT NULL AND failure_reason IS NULL))"
        ))


def _rebuild_projection_transition_tables_sqlite(
    connection: Connection,
    tables: tuple[Table, ...],
) -> None:
    """Rebuild the v40 FK-connected projection cluster on SQLite."""

    suffix = "__041"
    names = {table.name: f"{table.name}{suffix}" for table in tables}
    for table in tables:
        ddl = str(CreateTable(table).compile(dialect=connection.dialect))
        for original, temporary in sorted(
            names.items(), key=lambda item: len(item[0]), reverse=True
        ):
            ddl = ddl.replace(original, temporary)
        connection.exec_driver_sql(ddl)

    inspector = inspect(connection)
    for table in tables:
        source_columns = {
            column["name"]
            for column in inspector.get_columns(table.name)
        }
        destinations = [column.name for column in table.columns]
        expressions = []
        for column in destinations:
            if column in source_columns:
                expressions.append(column)
            elif column == "confirmed_at":
                promotion_predicate = _v40_projection_poll_promoted_predicate(
                    poll_ref="poll",
                    generation_ref="generation",
                    poll_table="projection_provider_polls",
                    generation_table="projection_materialization_generations",
                )
                expressions.append(
                    "COALESCE((SELECT MAX(poll.retrieved_at) "
                    "FROM projection_provider_polls AS poll "
                    "JOIN projection_materialization_generations AS generation "
                    "ON generation.generation_id = poll.generation_id "
                    f"WHERE poll.generation_id = {table.name}.generation_id "
                    "AND generation.outcome IN ('advanced', 'rematerialized') "
                    f"AND {promotion_predicate}), "
                    "observed_at) AS confirmed_at"
                )
            elif column == "promoted":
                promotion_predicate = _v40_projection_poll_promoted_predicate(
                    poll_ref=table.name,
                    generation_ref="generation",
                    poll_table="projection_provider_polls",
                    generation_table="projection_materialization_generations",
                )
                expressions.append(
                    "CASE WHEN EXISTS (SELECT 1 "
                    "FROM projection_materialization_generations AS generation "
                    f"WHERE generation.generation_id = {table.name}.generation_id "
                    "AND generation.outcome IN ('advanced', 'rematerialized') "
                    f"AND {promotion_predicate}) "
                    "THEN 1 ELSE 0 END AS promoted"
                )
            elif column == "failure_reason":
                expressions.append("NULL AS failure_reason")
            elif column == "duration_ms":
                expressions.append("NULL AS duration_ms")
            else:
                raise RuntimeError(
                    f"migration 041 cannot backfill projection column {column}"
                )
        connection.exec_driver_sql(
            f"INSERT INTO {names[table.name]} ({', '.join(destinations)}) "
            f"SELECT {', '.join(expressions)} FROM {table.name}"
        )

    for table in reversed(tables):
        connection.exec_driver_sql(f"DROP TABLE {table.name}")
    for table in tables:
        connection.exec_driver_sql(
            f"ALTER TABLE {names[table.name]} RENAME TO {table.name}"
        )
    for table in tables:
        for index in table.indexes:
            connection.exec_driver_sql(
                str(CreateIndex(index).compile(dialect=connection.dialect))
            )
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError("migration 041 left invalid projection foreign keys")
def _bind_manifests_to_event_catalog_publications(
    connection: Connection,
) -> None:
    """Bind each manifest to the exact immutable Event Catalog it validated."""

    from app.models.collection_control import CatalogPublication, CollectionManifest

    preparer = connection.dialect.identifier_preparer
    table_name = preparer.quote(CollectionManifest.__tablename__)
    columns = {
        column["name"]
        for column in inspect(connection).get_columns(
            CollectionManifest.__tablename__
        )
    }
    if "event_catalog_publication_id" not in columns:
        connection.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN "
            "event_catalog_publication_id VARCHAR(36) REFERENCES "
            "collection_catalog_publications(publication_id) ON DELETE RESTRICT"
        ))
    if "event_catalog_checksum" not in columns:
        connection.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN "
            "event_catalog_checksum VARCHAR(64)"
        ))

    manifest_table = CollectionManifest.__table__
    catalog_table = CatalogPublication.__table__
    manifests = connection.execute(select(
        manifest_table.c.manifest_id,
        manifest_table.c.season,
        manifest_table.c.cutoff,
        manifest_table.c.created_at,
        manifest_table.c.event_catalog_publication_id,
    )).mappings()
    for manifest in manifests:
        if manifest["event_catalog_publication_id"]:
            continue
        catalogs = list(connection.execute(
            select(
                catalog_table.c.publication_id,
                catalog_table.c.checksum,
                catalog_table.c.published_at,
            ).where(
                catalog_table.c.season == manifest["season"],
                catalog_table.c.catalog_type == "event",
                catalog_table.c.cutoff == manifest["cutoff"],
                catalog_table.c.complete.is_(True),
            ).order_by(catalog_table.c.published_at.desc())
        ).mappings())
        eligible = [
            catalog
            for catalog in catalogs
            if catalog["published_at"] <= manifest["created_at"]
        ]
        if len(eligible) != 1:
            # Ambiguous legacy rows stay explicitly unbound and therefore
            # fail closed in governance reads.
            continue
        catalog = eligible[0]
        connection.execute(
            manifest_table.update().where(
                manifest_table.c.manifest_id == manifest["manifest_id"]
            ).values(
                event_catalog_publication_id=catalog["publication_id"],
                event_catalog_checksum=catalog["checksum"],
            )
        )


def _bind_publication_versions_to_manifest_authority(
    connection: Connection,
) -> None:
    """Bind governed versions to one unambiguous manifest/catalog authority."""

    from app.models.collection_control import (
        CatalogPublication,
        CollectionManifest,
        CollectionObservation,
        PublicationObservation,
        PublicationVersion,
    )
    from app.domain.team_matchup_taxonomy import NBA_PUBLICATION_STREAM_KEYS

    preparer = connection.dialect.identifier_preparer
    table_name = preparer.quote(PublicationVersion.__tablename__)
    columns = {
        column["name"]
        for column in inspect(connection).get_columns(
            PublicationVersion.__tablename__
        )
    }
    additions = (
        (
            "manifest_id",
            "VARCHAR(36) REFERENCES collection_manifests(manifest_id) "
            "ON DELETE RESTRICT",
        ),
        (
            "event_catalog_publication_id",
            "VARCHAR(36) REFERENCES "
            "collection_catalog_publications(publication_id) ON DELETE RESTRICT",
        ),
        ("event_catalog_checksum", "VARCHAR(64)"),
    )
    for column_name, definition in additions:
        if column_name not in columns:
            connection.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN "
                f"{column_name} {definition}"
            ))

    version_table = PublicationVersion.__table__
    manifest_table = CollectionManifest.__table__
    catalog_table = CatalogPublication.__table__

    def normalized_cutoff(value):
        if isinstance(value, str):
            value = PythonDateTime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    versions = connection.execute(select(
        version_table.c.publication_id,
        version_table.c.season,
        version_table.c.cutoff,
        version_table.c.manifest_id,
        version_table.c.stream_key,
    )).mappings()
    for version in versions:
        if (
            version["manifest_id"]
            or version["stream_key"] not in NBA_PUBLICATION_STREAM_KEYS
        ):
            continue
        manifests = list(connection.execute(
            select(
                manifest_table.c.manifest_id,
                manifest_table.c.cutoff,
                manifest_table.c.scopes,
                manifest_table.c.accepted_versions,
                manifest_table.c.event_catalog_publication_id,
                manifest_table.c.event_catalog_checksum,
            ).where(
                manifest_table.c.season == version["season"],
            )
        ).mappings())
        try:
            manifests = [
                manifest
                for manifest in manifests
                if (
                    normalized_cutoff(manifest["cutoff"])
                    == normalized_cutoff(version["cutoff"])
                    and "canonical_game_ledger"
                    in set(json.loads(manifest["scopes"]))
                    and 1 in set(json.loads(manifest["accepted_versions"]))
                )
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(manifests) != 1:
            continue
        manifest = manifests[0]
        if (
            not manifest["event_catalog_publication_id"]
            or not manifest["event_catalog_checksum"]
        ):
            continue
        catalogs = list(connection.execute(select(
            catalog_table.c.publication_id,
            catalog_table.c.cutoff,
            catalog_table.c.checksum,
        ).where(
            catalog_table.c.season == version["season"],
            catalog_table.c.catalog_type == "event",
            catalog_table.c.complete.is_(True),
        )).mappings())
        catalogs = [
            catalog
            for catalog in catalogs
            if normalized_cutoff(catalog["cutoff"])
            == normalized_cutoff(version["cutoff"])
        ]
        if (
            len(catalogs) != 1
            or catalogs[0]["publication_id"]
            != manifest["event_catalog_publication_id"]
            or catalogs[0]["checksum"] != manifest["event_catalog_checksum"]
        ):
            continue
        catalog = catalogs[0]
        provenance_manifest_ids = set(connection.scalars(
            select(CollectionObservation.manifest_id)
            .select_from(
                PublicationObservation.__table__.join(
                    CollectionObservation.__table__,
                    PublicationObservation.observation_id
                    == CollectionObservation.observation_id,
                )
            )
            .where(
                PublicationObservation.publication_id
                == version["publication_id"]
            )
        ))
        if provenance_manifest_ids and provenance_manifest_ids != {
            manifest["manifest_id"]
        }:
            continue
        connection.execute(version_table.update().where(
            version_table.c.publication_id == version["publication_id"]
        ).values(
            manifest_id=manifest["manifest_id"],
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
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
    # #85 was developed in parallel with #86.  Keep its additive lifecycle
    # evidence migration after the ledger migrations so both histories remain
    # replayable on one linear schema.
    Migration(28, "028_collector_status_transitions", _create_collector_status_transitions),
    Migration(29, "029_publication_activations", _create_publication_activations),
    Migration(30, "030_bind_publication_activation_candidates", _upgrade_publication_activation_constraints),
    Migration(31, "031_repair_canonical_game_ledger_tables", _repair_canonical_game_ledger_tables),
    Migration(32, "032_ledger_raw_row_evidence", _create_ledger_raw_row_evidence),
    Migration(33, "033_ledger_observation_evidence", _create_ledger_observation_evidence),
    Migration(34, "034_team_matchup_ledger_lineage", _add_team_matchup_ledger_lineage),
    Migration(35, "035_governed_catalog_freshness", _backfill_governed_catalog_freshness),
    Migration(
        36,
        "036_publication_player_game_log_projection",
        _create_publication_player_game_log_projection,
    ),
    Migration(
        37,
        "037_team_matchup_publication_lineage",
        _add_team_matchup_publication_lineage,
    ),
    Migration(
        38,
        "038_bind_manifests_to_event_catalog_publications",
        _bind_manifests_to_event_catalog_publications,
    ),
    Migration(
        39,
        "039_bind_publication_versions_to_manifest_authority",
        _bind_publication_versions_to_manifest_authority,
    ),
    Migration(40, "040_projection_archive", _create_projection_archive_tables),
    Migration(
        41,
        "041_projection_archive_transitions",
        _upgrade_projection_archive_transitions,
    ),
    Migration(
        42,
        "042_team_matchup_provider_provenance",
        _add_team_matchup_provider_provenance,
    ),
    Migration(
        43,
        "043_projection_collection_control",
        _create_projection_collection_tables,
    ),
)


def _acquire_migration_lock(connection: Connection) -> None:
    """Serialize PostgreSQL migrations for the duration of their transaction."""
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
        )


def _repair_legacy_matchup_provider_migration(connection: Connection) -> None:
    """Move the pre-linearized provider migration history to version 042.

    One short-lived branch recorded the provider-provenance upgrade as
    ``(40, 040_team_matchup_provider_provenance)``.  Version 040 is now owned
    by the projection archive, so leaving that row in place causes a current
    database to skip the archive and fail when migration 041 runs.  Repair
    only that exact historical name; a legitimate projection-archive row at
    version 040 must remain untouched.
    """

    legacy = connection.execute(
        select(
            _schema_migrations.c.version,
            _schema_migrations.c.name,
            _schema_migrations.c.applied_at,
        ).where(_schema_migrations.c.version == 40)
    ).mappings().one_or_none()
    if legacy is None or legacy["name"] != "040_team_matchup_provider_provenance":
        return

    canonical_name = "042_team_matchup_provider_provenance"
    existing_042 = connection.execute(
        select(_schema_migrations.c.name).where(_schema_migrations.c.version == 42)
    ).scalar_one_or_none()
    if existing_042 is not None and existing_042 != canonical_name:
        raise ValueError("migration version 042 has an unexpected name")

    connection.execute(
        _schema_migrations.delete().where(_schema_migrations.c.version == 40)
    )
    if existing_042 is None:
        connection.execute(
            insert(_schema_migrations).values(
                version=42,
                name=canonical_name,
                applied_at=legacy["applied_at"],
            )
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
        _acquire_migration_lock(connection)
        _migration_metadata.create_all(connection)
        _repair_legacy_matchup_provider_migration(connection)
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

        # Issue #116 extends the already-published migration-036 head.  Run
        # this additive, idempotent compatibility upgrade for databases that
        # recorded 036 before the correction metadata existed; fresh databases
        # have already executed it from the 036 upgrade function above.
        if max(applied_versions, default=0) >= 36:
            _upgrade_correction_propagation(connection)

    return MigrationResult(
        applied=tuple(applied_names),
        current_version=max(applied_versions, default=0),
    )


def _validate_migration_order() -> None:
    """Reject duplicate or out-of-order migration definitions early."""
    versions = [migration.version for migration in MIGRATIONS]
    if versions != sorted(set(versions)):
        raise ValueError("Migrations must have unique, increasing versions")
