"""Offline tests for the internal DFS board collector."""

from __future__ import annotations

import threading
import time
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


class FakeProvider:
    def __init__(self, name: str, result=None, *, delay: float = 0.0, error=None):
        self.name = name
        self.result = result if result is not None else _snapshot(name)
        self.delay = delay
        self.error = error
        self.calls: list[tuple[NBAMarketQuery, RetrievalContext]] = []

    def get_snapshot(self, query, context):
        self.calls.append((query, context))
        if self.delay:
            time.sleep(self.delay)
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


def test_completed_provider_result_at_deadline_is_harvested() -> None:
    started = threading.Event()
    release = threading.Event()

    class BoundaryProvider(FakeProvider):
        def get_snapshot(self, query, context):
            self.calls.append((query, context))
            started.set()
            release.wait()
            return self.result

    provider = BoundaryProvider("dabble")
    service = DFSBoardService(provider_registry={"dabble": provider}, deadline_seconds=0.05)
    result: dict[str, object] = {}

    def retrieve() -> None:
        result["board"] = service.get_board(NBAMarketQuery(), _context(1))

    thread = threading.Thread(target=retrieve)
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.04)
    release.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    board = result["board"]
    assert board.snapshots == (provider.result,)


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
    providers = {name: FakeProvider(name, delay=0.04) for name in ("dabble", "prizepicks", "underdog")}
    service = DFSBoardService(provider_registry=providers, max_concurrency=2)
    context = _context()

    started = time.monotonic()
    board = service.get_board(NBAMarketQuery(), context)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.08
    assert elapsed < 0.20
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
