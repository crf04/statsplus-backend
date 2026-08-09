"""Offline tests for the internal DFS board collector."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.config.settings import ConfigurationError, load_settings
from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    CoverageCode,
    CoverageEvidence,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    SnapshotStatus,
)
from app.services.dfs_board import DFSBoardService, ProviderFailureReason, ProviderOutcomeStatus
from app.utils.telemetry import BoardTelemetryEvent
from app.utils import telemetry


_RETRIEVED_AT = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)


def _snapshot(
    provider: str,
    *,
    status: SnapshotStatus | str = SnapshotStatus.COMPLETE,
    coverage: CoverageEvidence | None = None,
) -> ProviderSnapshot:
    evidence = coverage or CoverageEvidence(pagination_complete=True, fanout_complete=True)
    return ProviderSnapshot(
        provider=provider,
        status=status,
        markets=(),
        coverage=evidence,
        retrieved_at=_RETRIEVED_AT,
    )


def _context(seconds: float = 2.0) -> RetrievalContext:
    return RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        request_id="board-test",
    )


class FakeBoardTelemetry:
    def __init__(self) -> None:
        self.events: list[BoardTelemetryEvent] = []

    def record(self, event: BoardTelemetryEvent) -> None:
        self.events.append(event)


class ControlledClock:
    def __init__(self) -> None:
        self.wall = _RETRIEVED_AT
        self.monotonic_value = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)


class FakeProvider:
    def __init__(self, name: str, result=None, *, error=None):
        self.name = name
        self.result = result if result is not None else _snapshot(name)
        self.error = error
        self.calls: list[tuple[NBAMarketQuery, RetrievalContext]] = []

    def get_snapshot(self, query, context):
        self.calls.append((query, context))
        if self.error is not None:
            raise self.error
        return self.result


def test_board_context_is_capped_to_fifteen_seconds_and_parent_deadline() -> None:
    provider = FakeProvider("dabble")
    parent = RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
        request_id="board-test",
    )
    service = DFSBoardService(provider_registry={"dabble": provider}, deadline_seconds=60)

    service.get_board(NBAMarketQuery(), parent)

    child = provider.calls[0][1]
    assert child is not parent
    assert child.request_id == parent.request_id
    assert child.deadline <= datetime.now(timezone.utc) + timedelta(seconds=15.1)
    assert child.deadline <= parent.deadline


def test_bare_timeout_error_has_stable_timeout_reason() -> None:
    provider = FakeProvider("dabble", error=TimeoutError("socket stalled"))
    outcome = DFSBoardService(provider_registry={"dabble": provider}).get_board(
        NBAMarketQuery(), _context()
    ).provider_outcomes[0]
    assert outcome.reason is ProviderFailureReason.TIMEOUT


def test_provider_result_after_deadline_is_dropped_without_sleep() -> None:
    started = threading.Event()
    release = threading.Event()

    class BoundaryProvider(FakeProvider):
        def get_snapshot(self, query, context):
            self.calls.append((query, context))
            started.set()
            release.wait()
            return self.result

    provider = BoundaryProvider("dabble")
    clock = ControlledClock()
    telemetry = FakeBoardTelemetry()
    service = DFSBoardService(
        provider_registry={"dabble": provider},
        deadline_seconds=1,
        clock=clock.now,
        monotonic=clock.monotonic,
        telemetry_recorder=telemetry,
    )
    result: dict[str, object] = {}
    context = RetrievalContext(deadline=clock.wall + timedelta(seconds=10), request_id="board-test")

    def retrieve() -> None:
        result["board"] = service.get_board(NBAMarketQuery(), context)

    thread = threading.Thread(target=retrieve)
    thread.start()
    assert started.wait(timeout=1)
    clock.advance(2)
    release.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    board = result["board"]
    assert board.snapshots == ()
    assert telemetry.events[0].outcome_counts == (("failed", 1),)


def test_board_accepts_result_completed_at_exact_monotonic_boundary() -> None:
    clock = ControlledClock()

    class ExactProvider(FakeProvider):
        def get_snapshot(self, query, context):
            self.calls.append((query, context))
            clock.advance(1)
            return self.result

    provider = ExactProvider("dabble")
    context = RetrievalContext(deadline=clock.wall + timedelta(seconds=10), request_id="board-test")
    board = DFSBoardService(
        provider_registry={"dabble": provider},
        deadline_seconds=1,
        clock=clock.now,
        monotonic=clock.monotonic,
    ).get_board(NBAMarketQuery(), context)

    assert board.snapshots == (provider.result,)


def test_late_outcome_construction_is_not_harvested() -> None:
    clock = ControlledClock()

    class NearDeadlineProvider(FakeProvider):
        def get_snapshot(self, query, context):
            self.calls.append((query, context))
            clock.advance(0.9)
            return self.result

    provider = NearDeadlineProvider("dabble")
    service = DFSBoardService(
        provider_registry={"dabble": provider},
        deadline_seconds=1,
        clock=clock.now,
        monotonic=clock.monotonic,
    )
    original = DFSBoardService._outcome_from_snapshot

    def normalize_late(name, snapshot):
        outcome = original(name, snapshot)
        clock.advance(0.2)
        return outcome

    service._outcome_from_snapshot = normalize_late
    provider.result = _snapshot("dabble")
    parent = RetrievalContext(deadline=clock.wall + timedelta(seconds=10), request_id="board-test")
    board = service.get_board(NBAMarketQuery(), parent)

    assert board.snapshots == ()
    assert board.provider_outcomes[0].reason is ProviderFailureReason.DEADLINE_EXCEEDED


def test_board_telemetry_records_bounded_outcomes_and_coverage() -> None:
    telemetry = FakeBoardTelemetry()
    coverage = CoverageEvidence(
        fetched_count=4,
        eligible_count=2,
        normalized_count=1,
        skipped_count=2,
        pagination_complete=True,
        fanout_complete=True,
    )
    provider = FakeProvider("dabble", _snapshot("dabble", coverage=coverage))
    board = DFSBoardService(
        provider_registry={"dabble": provider}, telemetry_recorder=telemetry
    ).get_board(NBAMarketQuery(), _context())

    assert board.usable
    event = telemetry.events[0]
    assert event.duration_ms >= 0
    assert event.outcome_counts == (("complete", 1),)
    assert event.failure_reason_counts == ()
    assert (event.fetched_count, event.eligible_count, event.normalized_count, event.skipped_count) == (4, 2, 1, 2)


def test_default_board_telemetry_uses_bounded_events_without_provider_failure_count() -> None:
    telemetry.clear_recorded_provider_events()
    try:
        service = DFSBoardService(provider_registry={"dabble": FakeProvider("dabble")})
        service.get_board(NBAMarketQuery(), _context())
        assert telemetry.get_recorded_provider_events() == []
        event = telemetry.get_recorded_board_events()[-1]
        assert event["outcome_complete"] == 1
        assert event["request_id"] == "board-test"
        assert "dfs_board" not in telemetry.snapshot_metrics()["provider_failures"]
    finally:
        telemetry.clear_recorded_provider_events()


def test_production_settings_require_an_explicit_nonempty_provider_list():
    with pytest.raises(ConfigurationError, match="DFS_ENABLED_PROVIDERS"):
        load_settings(
            environ={
                "FLASK_ENV": "production",
                "DATABASE_URL": "postgresql://statsplus.example/db",
                "CORS_ALLOWED_ORIGINS": "https://statsplus.example",
                "FIREBASE_SERVICE_ACCOUNT_JSON": (
                    '{"project_id":"p","private_key":"k","client_email":"e"}'
                ),
                "DFS_ENABLED_PROVIDERS": "",
            }
        )


def test_disabled_providers_are_metadata_not_failed_outcomes():
    provider = FakeProvider("dabble")
    service = DFSBoardService(provider_registry={"dabble": provider})

    board = service.get_board(NBAMarketQuery(), _context())

    assert board.disabled_providers == ("prizepicks", "underdog")
    assert [outcome.provider for outcome in board.provider_outcomes] == ["dabble"]
    assert all(outcome.status is not ProviderOutcomeStatus.FAILED for outcome in board.provider_outcomes)


def test_enabled_provider_registry_is_concurrent_and_context_is_shared():
    release = threading.Event()
    started = [threading.Event() for _ in range(3)]
    active = 0
    maximum = 0
    lock = threading.Lock()

    class BoundedProvider(FakeProvider):
        def get_snapshot(self, query, context):
            nonlocal active, maximum
            self.calls.append((query, context))
            with lock:
                active += 1
                maximum = max(maximum, active)
            started[tuple(providers).index(self.name)].set()
            release.wait()
            with lock:
                active -= 1
            return self.result

    providers = {name: BoundedProvider(name) for name in ("dabble", "prizepicks", "underdog")}
    service = DFSBoardService(provider_registry=providers, max_concurrency=2)
    context = _context()
    result: dict[str, object] = {}
    thread = threading.Thread(target=lambda: result.setdefault("board", service.get_board(NBAMarketQuery(), context)))
    thread.start()
    assert started[0].wait(timeout=1)
    assert started[1].wait(timeout=1)
    assert maximum == 2
    release.set()
    thread.join(timeout=1)
    board = result["board"]
    child_contexts = [call_context for provider in providers.values() for _, call_context in provider.calls]
    assert child_contexts
    assert all(call_context is child_contexts[0] for call_context in child_contexts)
    assert child_contexts[0] is not context
    assert [outcome.provider for outcome in board.provider_outcomes] == [
        "dabble",
        "prizepicks",
        "underdog",
    ]


def test_complete_empty_snapshot_is_usable_and_coverage_is_preserved():
    coverage = CoverageEvidence(
        fetched_count=4,
        eligible_count=1,
        normalized_count=1,
        skipped_count=3,
        pagination_complete=True,
        fanout_complete=True,
        warning_codes=(CoverageCode.MALFORMED_RECORD,),
    )
    provider = FakeProvider("dabble", _snapshot("dabble", coverage=coverage))

    board = DFSBoardService(provider_registry={"dabble": provider}).get_board(
        NBAMarketQuery(), _context()
    )

    outcome = board.provider_outcomes[0]
    assert outcome.status is ProviderOutcomeStatus.COMPLETE
    assert outcome.usable
    assert outcome.snapshot is not None
    assert outcome.snapshot.coverage == coverage
    assert board.snapshots == (outcome.snapshot,)


def test_partial_snapshot_is_one_observation_and_keeps_coverage():
    coverage = CoverageEvidence(
        fetched_count=2,
        eligible_count=1,
        normalized_count=1,
        skipped_count=1,
        pagination_complete=False,
        warning_codes=(CoverageCode.PAGE_FETCH_FAILED,),
    )
    snapshot = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.PARTIAL,
        markets=(PlayerProjectionMarket(provider="dabble"),),
        coverage=coverage,
        retrieved_at=_RETRIEVED_AT,
    )
    provider = FakeProvider("dabble", snapshot)

    outcome = DFSBoardService(provider_registry={"dabble": provider}).get_board(
        NBAMarketQuery(), _context()
    ).provider_outcomes[0]

    assert outcome.status is ProviderOutcomeStatus.PARTIAL
    assert outcome.snapshot is snapshot
    assert outcome.coverage is coverage
    assert outcome.reason is ProviderFailureReason.UPSTREAM_ERROR


def test_expected_provider_failures_do_not_erase_usable_snapshots():
    usable = FakeProvider("dabble")
    failed = FakeProvider("prizepicks", error=requests.exceptions.Timeout())

    board = DFSBoardService(
        provider_registry={"dabble": usable, "prizepicks": failed}
    ).get_board(NBAMarketQuery(), _context())

    assert [snapshot.provider for snapshot in board.snapshots] == ["dabble"]
    failed_outcome = board.provider_outcomes[1]
    assert failed_outcome.status is ProviderOutcomeStatus.FAILED
    assert failed_outcome.reason is ProviderFailureReason.TIMEOUT


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("rate_limited", ProviderFailureReason.RATE_LIMITED),
        ("access_denied", ProviderFailureReason.ACCESS_DENIED),
        ("malformed_response", ProviderFailureReason.MALFORMED_RESPONSE),
    ],
)
def test_provider_failure_details_become_stable_sanitized_reasons(detail, reason):
    provider = FakeProvider(
        "dabble",
        error=ProviderUnavailableError("provider failed", provider_reason=detail),
    )

    outcome = DFSBoardService(provider_registry={"dabble": provider}).get_board(
        NBAMarketQuery(), _context()
    ).provider_outcomes[0]

    assert outcome.status is ProviderOutcomeStatus.FAILED
    assert outcome.reason is reason
    assert "provider failed" not in repr(outcome)


def test_implementation_defects_propagate():
    provider = FakeProvider("dabble", error=AssertionError("bug"))

    with pytest.raises(AssertionError, match="bug"):
        DFSBoardService(provider_registry={"dabble": provider}).get_board(
            NBAMarketQuery(), _context()
        )


def test_deadline_drops_pending_and_late_provider_results():
    release = threading.Event()
    started = threading.Event()

    class LateProvider(FakeProvider):
        def get_snapshot(self, query, context):
            self.calls.append((query, context))
            started.set()
            release.wait()
            return self.result

    provider = LateProvider("dabble")
    context = _context(seconds=0.04)
    service = DFSBoardService(provider_registry={"dabble": provider})

    try:
        board = service.get_board(NBAMarketQuery(), context)
    finally:
        release.set()

    assert started.is_set()
    assert board.snapshots == ()
    assert board.provider_outcomes[0].status is ProviderOutcomeStatus.FAILED
    assert board.provider_outcomes[0].reason is ProviderFailureReason.DEADLINE_EXCEEDED
