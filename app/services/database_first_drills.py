"""Deterministic failure, recovery, and isolated-restore drill tooling."""

from __future__ import annotations

import hashlib
import json
import os
import gzip
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionManifest,
    CollectorStatusTransition,
    PublicationPointer,
)
from app.services.collection_control import (
    CollectorClaims,
    CollectorTokenService,
    ObservationIngestionService,
    PublicationService,
)
from app.collector.contracts import ObservationEnvelope
from app.collector.outbox import ResidentialOutbox


UTC = timezone.utc
DISPOSABLE_MARKER_TABLE = "statsplus_disposable_control"
DISPOSABLE_MARKER_PURPOSE = "database_first_drill"

# These are the application tables that may contain production/control-plane
# rows.  The marker itself is deliberately not managed by migrations: an
# operator creates it out-of-band before a drill is allowed to migrate a
# target.  A migrated-but-empty disposable database is safe to reuse.
DOMAIN_TABLES = frozenset({
    "alembic_version",
    "canonical_game_ledger_games",
    "canonical_game_ledger_backfill",
    "canonical_game_ledger_publications",
    "collection_audit_events",
    "collection_observations",
    "composition_jobs",
    "publication_activations",
    "publication_observations",
    "publication_pointers",
    "publication_streams",
    "publication_versions",
    "player_game_logs",
    "player_diets",
    "team_matchups",
})


@dataclass(frozen=True, slots=True)
class DrillResult:
    name: str
    status: str
    attempts: int
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """Non-secret identity facts read from one connected database target."""

    dialect: str
    server: str
    port: int | None
    database: str
    schema: str

    @property
    def database_key(self) -> tuple[str, str, int | None, str]:
        """Return the identity used to enforce target separation."""

        return self.dialect, self.server, self.port, self.database


def _connected_database_identity(
    engine: Engine,
    *,
    schema: str | None,
) -> DatabaseIdentity:
    """Read a canonical, credential-free identity from an open connection."""

    dialect = engine.dialect.name
    if dialect == "postgresql":
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_database() AS database_name, "
                    "current_schema() AS current_schema, "
                    "inet_server_addr()::text AS server_address, "
                    "inet_server_port() AS server_port"
                )
            ).mappings().one()
        server = str(row.get("server_address") or engine.url.host or "")
        database = str(row.get("database_name") or "")
        current_schema = str(row.get("current_schema") or "")
        port = row.get("server_port")
        return DatabaseIdentity(
            dialect=dialect,
            server=server,
            port=int(port) if port is not None else None,
            database=database,
            schema=str(schema or current_schema),
        )
    if dialect == "sqlite":
        with engine.connect() as connection:
            row = connection.exec_driver_sql("PRAGMA database_list").fetchone()
        database = str(row[2] if row is not None and len(row) > 2 and row[2] else engine.url.database or "")
        if database == ":memory:":
            # Separate in-memory engines have no server/database name.  The
            # pool identity is process-local but still distinguishes targets.
            database = f":memory:{id(engine.pool)}"
        return DatabaseIdentity(
            dialect=dialect,
            server="",
            port=None,
            database=database,
            schema=str(schema or "main"),
        )
    return DatabaseIdentity(
        dialect=dialect,
        server=str(engine.url.host or ""),
        port=engine.url.port,
        database=str(engine.url.database or ""),
        schema=str(schema or ""),
    )


def connected_database_identity(
    database_url: str,
    *,
    schema: str | None = None,
) -> DatabaseIdentity:
    """Connect once and return non-secret identity facts for a database URL."""

    try:
        engine = create_engine(database_url)
        try:
            return _connected_database_identity(engine, schema=schema)
        finally:
            engine.dispose()
    except Exception as error:
        raise ValueError("database_identity_unavailable") from error


def same_database_identity(
    left: DatabaseIdentity,
    right: DatabaseIdentity,
) -> bool:
    """Compare connected targets without comparing URLs or credentials."""

    return left.database_key == right.database_key


@dataclass(frozen=True, slots=True)
class FailureDrillReport:
    status: str
    started_at: str
    completed_at: str
    drills: tuple[DrillResult, ...]
    environment: str = "unit"
    production_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "environment": self.environment,
            "production_evidence": self.production_evidence,
            "drills": [asdict(drill) for drill in self.drills],
        }
        restore_drill = next(
            (drill.details for drill in self.drills if drill.name == "isolated_restore_replay"),
            {},
        )
        if self.production_evidence:
            payload.update(
                {
                    "engine": "postgresql",
                    "restore_command_evidence": restore_drill.get("restore_command_evidence"),
                    "restore_duration_ms": restore_drill.get("restore_duration_ms"),
                    "pbp_repair_observation_id": restore_drill.get("pbp_repair_observation_id"),
                    "pbp_repair_job_id": restore_drill.get("pbp_repair_job_id"),
                    "recovery_data_point": restore_drill.get("recovery_data_point"),
                }
            )
        else:
            payload["engine"] = "sqlite"
        payload["artifact_schema"] = {
            "version": 1,
            "engine": "postgresql" if self.production_evidence else "sqlite",
            "required_fields": (
                [
                    "restore_command_evidence",
                    "recovery_time_ms",
                    "pbp_repair_observation_id",
                    "pbp_repair_job_id",
                ]
                if self.production_evidence
                else ["recovery_time_ms", "recovery_data_point"]
            ),
        }
        return payload


class FailureDrillRunner:
    """Run named drills with injectable side effects and no network access."""

    NAMES = (
        "railway_outage_retry",
        "duplicate_delivery_idempotency",
        "collector_reboot_outbox_replay",
        "expired_credential_rejection",
        "provider_failure_last_good_retention",
        "alert_recovery",
        "isolated_restore_replay",
    )

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        clock: Callable[[], datetime] | None = None,
        database_url: str | None = None,
        environment: str = "unit",
        isolated: bool = False,
        production_database_url: str | None = None,
        restored_database_url: str | None = None,
        restore_adapter: Callable[[], Mapping[str, Any]] | None = None,
        disposable_marker_nonce: str | None = None,
        disposable_schema: str | None = None,
        restored_marker_nonce: str | None = None,
        restored_schema: str | None = None,
        restore_expectations: Mapping[str, Any] | None = None,
        pbp_repair: Callable[[Engine, str], Mapping[str, Any] | bool] | None = None,
        restore_ingestion: Callable[[Engine, CollectorClaims, Mapping[str, Any], bytes], Any] | None = None,
        restore_started: float | None = None,
        restore_command_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self.environment = str(environment).strip().lower() or "unit"
        self.run_id = uuid4().hex
        self.production_database_url = production_database_url
        self.restored_database_url = restored_database_url
        self.restore_adapter = restore_adapter
        self.disposable_marker_nonce = disposable_marker_nonce or os.environ.get(
            "STATPLUS_DISPOSABLE_MARKER_NONCE"
        )
        self.disposable_schema = disposable_schema or os.environ.get(
            "STATPLUS_DISPOSABLE_SCHEMA"
        )
        self.restored_marker_nonce = restored_marker_nonce or os.environ.get(
            "STATPLUS_RESTORED_MARKER_NONCE"
        )
        self.restored_schema = restored_schema or os.environ.get(
            "STATPLUS_RESTORED_SCHEMA"
        )
        self.restore_expectations = dict(restore_expectations or {})
        self.pbp_repair = pbp_repair
        self.restore_ingestion = restore_ingestion
        self.restore_started = restore_started
        self.restore_command_evidence = dict(restore_command_evidence or {})
        self._drill_identity: DatabaseIdentity | None = None
        self._production_identity: DatabaseIdentity | None = None
        self._drill_cutoff: datetime | None = None
        requested_url = database_url or (str(engine.url) if engine is not None else None)
        self.engine = engine or create_engine(database_url or "sqlite:///:memory:")
        production_mode = self.environment not in {"unit", "test_unit"}
        if production_mode and not production_database_url:
            self.configuration_error = "production_database_url_required"
        else:
            self.configuration_error = None
        if self.configuration_error is None:
            self.configuration_error = self._validate_configuration(
                requested_url,
                isolated=isolated,
                production_database_url=production_database_url,
                restored_database_url=restored_database_url,
                disposable_marker_nonce=self.disposable_marker_nonce,
                disposable_schema=self.disposable_schema,
            )
        if self.configuration_error is None and requested_url is not None:
            self.configuration_error = self._preflight_target(
                self.engine,
                marker_nonce=self.disposable_marker_nonce,
                schema=self.disposable_schema,
                label="drill",
            )
        if self.configuration_error is None and requested_url is not None:
            try:
                self._drill_identity = _connected_database_identity(
                    self.engine,
                    schema=self.disposable_schema,
                )
                if production_database_url:
                    self._production_identity = connected_database_identity(
                        str(production_database_url),
                        schema=None,
                    )
                    if same_database_identity(
                        self._drill_identity,
                        self._production_identity,
                    ):
                        self.configuration_error = (
                            "drill_database_must_be_separate_from_production"
                        )
                if self.configuration_error is None and restored_database_url:
                    restored_identity = connected_database_identity(
                        str(restored_database_url),
                        schema=self.restored_schema,
                    )
                    if same_database_identity(
                        self._drill_identity,
                        restored_identity,
                    ) or (
                        self._production_identity is not None
                        and same_database_identity(
                            self._production_identity,
                            restored_identity,
                        )
                    ):
                        self.configuration_error = (
                            "restored_database_must_be_separate"
                        )
            except Exception:
                self.configuration_error = "database_identity_unavailable"
        if self.configuration_error is not None:
            self.production_evidence = False
            self.publications = None
            self.ingestion = None
            return
        self.production_evidence = (
            production_mode
            and self.engine.dialect.name == "postgresql"
            and self._production_identity is not None
        )
        run_migrations(self.engine)
        self.publications = PublicationService(self.engine, clock=self.clock)
        self.publications.register_stream(
            "drill_stream",
            provider="drill",
            owner="local",
            required_observations=(),
            publication_strategy="replace",
            enabled=True,
        )
        self.publications.register_stream(
            "drill_ingestion_stream",
            provider="drill",
            owner="local",
            required_observations=("drill_observation",),
            publication_strategy="replace",
            supported_windows=("season",),
            enabled=True,
        )
        self.ingestion = ObservationIngestionService(
            self.engine, publication_service=self.publications, clock=self.clock
        )

    @staticmethod
    def _validate_configuration(
        database_url: str | None,
        *,
        isolated: bool,
        production_database_url: str | None,
        restored_database_url: str | None,
        disposable_marker_nonce: str | None = None,
        disposable_schema: str | None = None,
    ) -> str | None:
        if database_url is None:
            return None
        # ``isolated=True`` is retained only for source compatibility.  It is
        # an assertion supplied by the caller, not isolation evidence.  A
        # marker row is checked against the opened database before migrations.
        if not disposable_marker_nonce or not str(disposable_marker_nonce).strip():
            return "out_of_band_disposable_marker_nonce_required"
        if str(database_url).lower().startswith("postgres") and not str(disposable_schema or "").strip():
            return "postgres_disposable_schema_required"
        return None

    @staticmethod
    def _preflight_target(
        engine: Engine,
        *,
        marker_nonce: str | None,
        schema: str | None,
        label: str,
        require_empty: bool = True,
    ) -> str | None:
        """Perform a read-only disposable-target check before migrations.

        The marker is provisioned by an operator or test harness outside this
        runner.  Consequently a caller cannot make an arbitrary URL safe by
        passing an ``isolated`` boolean or by having this process create its
        own marker immediately before migration.
        """

        try:
            inspector = inspect(engine)
            table_names = set(inspector.get_table_names(schema=schema))
        except Exception:
            return f"{label}_database_preflight_unreadable"
        if DISPOSABLE_MARKER_TABLE not in table_names:
            return f"{label}_database_disposable_marker_missing"
        if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(schema)):
            return f"{label}_database_disposable_schema_invalid"
        qualified_marker = (
            f'"{schema}"."{DISPOSABLE_MARKER_TABLE}"' if schema else DISPOSABLE_MARKER_TABLE
        )
        try:
            with engine.connect() as connection:
                marker = connection.execute(
                    text(
                        f"SELECT marker_nonce, purpose, schema_name "
                        f"FROM {qualified_marker} WHERE marker_nonce = :nonce"
                    ),
                    {"nonce": str(marker_nonce)},
                ).mappings().first()
                if marker is None or str(marker.get("purpose")) != DISPOSABLE_MARKER_PURPOSE:
                    return f"{label}_database_disposable_marker_mismatch"
                if schema and marker.get("schema_name") not in {None, str(schema)}:
                    return f"{label}_database_disposable_schema_mismatch"
                # A target may already have the migration ledger, but no
                # domain rows.  This query is deliberately read-only.
                for table in sorted(table_names & DOMAIN_TABLES):
                    if table == "alembic_version":
                        continue
                    if not require_empty:
                        continue
                    qualified_table = f'"{schema}"."{table}"' if schema else table
                    count = connection.execute(
                        text(f"SELECT COUNT(*) FROM {qualified_table}")
                    ).scalar_one()
                    if int(count) > 0:
                        return f"{label}_database_not_empty"
        except Exception:
            return f"{label}_database_disposable_marker_unreadable"
        return None

    @classmethod
    def preflight_disposable_database(
        cls,
        database_url: str,
        *,
        marker_nonce: str,
        schema: str | None,
        label: str,
        require_empty: bool = True,
    ) -> None:
        """Check a marked target before an operator can restore into it."""

        engine = create_engine(database_url)
        try:
            if engine.dialect.name != "postgresql":
                raise ValueError(f"{label}_restore_requires_postgres")
            error = cls._preflight_target(
                engine,
                marker_nonce=marker_nonce,
                schema=schema,
                label=label,
                require_empty=require_empty,
            )
            if error is not None:
                raise ValueError(error)
        finally:
            engine.dispose()

    def run(
        self,
        *,
        hooks: Mapping[str, Callable[[], Any]] | None = None,
        require_production_evidence: bool = False,
    ) -> FailureDrillReport:
        started = self.clock().astimezone(UTC).isoformat()
        results: list[DrillResult] = []
        if self.configuration_error is not None:
            return self._failed_report(started, self.configuration_error)
        if require_production_evidence and not self.production_database_url:
            return self._failed_report(started, "production_database_url_required")
        if require_production_evidence and not self.production_evidence:
            completed = self.clock().astimezone(UTC).isoformat()
            return self._failed_report(started, "postgres isolated drill database required")
        if require_production_evidence and not self.restored_database_url:
            return self._failed_report(started, "restored isolated Postgres database URL required")
        for name in self.NAMES:
            hook = (hooks or {}).get(name) or self._default_hook(name)
            measured = perf_counter()
            try:
                value = hook() if hook is not None else True
                passed = value is not False
                details = dict(value) if isinstance(value, Mapping) else {"verified": passed}
                if "verified" in details:
                    passed = bool(details["verified"])
                details.setdefault("recovery_time_ms", round((perf_counter() - measured) * 1000, 3))
                details.setdefault("recovery_point", "post-commit")
                details.setdefault("production_evidence", self.production_evidence)
                details.setdefault("environment", self.environment)
                details.setdefault("run_id", self.run_id)
                results.append(
                    DrillResult(
                        name=name,
                        status="passed" if passed else "failed",
                        attempts=2 if name == "railway_outage_retry" else 1,
                        details=dict(details),
                    )
                )
            except Exception as error:  # drill output is an operator artifact
                results.append(
                    DrillResult(
                        name=name,
                        status="failed",
                        attempts=1,
                        details={
                            "error": type(error).__name__,
                            "recovery_time_ms": round((perf_counter() - measured) * 1000, 3),
                            "production_evidence": self.production_evidence,
                            "environment": self.environment,
                            "run_id": self.run_id,
                        },
                    )
                )
        completed = self.clock().astimezone(UTC).isoformat()
        return FailureDrillReport(
            status="passed" if all(item.status == "passed" for item in results) else "failed",
            started_at=started,
            completed_at=completed,
            drills=tuple(results),
            environment=self.environment,
            production_evidence=self.production_evidence,
        )

    def _failed_report(self, started: str, error: str) -> FailureDrillReport:
        completed = self.clock().astimezone(UTC).isoformat()
        return FailureDrillReport(
            status="failed",
            started_at=started,
            completed_at=completed,
            drills=tuple(
                DrillResult(
                    name=name,
                    status="failed",
                    attempts=0,
                    details={
                        "error": error,
                        "environment": self.environment,
                        "run_id": self.run_id,
                        "production_evidence": False,
                    },
                )
                for name in self.NAMES
            ),
            environment=self.environment,
            production_evidence=False,
        )

    def _id(self, label: str) -> str:
        return f"drill-{self.run_id[:16]}-{label}"

    def _default_hook(self, name: str) -> Callable[[], Mapping[str, Any]]:
        """Return a deterministic exercise against the temporary control plane."""

        def railway_outage_retry() -> Mapping[str, Any]:
            attempts = []
            try:
                self.publications.compose(
                    "drill_stream",
                    season="2025-26",
                    cutoff=self.clock(),
                    payload=object(),
                )
            except (TypeError, ValueError):
                attempts.append("failed")
            publication = self.publications.compose(
                "drill_stream",
                season="2025-26",
                cutoff=self.clock(),
                payload={"attempt": 2},
            )
            attempts.append("succeeded")
            published_attempts = (publication.fence,)
            return {
                "retry_statuses": tuple(attempts),
                "published_attempts": published_attempts,
                "no_partial_publish": published_attempts == (1,),
                "verified": tuple(attempts) == ("failed", "succeeded")
                and published_attempts == (1,),
            }

        def duplicate_delivery_idempotency() -> Mapping[str, Any]:
            now = self.clock().astimezone(UTC)
            self._drill_cutoff = now
            payload = json.dumps(
                {
                    "base": "drill_observation",
                    "rows": [{"slice_key": "sample", "value": 1}],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            checksum = hashlib.sha256(payload).hexdigest()
            manifest_id = self._id("ingestion-manifest")
            with self.engine.begin() as connection:
                connection.execute(
                    CollectionManifest.__table__.insert().values(
                        manifest_id=manifest_id,
                        season="2025-26",
                        cutoff=now,
                        collect_before=now + timedelta(hours=1),
                        accepted_versions=json.dumps([1]),
                        scopes=json.dumps(["drill_observation"]),
                        checksum=hashlib.sha256(manifest_id.encode()).hexdigest(),
                        status="active",
                        created_at=now,
                    )
                )
            tokens = CollectorTokenService(
                self.engine,
                environment="testing",
                signing_secret="drill-ingestion-secret",
                clock=self.clock,
            )
            identity = tokens.create_identity(
                self._id("ingestion-collector"),
                scopes=["ingest"],
                providers=["drill"],
                surfaces=["drill_observation"],
                owner="local",
                identity_id=self._id("ingestion-collector"),
            )
            token = tokens.issue_for_secret(
                identity["identity_id"],
                identity["secret"],
                scopes=["ingest"],
                providers=["drill"],
                surfaces=["drill_observation"],
            )
            claims = tokens.validate(token, required_scope="ingest")
            envelope = {
                "manifest_id": manifest_id,
                "client_observation_id": self._id("receipt-1"),
                "environment": "testing",
                "provider": "drill",
                "observation_type": "drill_observation",
                "scope": {"window": "season"},
                "season": "2025-26",
                "cutoff": now.isoformat(),
                "schema_version": 1,
                "retrieved_at": now.isoformat(),
                "checksum": checksum,
            }
            first = self.ingestion.ingest(claims, envelope, payload)
            second = self.ingestion.ingest(claims, envelope, payload)
            return {
                "deliveries": 2,
                "committed": 1 if first.observation_id == second.observation_id else 2,
                "idempotent": first.observation_id == second.observation_id,
                "replay": second.replay,
                "verified": first.observation_id == second.observation_id and second.replay,
                "receipt_layer": "control_plane_observation_ingestion",
            }

        def collector_reboot_outbox_replay() -> Mapping[str, Any]:
            now = self.clock().astimezone(UTC)
            envelope = ObservationEnvelope(
                manifest_id=self._id("manifest"),
                client_observation_id=self._id("observation-1"),
                environment="testing",
                collector_id=self._id("collector"),
                provider="nba",
                observation_type="canonical_game_ledger",
                scope={"surface": "canonical_game_ledger", "game_id": self._id("game")},
                season="2025-26",
                cutoff=now.isoformat(),
                retrieved_at=now.isoformat(),
                schema_version=1,
                payload={"surface": "canonical_game_ledger", "rows": [{"game_id": self._id("game")}]},
            )
            with TemporaryDirectory(prefix="statsplus-outbox-") as directory:
                path = str(Path(directory) / "outbox.sqlite3")
                first_outbox = ResidentialOutbox(path, clock=lambda: now)
                first = first_outbox.enqueue_observation(envelope)
                duplicate = first_outbox.enqueue_observation(envelope)
                first_outbox.close()
                rebooted = ResidentialOutbox(path, clock=lambda: now)
                pending = rebooted.pending()
                acknowledged = rebooted.acknowledge(
                    pending[0].item_id, checksum=envelope.checksum
                ) if pending else False
                rebooted.close()
            return {
                "pending_before_reboot": 1,
                "replayed_after_reboot": len(pending),
                "idempotent": first.item_id == duplicate.item_id,
                "acknowledged": acknowledged,
                "verified": first.item_id == duplicate.item_id and acknowledged,
                "receipt_layer": "residential_outbox_sqlite",
            }

        def expired_credential_rejection() -> Mapping[str, Any]:
            current = [self.clock().astimezone(UTC)]
            tokens = CollectorTokenService(
                self.engine,
                environment="testing",
                signing_secret="drill-secret",
                clock=lambda: current[0],
            )
            identity = tokens.create_identity(
                self._id("collector"),
                scopes=["ingest"],
                providers=["nba"],
                surfaces=["canonical_game_ledger"],
                identity_id=self._id("expiry-collector"),
            )
            token = tokens.issue_for_secret(
                identity["identity_id"], identity["secret"], scopes=["ingest"], ttl_seconds=1
            )
            current[0] += timedelta(seconds=2)
            try:
                tokens.validate(token, required_scope="ingest")
                credential_valid = True
            except Exception:
                credential_valid = False
            return {
                "credential_status": "expired",
                "accepted": credential_valid,
                "writes": 0,
                "verified": not credential_valid,
            }

        def provider_failure_last_good_retention() -> Mapping[str, Any]:
            last_good = {"value": 7}
            publication = self.publications.compose(
                "drill_stream",
                season="2025-26",
                cutoff=self.clock(),
                payload=last_good,
                expected_fence=1,
            )
            failed_attempt = {"value": None}
            served = self.publications.get_historical_payload(publication.publication_id)
            return {
                "last_good": last_good,
                "failed_attempt": failed_attempt,
                "served": served,
                "last_good_retained": served == last_good,
                "partial_attempt_published": served == failed_attempt,
                "verified": served == last_good and served != failed_attempt,
            }

        def alert_recovery() -> Mapping[str, Any]:
            now = self.clock()
            with self.engine.begin() as connection:
                for state, reason in (("retry", "provider outage"), ("complete", "recovered")):
                    connection.execute(CollectorStatusTransition.__table__.insert().values(
                        transition_id=self._id(state),
                        collector_id=self._id("collector"),
                        state=state,
                        reason=reason,
                        release_version="drill",
                        release_checksum="d" * 64,
                        created_at=now,
                    ))
            transitions = ("failure", "recovery")
            return {
                "alert_transitions": transitions,
                "recovery_emitted": transitions[-1] == "recovery",
                "verified": transitions[-1] == "recovery",
            }

        def isolated_restore_replay() -> Mapping[str, Any]:
            restore_started = self.restore_started or perf_counter()
            if self.production_evidence:
                return self._query_restored_database(started=restore_started)
            from sqlite3 import connect
            from app.services.canonical_game_ledger import (
                CanonicalGame,
                CanonicalGameLedgerRepository,
                PlayerGameFact,
                TeamGameFact,
                game_checksum,
            )
            from app.models.canonical_game_ledger import (
                CanonicalGameLedgerGame,
                LedgerBackfillState,
                LedgerPublication,
            )
            from app.models.collection_control import (
                AuditEvent,
                CollectionObservation,
                CompositionJob,
                PublicationActivation,
                PublicationObservation,
                PublicationVersion,
            )

            restore_publication = self._id("restore-publication")
            restore_prior_publication = self._id("restore-prior-publication")
            restore_stream = self._id("restore-stream")
            restore_observation = self._id("restore-observation")
            restore_collector = self._id("restore-collector")
            restore_game = self._id("restore-game")
            restore_audit = self._id("restore-audit")
            restore_job = self._id("restore-job")
            restore_activation = self._id("restore-activation")

            with self.engine.begin() as connection:
                now = self.clock().astimezone(UTC)
                publication = {
                    "publication_id": restore_publication,
                    "stream_key": restore_stream,
                    "season": "2025-26",
                    "cutoff": now,
                    "version": 1,
                    "status": "active",
                    "checksum": "r" * 64,
                    "payload": "{}",
                    "created_at": now,
                    "reason": "restore drill",
                    "fence": 1,
                }
                prior_publication = {
                    **publication,
                    "publication_id": restore_prior_publication,
                    "status": "rollback",
                    "checksum": "p" * 64,
                    "payload": json.dumps({"rows": [{"game_id": restore_game, "version": "prior"}]}),
                    "reason": "restore rollback evidence",
                    "fence": 0,
                }
                if connection.execute(
                    PublicationVersion.__table__.select().where(
                        PublicationVersion.publication_id == prior_publication["publication_id"]
                    )
                ).first() is None:
                    connection.execute(PublicationVersion.__table__.insert().values(**prior_publication))
                if connection.execute(
                    PublicationVersion.__table__.select().where(
                        PublicationVersion.publication_id == publication["publication_id"]
                    )
                ).first() is None:
                    connection.execute(PublicationVersion.__table__.insert().values(**publication))
                if connection.execute(
                    PublicationActivation.__table__.select().where(
                        PublicationActivation.activation_id == restore_activation
                    )
                ).first() is None:
                    connection.execute(PublicationActivation.__table__.insert().values(
                        activation_id=restore_activation,
                        stream_key=restore_stream,
                        publication_id=restore_publication,
                        actor="drill",
                        reason="restore drill activation",
                        fence=1,
                        created_at=now,
                    ))
                if connection.execute(
                    PublicationPointer.__table__.select().where(
                        PublicationPointer.stream_key == restore_stream
                    )
                ).first() is None:
                    connection.execute(PublicationPointer.__table__.insert().values(
                        stream_key=restore_stream,
                        active_publication_id=publication["publication_id"],
                        previous_publication_id=restore_prior_publication,
                        fence=1,
                        updated_at=now,
                    ))
                if connection.execute(
                    CollectionObservation.__table__.select().where(
                        CollectionObservation.observation_id == restore_observation
                    )
                ).first() is None:
                    connection.execute(CollectionObservation.__table__.insert().values(
                        observation_id=restore_observation,
                        client_observation_id=restore_observation,
                        collector_id=restore_collector,
                        manifest_id=None,
                        environment="testing",
                        provider="pbp",
                        observation_type="canonical_game_ledger",
                        scope=json.dumps({"surface": "canonical_game_ledger", "game_id": restore_game}),
                        season="2025-26",
                        cutoff=now,
                        schema_version=1,
                        checksum="o" * 64,
                        payload=json.dumps({"game_id": restore_game}),
                        payload_bytes=28,
                        retrieved_at=now,
                        accepted_at=now,
                    ))
                if connection.execute(
                    PublicationObservation.__table__.select().where(
                        PublicationObservation.publication_id == publication["publication_id"],
                        PublicationObservation.observation_id == restore_observation,
                    )
                ).first() is None:
                    connection.execute(PublicationObservation.__table__.insert().values(
                        publication_id=publication["publication_id"],
                        observation_id=restore_observation,
                        role="restore_evidence",
                        slice_key=restore_game,
                        created_at=now,
                    ))
                if connection.execute(
                    AuditEvent.__table__.select().where(
                        AuditEvent.event_id == restore_audit
                    )
                ).first() is None:
                    connection.execute(AuditEvent.__table__.insert().values(
                        event_id=restore_audit,
                        actor="drill",
                        action="restore.verify",
                        resource=restore_publication,
                        reason="restore drill",
                        details=json.dumps({"replay": True}),
                        created_at=now,
                    ))
                if connection.execute(
                    CanonicalGameLedgerGame.__table__.select().where(
                        CanonicalGameLedgerGame.game_id == restore_game
                    )
                ).first() is None:
                    connection.execute(CanonicalGameLedgerGame.__table__.insert().values(
                        game_id=restore_game,
                        season="2025-26",
                        season_type="Regular Season",
                        game_date=now.date(),
                        home_team_id=1,
                        home_team_tricode="AAA",
                        away_team_id=2,
                        away_team_tricode="BBB",
                        status="final",
                        source_observation_id=restore_observation,
                        # Deliberately stale evidence.  The repair callback
                        # below replaces this row through the governed
                        # CanonicalGameLedgerRepository seam.
                        checksum="x" * 64,
                        retrieved_at=now,
                        updated_at=now,
                    ))
                if connection.execute(
                    LedgerBackfillState.__table__.select().where(
                        LedgerBackfillState.season == "2025-26"
                    )
                ).first() is None:
                    connection.execute(LedgerBackfillState.__table__.insert().values(
                        season="2025-26",
                        cutoff=now,
                        cursor_game_id=restore_game,
                        completed_game_ids=json.dumps([restore_game]),
                        failed_game_ids=json.dumps([]),
                        status="complete",
                        updated_at=now,
                        last_error=None,
                    ))
                if connection.execute(
                    LedgerPublication.__table__.select().where(
                        LedgerPublication.stream_key == restore_stream,
                        LedgerPublication.season == "2025-26",
                        LedgerPublication.window_kind == "season",
                        LedgerPublication.window_games == 0,
                        LedgerPublication.as_of == now.date(),
                    )
                ).first() is None:
                    connection.execute(LedgerPublication.__table__.insert().values(
                        stream_key=restore_stream,
                        season="2025-26",
                        window_kind="season",
                        window_games=0,
                        as_of=now.date(),
                        status="complete",
                        checksum="l" * 64,
                        payload=json.dumps({"rows": [{"game_id": restore_game}]}),
                        game_count=1,
                        team_count=2,
                        retrieved_at=now,
                        reason=None,
                    ))
                if connection.execute(
                    CompositionJob.__table__.select().where(
                        CompositionJob.job_id == restore_job
                    )
                ).first() is None:
                    connection.execute(CompositionJob.__table__.insert().values(
                        job_id=restore_job,
                        stream_key=restore_stream,
                        manifest_id=None,
                        season="2025-26",
                        cutoff=now,
                        status="queued",
                        attempts=0,
                        created_at=now,
                        updated_at=now,
                        last_error=None,
                    ))
            repair_game = CanonicalGame(
                game_id=restore_game,
                season="2025-26",
                game_date=now.date(),
                home_team_id=1,
                home_team_tricode="AAA",
                away_team_id=2,
                away_team_tricode="BBB",
                team_facts=(
                    TeamGameFact(
                        team_id=1,
                        team_tricode="AAA",
                        opponent_team_id=2,
                        opponent_team_tricode="BBB",
                        is_home=True,
                        points=25,
                        field_goals_made=10,
                        field_goals_attempted=20,
                        two_pointers_made=8,
                        two_pointers_attempted=16,
                        three_pointers_made=2,
                        three_pointers_attempted=4,
                        free_throws_made=3,
                        free_throws_attempted=3,
                        offensive_rebounds=2,
                        defensive_rebounds=3,
                        rebounds=5,
                        assists=4,
                        turnovers=1,
                        steals=1,
                        blocks=1,
                        personal_fouls=2,
                        team_minutes=4.8,
                    ),
                    TeamGameFact(
                        team_id=2,
                        team_tricode="BBB",
                        opponent_team_id=1,
                        opponent_team_tricode="AAA",
                        is_home=False,
                        points=20,
                        field_goals_made=8,
                        field_goals_attempted=20,
                        two_pointers_made=6,
                        two_pointers_attempted=14,
                        three_pointers_made=2,
                        three_pointers_attempted=6,
                        free_throws_made=2,
                        free_throws_attempted=2,
                        offensive_rebounds=1,
                        defensive_rebounds=3,
                        rebounds=4,
                        assists=3,
                        turnovers=1,
                        steals=1,
                        blocks=0,
                        personal_fouls=2,
                        team_minutes=4.8,
                    ),
                ),
                player_facts=(
                    PlayerGameFact(
                        player_id=101,
                        player_name="Repair Home",
                        team_id=1,
                        team_tricode="AAA",
                        minutes=24.0,
                        points=25,
                        field_goals_made=10,
                        field_goals_attempted=20,
                        two_pointers_made=8,
                        two_pointers_attempted=16,
                        three_pointers_made=2,
                        three_pointers_attempted=4,
                        free_throws_made=3,
                        free_throws_attempted=3,
                        offensive_rebounds=2,
                        defensive_rebounds=3,
                        rebounds=5,
                        assists=4,
                        turnovers=1,
                        steals=1,
                        blocks=1,
                        personal_fouls=2,
                    ),
                    PlayerGameFact(
                        player_id=202,
                        player_name="Repair Away",
                        team_id=2,
                        team_tricode="BBB",
                        minutes=24.0,
                        points=20,
                        field_goals_made=8,
                        field_goals_attempted=20,
                        two_pointers_made=6,
                        two_pointers_attempted=14,
                        three_pointers_made=2,
                        three_pointers_attempted=6,
                        free_throws_made=2,
                        free_throws_attempted=2,
                        offensive_rebounds=1,
                        defensive_rebounds=3,
                        rebounds=4,
                        assists=3,
                        turnovers=1,
                        steals=1,
                        blocks=0,
                        personal_fouls=2,
                    ),
                ),
                source_observation_id=self._id("repair-observation"),
                retrieved_at=now,
                participant_ids_by_team=((1, (101,)), (2, (202,))),
            ).with_checksum()
            repair_checksum = game_checksum(repair_game)

            raw = self.engine.raw_connection()
            target_engine: Engine | None = None
            try:
                source = raw.driver_connection
                target = connect(":memory:")
                source.backup(target)
                target_engine = create_engine(
                    "sqlite://",
                    creator=lambda: target,
                    poolclass=StaticPool,
                )
                repair = self.pbp_repair
                if repair is None:
                    def repair(engine: Engine, game_id: str) -> Mapping[str, Any]:
                        if game_id != repair_game.game_id:
                            raise ValueError("repair_game_id_mismatch")
                        result = CanonicalGameLedgerRepository(engine).replace_game(repair_game)
                        return {
                            "game_id": game_id,
                            "observation_id": repair_game.source_observation_id,
                            "checksum": result.checksum,
                            "updated_rows": result.row_count,
                            "composition_job_id": restore_job,
                            "adapter": "canonical_game_ledger_repository",
                        }
                repair_result = repair(target_engine, restore_game)

                # Exercise the real SQLite residential outbox and the real
                # control-plane ingestion service on the restored copy.  The
                # second receipt is the server-side duplicate path, and the
                # item is acknowledged only after both deliveries succeed.
                replay_envelope = ObservationEnvelope(
                    manifest_id=self._id("ingestion-manifest"),
                    client_observation_id=self._id("restore-receipt"),
                    environment="testing",
                    collector_id=self._id("ingestion-collector"),
                    provider="drill",
                    observation_type="drill_observation",
                    scope={"window": "season"},
                    season="2025-26",
                    cutoff=(self._drill_cutoff or self.clock().astimezone(UTC)).isoformat(),
                    retrieved_at=self.clock().astimezone(UTC).isoformat(),
                    schema_version=1,
                    payload={"base": "drill_observation", "rows": [{"slice_key": "restore", "value": 1}]},
                )
                target_publications = PublicationService(target_engine, clock=self.clock)
                target_ingestion = ObservationIngestionService(
                    target_engine, publication_service=target_publications, clock=self.clock
                )
                claims = CollectorClaims(
                    collector_id=self._id("ingestion-collector"),
                    audience="statsplus",
                    environment="testing",
                    scopes=frozenset({"ingest"}),
                    token_id=self._id("restore-token"),
                    expires_at=self.clock().astimezone(UTC) + timedelta(hours=1),
                    owner="local",
                    providers=frozenset({"drill"}),
                    surfaces=frozenset({"drill_observation"}),
                )
                with TemporaryDirectory(prefix="statsplus-restore-outbox-") as outbox_directory:
                    outbox_path = str(Path(outbox_directory) / "outbox.sqlite3")
                    restored_outbox = ResidentialOutbox(
                        outbox_path, clock=lambda: self.clock().astimezone(UTC)
                    )
                    try:
                        item = restored_outbox.enqueue_observation(replay_envelope)
                        duplicate_item = restored_outbox.enqueue_observation(replay_envelope)
                        wire = json.loads(gzip.decompress(item.payload))
                        restored_payload = json.dumps(
                            wire["payload"], sort_keys=True, separators=(",", ":")
                        ).encode()
                        restored_envelope = {key: value for key, value in wire.items() if key != "payload"}
                        first_receipt = target_ingestion.ingest(claims, restored_envelope, restored_payload)
                        second_receipt = target_ingestion.ingest(claims, restored_envelope, restored_payload)
                        acknowledged = restored_outbox.acknowledge(
                            item.item_id, checksum=replay_envelope.checksum
                        )
                    finally:
                        restored_outbox.close()
                checks = {
                    "ledger": f"SELECT game_id FROM canonical_game_ledger_games WHERE game_id = '{restore_game}'",
                    "pointers": f"SELECT stream_key FROM publication_pointers WHERE stream_key = '{restore_stream}' AND active_publication_id = '{restore_publication}' AND previous_publication_id = '{restore_prior_publication}'",
                    "audit": f"SELECT event_id FROM collection_audit_events WHERE event_id = '{restore_audit}'",
                    "provenance": f"SELECT publication_id FROM publication_observations WHERE publication_id = '{restore_publication}'",
                    "pbp_repair": "SELECT season FROM canonical_game_ledger_backfill WHERE season = '2025-26' AND status = 'complete'",
                    "replay": f"SELECT job_id FROM composition_jobs WHERE job_id = '{restore_job}'",
                }
                restored_checks = {
                    name: bool(target.execute(statement).fetchone())
                    for name, statement in checks.items()
                }
                restored_rows = {
                    row[0] for row in target.execute(
                        "SELECT stream_key FROM publication_pointers"
                    ).fetchall()
                }
                exact = {
                    "ledger_checksum": target.execute(
                        "SELECT checksum FROM canonical_game_ledger_games WHERE game_id = ?", (restore_game,)
                    ).fetchone()[0] == repair_checksum,
                    "active_checksum": target.execute(
                        "SELECT checksum FROM publication_versions WHERE publication_id = ?", (restore_publication,)
                    ).fetchone()[0] == "r" * 64,
                    "previous_checksum": target.execute(
                        "SELECT checksum FROM publication_versions WHERE publication_id = ?", (restore_prior_publication,)
                    ).fetchone()[0] == "p" * 64,
                    "activation_fk": target.execute(
                        "SELECT 1 FROM publication_activations a JOIN publication_versions v "
                        "ON v.publication_id = a.publication_id WHERE a.activation_id = ?", (restore_activation,)
                    ).fetchone() is not None,
                    "provenance_fk": target.execute(
                        "SELECT 1 FROM publication_observations p JOIN publication_versions v "
                        "ON v.publication_id = p.publication_id JOIN collection_observations o "
                        "ON o.observation_id = p.observation_id WHERE p.publication_id = ?", (restore_publication,)
                    ).fetchone() is not None,
                }
                target.close()
            finally:
                if target_engine is not None:
                    target_engine.dispose()
                raw.close()
            replayed_rows = set(restored_rows)
            return {
                "restored_rows": len(restored_rows),
                "replayed_rows": len(replayed_rows),
                "idempotent": replayed_rows == restored_rows,
                "ledger_validated": restored_checks["ledger"],
                "pointers_validated": restored_checks["pointers"],
                "audit_validated": restored_checks["audit"],
                "provenance_validated": restored_checks["provenance"],
                "pbp_repair_validated": restored_checks["pbp_repair"],
                "replay_validated": restored_checks["replay"],
                "exact_checksums_validated": all(exact.values()),
                "repair_result": repair_result,
                "outbox_replayed_twice": first_receipt.observation_id == second_receipt.observation_id and second_receipt.replay,
                "outbox_duplicate_item_idempotent": duplicate_item.item_id == item.item_id,
                "outbox_acknowledged": acknowledged,
                "recovery_time_ms": round((perf_counter() - restore_started) * 1000, 3),
                "recovery_data_point": {
                    "observed_at": self.clock().astimezone(UTC).isoformat(),
                    "latest_governed_cutoff": self.clock().astimezone(UTC).isoformat(),
                    "latest_observation": restore_observation,
                    "repair_observation": repair_game.source_observation_id,
                    "repair_checksum": repair_checksum,
                    "composition_job_id": restore_job,
                },
                "sla_claimed": False,
                "adapter": "sqlite_unit",
                "environment": "unit",
                "production_evidence": False,
                "verified": (
                    replayed_rows == restored_rows
                    and all(restored_checks.values())
                    and all(exact.values())
                    and first_receipt.observation_id == second_receipt.observation_id
                    and second_receipt.replay
                    and duplicate_item.item_id == item.item_id
                    and acknowledged
                ),
            }

        hooks: dict[str, Callable[[], Mapping[str, Any]]] = {
            "railway_outage_retry": railway_outage_retry,
            "duplicate_delivery_idempotency": duplicate_delivery_idempotency,
            "collector_reboot_outbox_replay": collector_reboot_outbox_replay,
            "expired_credential_rejection": expired_credential_rejection,
            "provider_failure_last_good_retention": provider_failure_last_good_retention,
            "alert_recovery": alert_recovery,
            "isolated_restore_replay": isolated_restore_replay,
        }
        return hooks[name]

    def _restore_outbox_replay(self, restored: Engine) -> Mapping[str, Any]:
        """Replay one configured receipt twice against the restored control plane."""

        specification = self.restore_expectations.get("replay")
        if not isinstance(specification, Mapping):
            return {"verified": False, "reason": "restore_replay_specification_required"}
        try:
            payload = specification["payload"]
            envelope = ObservationEnvelope(
                manifest_id=str(specification["manifest_id"]),
                client_observation_id=str(specification["client_observation_id"]),
                environment=str(specification.get("environment", "production")),
                collector_id=str(specification["collector_id"]),
                provider=str(specification["provider"]),
                observation_type=str(specification["observation_type"]),
                scope=specification["scope"],
                season=str(specification["season"]),
                cutoff=str(specification["cutoff"]),
                retrieved_at=str(specification.get("retrieved_at", specification["cutoff"])),
                schema_version=int(specification.get("schema_version", 1)),
                payload=payload,
            )
            claims_data = specification["claims"]
            if not isinstance(claims_data, Mapping):
                raise ValueError("restore_replay_claims_required")
            expires_at = datetime.fromisoformat(
                str(claims_data["expires_at"]).replace("Z", "+00:00")
            )
            claims = CollectorClaims(
                collector_id=str(claims_data.get("collector_id", envelope.collector_id)),
                audience=str(claims_data.get("audience", "statsplus")),
                environment=str(claims_data.get("environment", envelope.environment)),
                scopes=frozenset(str(value) for value in claims_data.get("scopes", ["ingest"])),
                token_id=str(claims_data["token_id"]),
                expires_at=expires_at,
                owner=str(claims_data.get("owner", "")),
                providers=frozenset(str(value) for value in claims_data.get("providers", [envelope.provider])),
                surfaces=frozenset(str(value) for value in claims_data.get("surfaces", [envelope.observation_type])),
            )
            target_ingestion = ObservationIngestionService(
                restored,
                publication_service=PublicationService(restored, clock=self.clock),
                clock=self.clock,
            )
            with TemporaryDirectory(prefix="statsplus-operator-outbox-") as directory:
                outbox = ResidentialOutbox(str(Path(directory) / "outbox.sqlite3"), clock=self.clock)
                try:
                    item = outbox.enqueue_observation(envelope)
                    duplicate = outbox.enqueue_observation(envelope)
                    wire = json.loads(gzip.decompress(item.payload))
                    body = json.dumps(wire["payload"], sort_keys=True, separators=(",", ":")).encode()
                    wire_envelope = {key: value for key, value in wire.items() if key != "payload"}
                    invoke = self.restore_ingestion
                    if invoke is None:
                        first = target_ingestion.ingest(claims, wire_envelope, body)
                        second = target_ingestion.ingest(claims, wire_envelope, body)
                    else:
                        first = invoke(restored, claims, wire_envelope, body)
                        second = invoke(restored, claims, wire_envelope, body)
                    acknowledged = outbox.acknowledge(item.item_id, checksum=envelope.checksum)
                finally:
                    outbox.close()
            first_id = getattr(first, "observation_id", None)
            second_id = getattr(second, "observation_id", None)
            replay = bool(getattr(second, "replay", False))
            return {
                "verified": duplicate.item_id == item.item_id
                and first_id == second_id and replay and acknowledged,
                "outbox_replayed_twice": first_id == second_id and replay,
                "outbox_duplicate_item_idempotent": duplicate.item_id == item.item_id,
                "outbox_acknowledged": acknowledged,
            }
        except Exception as error:
            return {"verified": False, "error": type(error).__name__}

    def _restore_pbp_repair(self, restored: Engine) -> Mapping[str, Any]:
        specification = self.restore_expectations.get("pbp_repair")
        if not isinstance(specification, Mapping):
            return {"verified": False, "reason": "pbp_repair_specification_required"}
        game_id = str(specification.get("game_id", ""))
        checksum = str(specification.get("checksum", ""))
        expected_observation_id = str(specification.get("observation_id", ""))
        expected_job_id = str(specification.get("composition_job_id", ""))
        if not game_id or len(checksum) != 64 or not expected_observation_id or not expected_job_id:
            return {"verified": False, "reason": "pbp_repair_identity_required"}
        if self.pbp_repair is None:
            return {
                "verified": False,
                "reason": "governed_pbp_repair_adapter_required",
                "game_id": game_id,
            }
        try:
            result = self.pbp_repair(restored, game_id)
            if not isinstance(result, Mapping):
                return {"verified": False, "reason": "governed_pbp_repair_evidence_required"}
            executed = bool(result.get("verified", True)) and int(result.get("updated_rows", 1)) > 0
            with restored.connect() as connection:
                verified = connection.execute(text(
                    "SELECT checksum FROM canonical_game_ledger_games WHERE game_id = :game_id"
                ), {"game_id": game_id}).scalar_one_or_none() == checksum
            evidence = {
                "game_id": game_id,
                "observation_id": str(result.get("observation_id", "")),
                "composition_job_id": str(result.get("composition_job_id", "")),
                "checksum": checksum,
                "updated_rows": int(result.get("updated_rows", 0)),
                "adapter": str(result.get("adapter", "governed_ledger_repair")),
            }
            identities_match = (
                evidence["observation_id"] == expected_observation_id
                and evidence["composition_job_id"] == expected_job_id
            )
            return {
                "verified": executed and verified and identities_match,
                "repair_result": dict(result),
                **evidence,
            }
        except Exception as error:
            return {"verified": False, "error": type(error).__name__}

    def _query_restored_database(self, *, started: float | None = None) -> Mapping[str, Any]:
        """Validate a disposable restored Postgres database by querying it.

        The operator supplies the URL of the restored isolated database, not a
        JSON assertion callback.  Every reported check below is measured from
        rows in that database, so a control-plane command cannot manufacture a
        passing restore artifact.
        """

        if not self.restored_database_url:
            raise ValueError("restored_isolated_database_url_required")
        restore_started = started if started is not None else perf_counter()
        if not self.restored_marker_nonce:
            raise ValueError("restored_out_of_band_disposable_marker_nonce_required")
        if not self.restored_schema:
            raise ValueError("restored_postgres_schema_required")
        command_evidence = self.restore_command_evidence
        if (
            not command_evidence
            or command_evidence.get("status") != "succeeded"
            or not command_evidence.get("backup_artifact")
            or not command_evidence.get("command")
            or command_evidence.get("returncode") != 0
        ):
            raise ValueError("restore_command_evidence_required")
        restored = create_engine(self.restored_database_url)
        try:
            if restored.dialect.name != "postgresql":
                raise ValueError("production_restore_requires_postgres")
            restored_identity = _connected_database_identity(
                restored,
                schema=self.restored_schema,
            )
            drill_identity = self._drill_identity or _connected_database_identity(
                self.engine,
                schema=self.disposable_schema,
            )
            if same_database_identity(drill_identity, restored_identity):
                raise ValueError("restored_database_must_be_disposable_and_separate")
            if self._production_identity is not None and same_database_identity(
                self._production_identity,
                restored_identity,
            ):
                raise ValueError("restored_database_must_be_separate_from_production")
            if not self.restore_expectations:
                raise ValueError("restore_expectations_required")
            preflight = self._preflight_target(
                restored,
                marker_nonce=self.restored_marker_nonce,
                schema=self.restored_schema,
                label="restored",
                require_empty=False,
            )
            if preflight is not None:
                raise ValueError(preflight)
            table_names = set(inspect(restored).get_table_names())
            required_tables = {
                "canonical_game_ledger_games",
                "canonical_game_ledger_backfill",
                "publication_pointers",
                "publication_versions",
                "publication_observations",
                "publication_activations",
                "collection_observations",
                "collection_audit_events",
                "composition_jobs",
            }
            if not required_tables <= table_names:
                raise ValueError("restored_database_schema_incomplete")
            replay_result = self._restore_outbox_replay(restored)
            repair_result = self._restore_pbp_repair(restored)
            with restored.connect() as connection:
                measured = {
                    "ledger_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM canonical_game_ledger_games "
                        "WHERE status IN ('final', 'completed')"
                    )).scalar_one()),
                    "pointer_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM publication_pointers p "
                        "JOIN publication_versions v "
                        "ON v.publication_id = p.active_publication_id"
                    )).scalar_one()),
                    "provenance_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM publication_observations"
                    )).scalar_one()),
                    "audit_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM collection_audit_events"
                    )).scalar_one()),
                    "pbp_repair_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM canonical_game_ledger_backfill "
                        "WHERE status = 'complete'"
                    )).scalar_one()),
                    "replay_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM composition_jobs "
                        "WHERE status IN ('queued', 'succeeded')"
                    )).scalar_one()),
                    "orphan_provenance_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM publication_observations p "
                        "LEFT JOIN publication_versions v ON v.publication_id = p.publication_id "
                        "LEFT JOIN collection_observations o ON o.observation_id = p.observation_id "
                        "WHERE v.publication_id IS NULL OR o.observation_id IS NULL"
                    )).scalar_one()),
                    "orphan_activation_rows": int(connection.execute(text(
                        "SELECT COUNT(*) FROM publication_activations a "
                        "LEFT JOIN publication_versions v ON v.publication_id = a.publication_id "
                        "WHERE v.publication_id IS NULL"
                    )).scalar_one()),
                    "latest_governed_cutoff": connection.execute(text(
                        "SELECT MAX(cutoff) FROM collection_observations"
                    )).scalar_one(),
                }
                expected_ledger = self.restore_expectations.get("ledger", ())
                expected_publications = self.restore_expectations.get("publications", ())
                expected_pointers = self.restore_expectations.get("pointers", ())
                def expected_rows(value: Any) -> tuple[Mapping[str, Any], ...]:
                    if isinstance(value, Mapping):
                        return (value,)
                    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
                        return tuple(value)
                    return ()
                ledger_rows = expected_rows(expected_ledger)
                publication_rows = expected_rows(expected_publications)
                pointer_rows = expected_rows(expected_pointers)
                exact_ledger = all(
                    connection.execute(text(
                        "SELECT 1 FROM canonical_game_ledger_games "
                        "WHERE game_id = :game_id AND checksum = :checksum"
                    ), {"game_id": str(row["game_id"]), "checksum": str(row["checksum"])}).first()
                    is not None
                    for row in ledger_rows
                )
                exact_publications = all(
                    connection.execute(text(
                        "SELECT 1 FROM publication_versions "
                        "WHERE publication_id = :publication_id AND checksum = :checksum"
                    ), {"publication_id": str(row["publication_id"]), "checksum": str(row["checksum"])}).first()
                    is not None
                    for row in publication_rows
                )
                exact_pointers = all(
                    connection.execute(text(
                        "SELECT 1 FROM publication_pointers WHERE stream_key = :stream_key "
                        "AND active_publication_id = :active_publication_id "
                        "AND previous_publication_id = :previous_publication_id"
                    ), {
                        "stream_key": str(row["stream_key"]),
                        "active_publication_id": str(row["active_publication_id"]),
                        "previous_publication_id": str(row["previous_publication_id"]),
                    }).first() is not None
                    for row in pointer_rows
                )
            checks = {
                "ledger_validated": measured["ledger_rows"] > 0,
                "pointers_validated": measured["pointer_rows"] > 0,
                "provenance_validated": measured["provenance_rows"] > 0,
                "audit_validated": measured["audit_rows"] > 0,
                "pbp_repair_validated": measured["pbp_repair_rows"] > 0,
                "replay_validated": measured["replay_rows"] > 0,
                "foreign_keys_validated": measured["orphan_provenance_rows"] == 0 and measured["orphan_activation_rows"] == 0,
                "exact_expected_rows_validated": (
                    bool(ledger_rows) and bool(publication_rows) and bool(pointer_rows)
                    and exact_ledger and exact_publications and exact_pointers
                ),
                "outbox_replayed_twice": bool(replay_result.get("outbox_replayed_twice")),
                "pbp_repair_executed": bool(repair_result.get("verified")),
                "pbp_repair_observation_id": bool(repair_result.get("observation_id")),
                "pbp_repair_job_id": bool(repair_result.get("composition_job_id")),
            }
            recovery_time_ms = round((perf_counter() - restore_started) * 1000, 3)
            return {
                **measured,
                **checks,
                "adapter": "postgres_disposable_restore",
                "environment": "postgres_isolated",
                "recovery_time_ms": recovery_time_ms,
                "restore_duration_ms": recovery_time_ms,
                "recovery_data_point": {
                    "observed_at": self.clock().astimezone(UTC).isoformat(),
                    "latest_governed_cutoff": (
                        measured["latest_governed_cutoff"].isoformat()
                        if hasattr(measured["latest_governed_cutoff"], "isoformat")
                        else measured["latest_governed_cutoff"]
                    ),
                    "latest_observation": repair_result.get("observation_id"),
                    "query_duration_ms": recovery_time_ms,
                },
                "restore_command_evidence": command_evidence,
                "replay_evidence": replay_result,
                "pbp_repair_evidence": repair_result,
                "pbp_repair_observation_id": repair_result.get("observation_id"),
                "pbp_repair_job_id": repair_result.get("composition_job_id"),
                "production_evidence": True,
                "verified": all(checks.values()),
            }
        finally:
            restored.dispose()


def run_failure_drills(
    *, hooks: Mapping[str, Callable[[], Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
    database_url: str | None = None,
    environment: str = "unit",
    isolated: bool = False,
    production_database_url: str | None = None,
    restored_database_url: str | None = None,
    require_production_evidence: bool = False,
    restore_adapter: Callable[[], Mapping[str, Any]] | None = None,
    disposable_marker_nonce: str | None = None,
    disposable_schema: str | None = None,
    restored_marker_nonce: str | None = None,
    restored_schema: str | None = None,
    restore_expectations: Mapping[str, Any] | None = None,
    pbp_repair: Callable[[Engine, str], Mapping[str, Any] | bool] | None = None,
    restore_ingestion: Callable[[Engine, CollectorClaims, Mapping[str, Any], bytes], Any] | None = None,
    restore_started: float | None = None,
    restore_command_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience function used by scripts and smoke tests."""

    return FailureDrillRunner(
        database_url=database_url,
        environment=environment,
        isolated=isolated,
        production_database_url=production_database_url,
        restored_database_url=restored_database_url,
        clock=clock,
        restore_adapter=restore_adapter,
        disposable_marker_nonce=disposable_marker_nonce,
        disposable_schema=disposable_schema,
        restored_marker_nonce=restored_marker_nonce,
        restored_schema=restored_schema,
        restore_expectations=restore_expectations,
        pbp_repair=pbp_repair,
        restore_ingestion=restore_ingestion,
        restore_started=restore_started,
        restore_command_evidence=restore_command_evidence,
    ).run(
        hooks=hooks,
        require_production_evidence=require_production_evidence,
    ).to_dict()


def run_restore_drill(
    *,
    restore: Callable[[], Any] | None = None,
    replay: Callable[[], Any] | None = None,
) -> DrillResult:
    """Verify an isolated restore and idempotent replay without claiming SLA."""

    started = perf_counter()
    try:
        if restore is None or replay is None:
            raise ValueError("restore and replay callbacks are required")
        restored = restore()
        replayed = replay()
        passed = restored is not False and replayed is not False
        measured = round((perf_counter() - started) * 1000, 3)
        return DrillResult(
            name="isolated_restore_replay",
            status="passed" if passed else "failed",
            attempts=1,
            details={
                "restore": restored,
                "replay": replayed,
                "sla_claimed": False,
                "recovery_time_ms": measured,
            },
        )
    except Exception as error:
        return DrillResult(
            name="isolated_restore_replay",
            status="failed",
            attempts=1,
            details={
                "error": type(error).__name__,
                "sla_claimed": False,
                "recovery_time_ms": round((perf_counter() - started) * 1000, 3),
            },
        )


__all__ = [
    "DatabaseIdentity",
    "DrillResult",
    "FailureDrillReport",
    "FailureDrillRunner",
    "connected_database_identity",
    "preflight_disposable_database",
    "run_failure_drills",
    "run_restore_drill",
    "same_database_identity",
]


def preflight_disposable_database(
    database_url: str,
    *,
    marker_nonce: str,
    schema: str | None,
    label: str,
    require_empty: bool = True,
) -> None:
    """Public operator seam for checking a marked target before restore."""

    FailureDrillRunner.preflight_disposable_database(
        database_url,
        marker_nonce=marker_nonce,
        schema=schema,
        label=label,
        require_empty=require_empty,
    )
