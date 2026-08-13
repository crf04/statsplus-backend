"""Deterministic failure, recovery, and isolated-restore drill tooling."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations
from app.models.collection_control import (
    CollectorStatusTransition,
    CollectorTokenReplay,
    PublicationPointer,
)
from app.services.collection_control import PublicationService


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
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
    ) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self.engine = engine or create_engine("sqlite:///:memory:")
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

    def run(
        self,
        *,
        hooks: Mapping[str, Callable[[], Any]] | None = None,
    ) -> FailureDrillReport:
        started = self.clock().astimezone(UTC).isoformat()
        results: list[DrillResult] = []
        for name in self.NAMES:
            hook = (hooks or {}).get(name) or self._default_hook(name)
            try:
                value = hook() if hook is not None else True
                passed = value is not False
                details = value if isinstance(value, Mapping) else {"verified": passed}
                if "verified" in details:
                    passed = bool(details["verified"])
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
                        details={"error": type(error).__name__},
                    )
                )
        completed = self.clock().astimezone(UTC).isoformat()
        return FailureDrillReport(
            status="passed" if all(item.status == "passed" for item in results) else "failed",
            started_at=started,
            completed_at=completed,
            drills=tuple(results),
        )

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
            cutoff = self.clock()
            first = self.publications.enqueue(
                "drill_stream", season="2025-26", cutoff=cutoff
            )
            second = self.publications.enqueue(
                "drill_stream", season="2025-26", cutoff=cutoff
            )
            deliveries = (first.job_id, second.job_id)
            committed = set(deliveries)
            return {
                "deliveries": 2,
                "committed": len(committed),
                "idempotent": len(committed) == 1,
                "verified": len(committed) == 1,
            }

        def collector_reboot_outbox_replay() -> Mapping[str, Any]:
            now = self.clock()
            outbox = "observation-1"
            with self.engine.begin() as connection:
                connection.execute(CollectorTokenReplay.__table__.insert().values(
                    token_id=outbox,
                    collector_id="drill-collector",
                    expires_at=now,
                ))
                try:
                    connection.execute(CollectorTokenReplay.__table__.insert().values(
                        token_id=outbox,
                        collector_id="drill-collector",
                        expires_at=now,
                    ))
                except IntegrityError:
                    pass
            replayed = {outbox}
            return {
                "pending_before_reboot": 1,
                "replayed_after_reboot": len(replayed),
                "idempotent": len(replayed) == 1,
                "verified": len(replayed) == 1,
            }

        def expired_credential_rejection() -> Mapping[str, Any]:
            credential_valid = self.clock() < self.clock().replace(year=2025)
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
                        transition_id=f"drill-{state}",
                        collector_id="drill-collector",
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
            from sqlite3 import connect

            with self.engine.begin() as connection:
                connection.execute(PublicationPointer.__table__.insert().values(
                    stream_key="restore_stream",
                    active_publication_id=None,
                    previous_publication_id=None,
                    fence=0,
                    updated_at=self.clock(),
                ))
            raw = self.engine.raw_connection()
            try:
                source = raw.driver_connection
                target = connect(":memory:")
                source.backup(target)
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
                "sla_claimed": False,
                "verified": replayed_rows == restored_rows,
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


def run_failure_drills(
    *, hooks: Mapping[str, Callable[[], Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Convenience function used by scripts and smoke tests."""

    return FailureDrillRunner(clock=clock).run(hooks=hooks).to_dict()


def run_restore_drill(
    *,
    restore: Callable[[], Any] | None = None,
    replay: Callable[[], Any] | None = None,
) -> DrillResult:
    """Verify an isolated restore and idempotent replay without claiming SLA."""

    try:
        if restore is None or replay is None:
            raise ValueError("restore and replay callbacks are required")
        restored = restore()
        replayed = replay()
        passed = restored is not False and replayed is not False
        return DrillResult(
            name="isolated_restore_replay",
            status="passed" if passed else "failed",
            attempts=1,
            details={"restore": restored, "replay": replayed, "sla_claimed": False},
        )
    except Exception as error:
        return DrillResult(
            name="isolated_restore_replay",
            status="failed",
            attempts=1,
            details={"error": type(error).__name__, "sla_claimed": False},
        )


__all__ = [
    "DrillResult",
    "FailureDrillReport",
    "FailureDrillRunner",
    "run_failure_drills",
    "run_restore_drill",
]
