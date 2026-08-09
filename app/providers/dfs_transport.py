"""Shared transport seam for deadline-bounded DFS snapshot requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import threading
import time
from typing import Any, TypeVar

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    DeadlineExceededError,
    MalformedProviderResponseError,
    RetrievalContext,
)
from app.utils.telemetry import (
    CACHE_DISABLED,
    ProviderResponseError,
    current_retry_count,
    increment_retry_count,
    provider_call,
)

_Result = TypeVar("_Result")
_FailureFactory = Callable[[str, Exception], Exception]

# A Requests timeout is advisory to the implementation supplied by the
# caller.  A bounded set of daemon workers gives us a hard caller-side escape
# hatch even when that implementation ignores both timeout phases.  Workers
# that are stuck in an in-flight socket remain bounded and cannot keep the
# interpreter alive during shutdown.
_MAX_IN_FLIGHT_REQUESTS = 32
_request_slots = threading.BoundedSemaphore(_MAX_IN_FLIGHT_REQUESTS)


class _RequestResult:
    """Result holder shared by one caller and its bounded daemon worker."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: Any = None
        self.error: BaseException | None = None
        self.retry_count = 0
        self.completed_at: float | None = None


def _run_request(
    result: _RequestResult,
    session: requests.Session | Any,
    url: str,
    params: Mapping[str, Any] | None,
    timeout: tuple[float, float],
) -> None:
    """Run only the potentially blocking network operation in a daemon."""

    try:
        if params is None:
            result.response = session.get(url, timeout=timeout)
        else:
            result.response = session.get(url, params=params, timeout=timeout)
    except BaseException as error:  # propagate provider failures to the caller
        result.error = error
    finally:
        # Retry counters are thread-local.  Carry the worker's count back to
        # the provider-call thread before the telemetry context closes.
        result.retry_count = current_retry_count()
        result.completed_at = time.monotonic()
        _request_slots.release()
        result.done.set()


def bounded_timeout(
    context: RetrievalContext,
    timeout: tuple[float, float],
    *,
    now: datetime,
    provider: str,
) -> tuple[float, float]:
    """Cap connect/read timeouts by the remaining absolute retrieval budget."""

    context.ensure_active(now=now)
    remaining = context.remaining_seconds(now=now)
    if remaining <= 0:
        raise DeadlineExceededError(f"{provider} retrieval deadline exceeded")
    connect, read = timeout
    return min(float(connect), remaining), min(float(read), remaining)


def request_json(
    *,
    context: RetrievalContext,
    session: requests.Session | Any,
    url: str,
    params: Mapping[str, Any] | None,
    timeout: tuple[float, float],
    now: Callable[[], datetime],
    provider: str,
    operation: str,
    parse: Callable[[Any], _Result],
    deadline_message: str,
    timeout_message: str,
    unavailable_message: str,
    invalid_json_message: str,
    failure_factory: _FailureFactory | None = None,
) -> _Result:
    """Execute one instrumented JSON request under an absolute deadline.

    The Requests ``(connect, read)`` timeout is still passed through so normal
    sessions retain their retry and socket behavior.  The caller also waits
    on a bounded daemon worker using a monotonic deadline, because a custom
    session may ignore or reinterpret those two phases.  Late responses are
    never parsed or returned.
    """

    try:
        monotonic_start = time.monotonic()
        current = now()
        bounded = bounded_timeout(
            context,
            timeout,
            now=current,
            provider=provider,
        )
        remaining = context.remaining_seconds(now=current)
        monotonic_deadline = monotonic_start + remaining
        with provider_call(
            provider,
            operation,
            cache_status=CACHE_DISABLED,
            request_id=context.request_id,
        ) as tracker:
            slot_wait = max(0.0, monotonic_deadline - time.monotonic())
            if not _request_slots.acquire(timeout=slot_wait):
                raise DeadlineExceededError(
                    f"{provider} retrieval deadline exceeded"
                )

            result = _RequestResult()
            try:
                worker = threading.Thread(
                    target=_run_request,
                    args=(result, session, url, params, bounded),
                    daemon=True,
                    name=f"statsplus-{provider}-request",
                )
                worker.start()
            except BaseException:
                # ``_run_request`` releases the slot after it starts.  A
                # thread-start failure owns no worker and must release here.
                _request_slots.release()
                raise

            remaining = max(0.0, monotonic_deadline - time.monotonic())
            if not result.done.wait(timeout=remaining):
                raise DeadlineExceededError(
                    f"{provider} retrieval deadline exceeded"
                )

            # The event may be set at the boundary while the caller is being
            # scheduled.  Accept only a result completed before the absolute
            # deadline and while the caller still has time to process it.
            if (
                result.completed_at is None
                or result.completed_at > monotonic_deadline
                or time.monotonic() >= monotonic_deadline
            ):
                raise DeadlineExceededError(
                    f"{provider} retrieval deadline exceeded"
                )

            for _ in range(result.retry_count):
                increment_retry_count()
            if result.error is not None:
                raise result.error

            response = result.response
            tracker.status_code = getattr(response, "status_code", None)
            context.ensure_active(now=now())
            response.raise_for_status()
            try:
                payload = response.json()
            except (TypeError, ValueError) as error:
                raise ProviderResponseError(invalid_json_message) from error
            try:
                result = parse(payload)
            except MalformedProviderResponseError as error:
                raise ProviderResponseError(str(error)) from error
            context.ensure_active(now=now())
            return result
    except DeadlineExceededError as error:
        if failure_factory is not None:
            raise failure_factory("deadline_exceeded", error) from error
        raise ProviderUnavailableError(deadline_message, detail=error) from error
    except requests.exceptions.Timeout as error:
        if failure_factory is not None:
            raise failure_factory("timeout", error) from error
        raise ProviderUnavailableError(timeout_message, detail=error) from error
    except requests.exceptions.HTTPError as error:
        if failure_factory is not None:
            raise failure_factory("http_error", error) from error
        raise ProviderUnavailableError(unavailable_message, detail=error) from error
    except requests.exceptions.RequestException as error:
        if failure_factory is not None:
            raise failure_factory("request_error", error) from error
        raise ProviderUnavailableError(unavailable_message, detail=error) from error


__all__ = ["bounded_timeout", "request_json"]
