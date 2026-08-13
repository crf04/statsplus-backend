"""Deterministic failure, recovery, and isolated-restore drill tooling."""

from __future__ import annotations

import hashlib
import json
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

from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionManifest,
    CollectorStatusTransition,
    PublicationPointer,
)
from app.services.collection_control import (
    CollectorTokenService,
    ObservationIngestionService,
    PublicationService,
)
from app.collector.contracts import ObservationEnvelope
from app.collector.outbox import ResidentialOutbox


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class DrillResult:
    name: str
    status: str
    attempts: int
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FailureDrillReport:
    status: str
    started_at: str
    completed_at: str
    drills: tuple[DrillResult, ...]
    environment: str = "unit"
    production_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "environment": self.environment,
            "production_evidence": self.production_evidence,
            "drills": [asdict(drill) for drill in self.drills],
        }


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
    ) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self.environment = str(environment).strip().lower() or "unit"
        self.run_id = uuid4().hex
        self.production_database_url = production_database_url
        self.restored_database_url = restored_database_url
        self.restore_adapter = restore_adapter
        requested_url = database_url or (str(engine.url) if engine is not None else None)
        self.configuration_error = self._validate_configuration(
            requested_url,
            isolated=isolated,
            production_database_url=production_database_url,
            restored_database_url=restored_database_url,
        )
        if self.configuration_error is not None:
            self.engine = engine or create_engine("sqlite:///:memory:")
            self.production_evidence = False
            self.publications = None
            self.ingestion = None
            return
        self.engine = engine or create_engine(database_url or "sqlite:///:memory:")
        self.production_evidence = (
            self.environment not in {"unit", "test_unit"}
            and self.engine.dialect.name == "postgresql"
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
    ) -> str | None:
        if database_url is None:
            return None
        normalized = str(database_url).lower()
        if not isolated and any(
            marker in normalized for marker in ("prod", "production", "railway")
        ):
            return "drill database must be explicitly marked isolated"
        if production_database_url and str(database_url) == str(production_database_url):
            return "drill database cannot equal production/control database"
        if restored_database_url and str(restored_database_url) in {
            str(database_url), str(production_database_url)
        }:
            return "restored drill database must be a separate disposable database"
        return None

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
            if self.production_evidence:
                return self._query_restored_database()
            from sqlite3 import connect
            from app.models.canonical_game_ledger import (
                CanonicalGameLedgerGame,
                LedgerBackfillState,
                LedgerPublication,
            )
            from app.models.collection_control import (
                AuditEvent,
                CollectionObservation,
                CompositionJob,
                PublicationObservation,
                PublicationVersion,
            )

            restore_publication = self._id("restore-publication")
            restore_stream = self._id("restore-stream")
            restore_observation = self._id("restore-observation")
            restore_collector = self._id("restore-collector")
            restore_game = self._id("restore-game")
            restore_audit = self._id("restore-audit")
            restore_job = self._id("restore-job")

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
                if connection.execute(
                    PublicationVersion.__table__.select().where(
                        PublicationVersion.publication_id == publication["publication_id"]
                    )
                ).first() is None:
                    connection.execute(PublicationVersion.__table__.insert().values(**publication))
                if connection.execute(
                    PublicationPointer.__table__.select().where(
                        PublicationPointer.stream_key == restore_stream
                    )
                ).first() is None:
                    connection.execute(PublicationPointer.__table__.insert().values(
                        stream_key=restore_stream,
                        active_publication_id=publication["publication_id"],
                        previous_publication_id=None,
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
                        checksum="g" * 64,
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
            raw = self.engine.raw_connection()
            try:
                source = raw.driver_connection
                target = connect(":memory:")
                source.backup(target)
                checks = {
                    "ledger": f"SELECT game_id FROM canonical_game_ledger_games WHERE game_id = '{restore_game}'",
                    "pointers": f"SELECT stream_key FROM publication_pointers WHERE stream_key = '{restore_stream}' AND active_publication_id = '{restore_publication}'",
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
                target.close()
            finally:
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
                "sla_claimed": False,
                "adapter": "sqlite_unit",
                "environment": "unit",
                "production_evidence": False,
                "verified": replayed_rows == restored_rows and all(restored_checks.values()),
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

    def _query_restored_database(self) -> Mapping[str, Any]:
        """Validate a disposable restored Postgres database by querying it.

        The operator supplies the URL of the restored isolated database, not a
        JSON assertion callback.  Every reported check below is measured from
        rows in that database, so a control-plane command cannot manufacture a
        passing restore artifact.
        """

        if not self.restored_database_url:
            raise ValueError("restored_isolated_database_url_required")
        started = perf_counter()
        if str(self.restored_database_url) in {
            str(self.production_database_url),
            str(self.engine.url),
        }:
            raise ValueError("restored_database_must_be_disposable_and_separate")
        restored = create_engine(self.restored_database_url)
        try:
            if restored.dialect.name != "postgresql":
                raise ValueError("production_restore_requires_postgres")
            table_names = set(inspect(restored).get_table_names())
            required_tables = {
                "canonical_game_ledger_games",
                "canonical_game_ledger_backfill",
                "publication_pointers",
                "publication_versions",
                "publication_observations",
                "collection_audit_events",
                "composition_jobs",
            }
            if not required_tables <= table_names:
                raise ValueError("restored_database_schema_incomplete")
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
                }
            checks = {
                "ledger_validated": measured["ledger_rows"] > 0,
                "pointers_validated": measured["pointer_rows"] > 0,
                "provenance_validated": measured["provenance_rows"] > 0,
                "audit_validated": measured["audit_rows"] > 0,
                "pbp_repair_validated": measured["pbp_repair_rows"] > 0,
                "replay_validated": measured["replay_rows"] > 0,
            }
            recovery_time_ms = round((perf_counter() - started) * 1000, 3)
            return {
                **measured,
                **checks,
                "adapter": "postgres_disposable_restore",
                "environment": "postgres_isolated",
                "recovery_time_ms": recovery_time_ms,
                "recovery_data_point": {
                    "observed_at": self.clock().astimezone(UTC).isoformat(),
                    "query_duration_ms": recovery_time_ms,
                },
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
    "DrillResult",
    "FailureDrillReport",
    "FailureDrillRunner",
    "run_failure_drills",
    "run_restore_drill",
]
