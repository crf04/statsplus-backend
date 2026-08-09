"""Deadline behavior shared by the DFS provider adapters."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ProviderUnavailableError
from app.providers.dabble import DabbleAdapter
from app.providers.dfs import NBAMarketQuery, RetrievalContext
from app.providers.prizepicks import PrizePicksAdapter
from app.providers.underdog import UnderdogAdapter
from app.utils import telemetry


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
