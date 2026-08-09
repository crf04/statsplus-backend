"""Deadline behavior shared by the DFS provider adapters."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ProviderUnavailableError
from app.providers.dabble import DabbleAdapter
from app.providers.dfs import NBAMarketQuery, RetrievalContext
from app.providers.dfs_transport import TransportErrorPolicy, request_json
from app.providers.prizepicks import PrizePicksAdapter
from app.providers.underdog import UnderdogAdapter
from app.utils import telemetry


_TEST_TRANSPORT_POLICY = TransportErrorPolicy(
    deadline_message="deadline",
    timeout_message="timeout",
    unavailable_message="unavailable",
    invalid_json_message="invalid json",
)


class _LateResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {}


class _BlockingSession:
    """A request implementation that deliberately ignores its timeout."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict[str, object]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> _LateResponse:
        self.calls.append({"url": url, **kwargs})
        self.started.set()
        self.release.wait()
        return _LateResponse()


class _BlockingJSONResponse:
    status_code = 200

    def __init__(self, session: "_BlockingJSONSession") -> None:
        self.session = session

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        self.session.pipeline_started.set()
        self.session.release.wait()
        self.session.pipeline_finished.set()
        return {}


class _BlockingJSONSession:
    """A response whose JSON decoder ignores the transport deadline."""

    def __init__(self) -> None:
        self.pipeline_started = threading.Event()
        self.pipeline_finished = threading.Event()
        self.release = threading.Event()
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> _BlockingJSONResponse:
        del url, kwargs
        return _BlockingJSONResponse(self)


class _BlockingParserSession:
    """A response whose provider parser ignores the transport deadline."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> _LateResponse:
        del url, kwargs
        return _LateResponse()


class _RetryThenBlockingSession:
    """Publish a retry, then hold the worker past the caller deadline."""

    def __init__(self) -> None:
        self.retry_seen = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> _LateResponse:
        del url, kwargs
        telemetry.increment_retry_count()
        self.retry_seen.set()
        self.release.wait()
        self.finished.set()
        return _LateResponse()


@pytest.fixture(autouse=True)
def _clear_provider_events():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


@pytest.mark.parametrize(
    ("provider", "build_adapter"),
    [
        ("dabble", lambda session: DabbleAdapter(session=session)),
        ("prizepicks", lambda session: PrizePicksAdapter(session=session)),
        ("underdog", lambda session: UnderdogAdapter(session=session)),
    ],
)
def test_blocking_request_returns_at_absolute_deadline(
    provider: str,
    build_adapter,
) -> None:
    session = _BlockingSession()
    request_id = f"deadline-{provider}"
    start = datetime.now(timezone.utc)
    deadline = start + timedelta(milliseconds=40)
    context = RetrievalContext(deadline=deadline, request_id=request_id)
    finished = threading.Event()
    outcome: dict[str, BaseException | object] = {}

    def retrieve() -> None:
        try:
            outcome["snapshot"] = build_adapter(session).get_snapshot(
                NBAMarketQuery(), context
            )
        except BaseException as error:  # captured for the assertion below
            outcome["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=retrieve)
    thread.start()
    assert session.started.wait(timeout=1.0)
    try:
        assert finished.wait(timeout=0.15), (
            f"{provider} did not return by the retrieval deadline"
        )
    finally:
        # The in-flight request is deliberately released only after the caller
        # has observed its timeout; cleanup must never depend on its timeout.
        session.release.set()
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), ProviderUnavailableError)
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["provider"] == provider
    assert events[0]["request_id"] == request_id
    assert events[0]["outcome"] == telemetry.OUTCOME_TIMEOUT


@pytest.mark.parametrize(
    ("provider", "build_adapter"),
    [
        ("dabble", lambda session: DabbleAdapter(session=session)),
        ("prizepicks", lambda session: PrizePicksAdapter(session=session)),
        ("underdog", lambda session: UnderdogAdapter(session=session)),
    ],
)
def test_blocking_json_returns_at_absolute_deadline(
    provider: str,
    build_adapter,
) -> None:
    session = _BlockingJSONSession()
    request_id = f"blocking-json-{provider}"
    start = datetime.now(timezone.utc)
    context = RetrievalContext(
        deadline=start + timedelta(milliseconds=40),
        request_id=request_id,
    )
    finished = threading.Event()
    outcome: dict[str, BaseException | object] = {}

    def retrieve() -> None:
        try:
            outcome["snapshot"] = build_adapter(session).get_snapshot(
                NBAMarketQuery(), context
            )
        except BaseException as error:  # captured for the assertion below
            outcome["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=retrieve)
    thread.start()
    assert session.pipeline_started.wait(timeout=1.0)
    try:
        assert finished.wait(timeout=0.15), (
            f"{provider} did not return while response.json was blocked"
        )
    finally:
        session.release.set()
        assert session.pipeline_finished.wait(timeout=1.0)
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), ProviderUnavailableError)
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["provider"] == provider
    assert events[0]["request_id"] == request_id
    assert events[0]["outcome"] == telemetry.OUTCOME_TIMEOUT


def test_blocking_provider_parser_returns_at_absolute_deadline() -> None:
    session = _BlockingParserSession()
    request_id = "blocking-parser"
    context = RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(milliseconds=40),
        request_id=request_id,
    )
    parser_started = threading.Event()
    parser_finished = threading.Event()

    def parse(_payload: object) -> str:
        parser_started.set()
        session.release.wait()
        parser_finished.set()
        return "late parser result"

    try:
        with pytest.raises(ProviderUnavailableError):
            request_json(
                context=context,
                session=session,
                url="https://example.test/snapshot",
                params=None,
                timeout=(1.0, 1.0),
                now=lambda: datetime.now(timezone.utc),
                provider="prizepicks",
                operation="get_snapshot",
                parse=parse,
                error_policy=_TEST_TRANSPORT_POLICY,
            )
        assert parser_started.is_set()
    finally:
        session.release.set()

    assert parser_finished.wait(timeout=1.0)
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["request_id"] == request_id
    assert events[0]["outcome"] == telemetry.OUTCOME_TIMEOUT


def test_retry_progress_is_recorded_when_worker_outlives_deadline() -> None:
    session = _RetryThenBlockingSession()
    request_id = "retry-before-deadline"
    context = RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(milliseconds=40),
        request_id=request_id,
    )

    try:
        with pytest.raises(ProviderUnavailableError):
            request_json(
                context=context,
                session=session,
                url="https://example.test/snapshot",
                params=None,
                timeout=(1.0, 1.0),
                now=lambda: datetime.now(timezone.utc),
                provider="prizepicks",
                operation="get_snapshot",
                parse=lambda _payload: "late result",
                error_policy=_TEST_TRANSPORT_POLICY,
            )
        assert session.retry_seen.is_set()
    finally:
        session.release.set()

    assert session.finished.wait(timeout=1.0)
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["request_id"] == request_id
    assert events[0]["outcome"] == telemetry.OUTCOME_TIMEOUT
    assert events[0]["retry_count"] == 1


def test_transport_error_policy_is_immutable_and_translates_messages():
    policy = TransportErrorPolicy(
        deadline_message="deadline",
        timeout_message="timeout",
        unavailable_message="unavailable",
        invalid_json_message="invalid json",
    )

    with pytest.raises(AttributeError):
        policy.timeout_message = "changed"  # type: ignore[misc]
