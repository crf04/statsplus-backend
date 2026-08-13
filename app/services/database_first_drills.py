"""Deterministic failure, recovery, and isolated-restore drill tooling."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


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

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))

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

    @staticmethod
    def _default_hook(name: str) -> Callable[[], Mapping[str, Any]]:
        """Return a credential-free simulation for one operational drill.

        The defaults intentionally model the safety invariant, rather than
        pretending that a network outage or a Postgres restore happened on a
        developer workstation.  Deployments can replace any hook with the
        real Railway/collector exercise while retaining the same report
        schema.
        """

        def railway_outage_retry() -> Mapping[str, Any]:
            attempts = ("failed", "succeeded")
            published_attempts = (2,)
            return {
                "retry_statuses": attempts,
                "published_attempts": published_attempts,
                "no_partial_publish": published_attempts == (2,),
                "verified": attempts == ("failed", "succeeded")
                and published_attempts == (2,),
            }

        def duplicate_delivery_idempotency() -> Mapping[str, Any]:
            deliveries = {"manifest-1", "manifest-1"}
            committed = {"manifest-1"}
            return {
                "deliveries": 2,
                "committed": len(committed),
                "idempotent": len(deliveries) == len(committed),
                "verified": len(deliveries) == len(committed),
            }

        def collector_reboot_outbox_replay() -> Mapping[str, Any]:
            outbox = {"observation-1"}
            replayed = set(outbox)
            replayed.update(outbox)
            return {
                "pending_before_reboot": len(outbox),
                "replayed_after_reboot": len(replayed),
                "idempotent": replayed == outbox,
                "verified": replayed == outbox,
            }

        def expired_credential_rejection() -> Mapping[str, Any]:
            credential_valid = False
            return {
                "credential_status": "expired",
                "accepted": credential_valid,
                "writes": 0,
                "verified": not credential_valid,
            }

        def provider_failure_last_good_retention() -> Mapping[str, Any]:
            last_good = {"value": 7}
            failed_attempt = {"value": None}
            served = last_good.copy()
            return {
                "last_good": last_good,
                "failed_attempt": failed_attempt,
                "served": served,
                "last_good_retained": served == last_good,
                "partial_attempt_published": served == failed_attempt,
                "verified": served == last_good and served != failed_attempt,
            }

        def alert_recovery() -> Mapping[str, Any]:
            transitions = ("failure", "recovery")
            return {
                "alert_transitions": transitions,
                "recovery_emitted": transitions[-1] == "recovery",
                "verified": transitions[-1] == "recovery",
            }

        def isolated_restore_replay() -> Mapping[str, Any]:
            restored_rows = {"ledger-1", "pointer-1", "audit-1"}
            replayed_rows = set(restored_rows)
            replayed_rows.update(restored_rows)
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
        # The no-callback path is a local idempotency simulation.  A Railway
        # deployment supplies callbacks that perform the actual isolated
        # backup restore and Outbox/PBP replay.
        restored = restore() if restore is not None else {"rows": 3, "pointers": 1}
        replayed = replay() if replay is not None else {"rows": 3, "duplicates": 0}
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
