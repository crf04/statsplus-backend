"""Deadline behavior shared by the DFS provider adapters."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.utils.telemetry import ProviderResponseError
from app.providers.dfs import RetrievalContext
from app.providers.dfs_transport import TransportErrorPolicy, _run_bounded, request_json
from app.utils import telemetry


_TEST_TRANSPORT_POLICY = TransportErrorPolicy(
    deadline_message="deadline",
    timeout_message="timeout",
    unavailable_message="unavailable",
    invalid_json_message="invalid json",
)


class _RetryResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self  # type: ignore[assignment]
            raise error

    def json(self) -> object:
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class _RetrySequenceSession:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.timeout_values: list[object] = []

    def get(self, url: str, **kwargs: object) -> _RetryResponse:
        del url
        self.timeout_values.append(kwargs.get("timeout"))
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _clear_provider_events():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def test_completed_result_is_accepted_when_caller_clock_resumes_late() -> None:
    class SequenceClock:
        def __init__(self) -> None:
            self.calls = 0
            self.worker_may_finish = threading.Event()
            self.late = False

        def __call__(self) -> float:
            self.calls += 1
            value = 2.0 if self.late and self.calls >= 5 else 0.0
            if self.calls == 4:
                self.worker_may_finish.set()
            return value

    clock = SequenceClock()
    context = RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(seconds=1),
        request_id="boundary-complete",
    )

    def complete(holder) -> None:
        clock.worker_may_finish.wait()
        holder.value = {"ok": True}
        holder.completed_at = 0.5
        clock.late = True
        holder.done.set()

    result = _run_bounded(
        context=context,
        now=lambda: datetime.now(timezone.utc),
        worker=complete,
        worker_name="boundary-complete",
        monotonic=clock,
    )

    assert result.value == {"ok": True}
    assert clock() > 1.0


@pytest.mark.parametrize("first", [requests.exceptions.Timeout(), _RetryResponse({}, 429), _RetryResponse({}, 500), _RetryResponse({}, 502), _RetryResponse({}, 503), _RetryResponse({}, 504)])
def test_safe_get_retries_once_within_the_deadline(first: object) -> None:
    session = _RetrySequenceSession(first, _RetryResponse({"ok": True}))
    context = RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(seconds=1),
        request_id="retry-safe-get",
    )

    result = request_json(
        context=context,
        session=session,
        url="https://example.test/snapshot",
        params=None,
        timeout=(3.0, 8.0),
        now=lambda: datetime.now(timezone.utc),
        provider="prizepicks",
        operation="get_snapshot",
        parse=lambda payload: payload,
        error_policy=_TEST_TRANSPORT_POLICY,
    )

    assert result == {"ok": True}
    assert session.calls == 2
    assert telemetry.get_recorded_provider_events()[-1]["retry_count"] == 1


@pytest.mark.parametrize("status", [501, 505])
def test_safe_get_does_not_retry_unreviewed_5xx_status(status: int) -> None:
    session = _RetrySequenceSession(_RetryResponse({}, status), _RetryResponse({"late": True}))
    with pytest.raises(ProviderUnavailableError):
        request_json(
            context=RetrievalContext(deadline=datetime.now(timezone.utc) + timedelta(seconds=1)),
            session=session,
            url="https://example.test/snapshot",
            params=None,
            timeout=(30.0, 30.0),
            now=lambda: datetime.now(timezone.utc),
            provider="prizepicks",
            operation="get_snapshot",
            parse=lambda payload: payload,
            error_policy=_TEST_TRANSPORT_POLICY,
        )
    assert session.calls == 1


def test_transport_caps_injected_timeout_tuple() -> None:
    session = _RetrySequenceSession(_RetryResponse({"ok": True}))
    request_json(
        context=RetrievalContext(deadline=datetime.now(timezone.utc) + timedelta(seconds=30)),
        session=session,
        url="https://example.test/snapshot",
        params=None,
        timeout=(30.0, 30.0),
        now=lambda: datetime.now(timezone.utc),
        provider="prizepicks",
        operation="get_snapshot",
        parse=lambda payload: payload,
        error_policy=_TEST_TRANSPORT_POLICY,
    )
    assert session.timeout_values[0] == (3.0, 8.0)


def test_safe_get_does_not_retry_access_denial_or_malformed_payload() -> None:
    access_denied = _RetrySequenceSession(
        _RetryResponse({}, 403), _RetryResponse({"late": True})
    )
    with pytest.raises(ProviderUnavailableError):
        request_json(
            context=RetrievalContext(deadline=datetime.now(timezone.utc) + timedelta(seconds=1)),
            session=access_denied,
            url="https://example.test/snapshot",
            params=None,
            timeout=(3.0, 8.0),
            now=lambda: datetime.now(timezone.utc),
            provider="prizepicks",
            operation="get_snapshot",
            parse=lambda payload: payload,
            error_policy=_TEST_TRANSPORT_POLICY,
        )
    assert access_denied.calls == 1

    malformed = _RetrySequenceSession(_RetryResponse(ValueError("bad json")), _RetryResponse({"late": True}))
    with pytest.raises(ProviderResponseError):
        request_json(
            context=RetrievalContext(deadline=datetime.now(timezone.utc) + timedelta(seconds=1)),
            session=malformed,
            url="https://example.test/snapshot",
            params=None,
            timeout=(3.0, 8.0),
            now=lambda: datetime.now(timezone.utc),
            provider="prizepicks",
            operation="get_snapshot",
            parse=lambda payload: payload,
            error_policy=_TEST_TRANSPORT_POLICY,
        )
    assert malformed.calls == 1


def test_transport_error_policy_is_immutable_and_translates_messages():
    policy = TransportErrorPolicy(
        deadline_message="deadline",
        timeout_message="timeout",
        unavailable_message="unavailable",
        invalid_json_message="invalid json",
    )

    with pytest.raises(AttributeError):
        policy.timeout_message = "changed"  # type: ignore[misc]
