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
    clear_retry_progress_callback,
    current_retry_count,
    increment_retry_count,
    provider_call,
    set_retry_progress_callback,
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
        self.value: Any = None
        self.status_code: int | None = None
        self.error: BaseException | None = None
        self.retry_count = 0
        self._retry_lock = threading.Lock()
        self._retry_progress: list[tuple[float, int]] = []
        self.completed_at: float | None = None

    def record_retry_progress(self, count: int) -> None:
        """Record one worker retry without touching the caller's thread-local."""

        with self._retry_lock:
            self.retry_count = max(self.retry_count, count)
            self._retry_progress.append((time.monotonic(), count))

    def retry_count_at(self, deadline: float) -> int:
        """Return retries observed by the absolute monotonic deadline."""

        with self._retry_lock:
            return max(
                (count for timestamp, count in self._retry_progress if timestamp <= deadline),
                default=0,
            )


def _run_request(
    result: _RequestResult,
    session: requests.Session | Any,
    url: str,
    params: Mapping[str, Any] | None,
    timeout: tuple[float, float],
    context: RetrievalContext,
    now: Callable[[], datetime],
    parse: Callable[[Any], _Result],
    invalid_json_message: str,
) -> None:
    """Run the complete potentially blocking response pipeline in a daemon."""

    try:
        # Requests retry hooks increment a thread-local counter.  Publish each
        # increment to this request's result as it happens so a caller that
        # reaches its deadline can report progress before this worker finishes.
        set_retry_progress_callback(result.record_retry_progress)
        if params is None:
            response = session.get(url, timeout=timeout)
        else:
            response = session.get(url, params=params, timeout=timeout)
        result.status_code = getattr(response, "status_code", None)
        context.ensure_active(now=now())
        response.raise_for_status()
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise ProviderResponseError(invalid_json_message) from error
        try:
            result.value = parse(payload)
        except MalformedProviderResponseError as error:
            raise ProviderResponseError(str(error)) from error
        context.ensure_active(now=now())
    except BaseException as error:  # propagate provider failures to the caller
        result.error = error
    finally:
        clear_retry_progress_callback()
        result.completed_at = time.monotonic()
        _request_slots.release()
        result.done.set()


def _run_callable(result: _RequestResult, call: Callable[[], _Result]) -> None:
    """Run non-transport provider work in the same bounded daemon pool."""

    try:
        result.value = call()
    except BaseException as error:  # propagate implementation failures
        result.error = error
    finally:
        result.completed_at = time.monotonic()
        _request_slots.release()
        result.done.set()


def run_bounded(
    *,
    context: RetrievalContext,
    now: Callable[[], datetime],
    call: Callable[[], _Result],
) -> _Result:
    """Run a potentially blocking provider pipeline under an absolute deadline.

    The bounded daemon worker is deliberately given no mutable caller-owned
    accumulator.  If it outlives the deadline, its result and any intermediate
    state remain isolated and are discarded by the caller.
    """

    monotonic_start = time.monotonic()
    current = now()
    context.ensure_active(now=current)
    remaining = context.remaining_seconds(now=current)
    monotonic_deadline = monotonic_start + remaining
    slot_wait = max(0.0, monotonic_deadline - time.monotonic())
    if not _request_slots.acquire(timeout=slot_wait):
        raise DeadlineExceededError("provider retrieval deadline exceeded")

    result = _RequestResult()
    try:
        worker = threading.Thread(
            target=_run_callable,
            args=(result, call),
            daemon=True,
            name="statsplus-provider-pipeline",
        )
        worker.start()
    except BaseException:
        _request_slots.release()
        raise

    remaining = max(0.0, monotonic_deadline - time.monotonic())
    if not result.done.wait(timeout=remaining):
        raise DeadlineExceededError("provider retrieval deadline exceeded")
    if (
        result.completed_at is None
        or result.completed_at > monotonic_deadline
        or time.monotonic() >= monotonic_deadline
    ):
        raise DeadlineExceededError("provider retrieval deadline exceeded")
    context.ensure_active(now=now())
    if result.error is not None:
        raise result.error
    return result.value


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
    session may ignore or reinterpret those two phases.  The worker owns the
    complete response pipeline (status handling, JSON decoding, and provider
    parsing), so late pipeline work cannot block the caller or be returned.
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
                    args=(
                        result,
                        session,
                        url,
                        params,
                        bounded,
                        context,
                        now,
                        parse,
                        invalid_json_message,
                    ),
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
                tracker.status_code = result.status_code
                retry_count = result.retry_count_at(monotonic_deadline)
                for _ in range(max(0, retry_count - current_retry_count())):
                    increment_retry_count()
                raise DeadlineExceededError(
                    f"{provider} retrieval deadline exceeded"
                )

            # The event may be set at the boundary while the caller is being
            # scheduled.  Accept only a result completed before the absolute
            # deadline and while the caller still has time to process it.
            tracker.status_code = result.status_code
            retry_count = result.retry_count_at(monotonic_deadline)
            for _ in range(max(0, retry_count - current_retry_count())):
                increment_retry_count()

            if (
                result.completed_at is None
                or result.completed_at > monotonic_deadline
                or time.monotonic() >= monotonic_deadline
            ):
                raise DeadlineExceededError(
                    f"{provider} retrieval deadline exceeded"
                )

            if result.error is not None:
                raise result.error
            return result.value
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


__all__ = ["bounded_timeout", "request_json", "run_bounded"]
