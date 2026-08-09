"""Shared transport seam for deadline-bounded DFS snapshot requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from dataclasses import dataclass
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
    ProviderResponseError,
    clear_retry_progress_callback,
    current_retry_count,
    increment_retry_count,
    provider_call,
    set_retry_progress_callback,
)

_Result = TypeVar("_Result")
_FailureFactory = Callable[[str, Exception], Exception]
_Worker = Callable[["_RequestResult"], None]
_ResultObserver = Callable[["_RequestResult", float], None]
_Preparation = Callable[[float], Callable[[], None] | None]


@dataclass(frozen=True, slots=True)
class TransportErrorPolicy:
    """Immutable public messages and translation for one transport seam."""

    deadline_message: str
    timeout_message: str
    unavailable_message: str
    invalid_json_message: str
    failure_factory: _FailureFactory | None = None

# A Requests timeout is advisory to the implementation supplied by the
# caller.  A bounded set of daemon workers gives us a hard caller-side escape
# hatch even when that implementation ignores both timeout phases.  Workers
# that are stuck in an in-flight socket remain bounded and cannot keep the
# interpreter alive during shutdown.
_MAX_IN_FLIGHT_REQUESTS = 32
_request_slots = threading.BoundedSemaphore(_MAX_IN_FLIGHT_REQUESTS)
_MAX_CONNECT_TIMEOUT_SECONDS = 3.0
_MAX_READ_TIMEOUT_SECONDS = 8.0
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class _RequestResult:
    """Result holder shared by one caller and its bounded daemon worker."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.status_code: int | None = None
        self.error: BaseException | None = None
        self.retry_count = 0
        self._retry_lock = threading.Lock()
        self._retry_progress: list[tuple[float, int]] = []
        self.completed_at: float | None = None
        self._monotonic = monotonic

    def record_retry_progress(self, count: int) -> None:
        """Record one worker retry without touching the caller's thread-local."""

        with self._retry_lock:
            self.retry_count = max(self.retry_count, count)
            self._retry_progress.append((self._monotonic(), count))

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
    request_get: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run the complete potentially blocking response pipeline in a daemon."""

    try:
        # Requests retry hooks increment a thread-local counter.  Publish each
        # increment to this request's result as it happens so a caller that
        # reaches its deadline can report progress before this worker finishes.
        set_retry_progress_callback(result.record_retry_progress)
        get = request_get or session.get
        response: Any | None = None
        # The adapter contract permits one and only one safe GET retry.  Do
        # this in the bounded worker so retry time is charged to the same
        # absolute RetrievalContext deadline as the initial request.
        for attempt in range(2):
            context.ensure_active(now=now())
            remaining = context.remaining_seconds(now=now())
            if remaining <= 0:
                raise DeadlineExceededError("provider retrieval deadline exceeded")
            attempt_timeout = (
                min(float(timeout[0]), remaining),
                min(float(timeout[1]), remaining),
            )
            try:
                if params is None:
                    response = get(url, timeout=attempt_timeout)
                else:
                    response = get(url, params=params, timeout=attempt_timeout)
            except requests.exceptions.Timeout:
                if attempt == 0:
                    increment_retry_count()
                    continue
                raise
            result.status_code = getattr(response, "status_code", None)
            status_code = result.status_code
            if attempt == 0 and status_code in _RETRYABLE_STATUS_CODES:
                increment_retry_count()
                continue
            break
        if response is None:  # pragma: no cover - every loop branch returns/raises
            raise requests.exceptions.RequestException("provider returned no response")
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
        result.completed_at = monotonic()
        _request_slots.release()
        result.done.set()


def _run_callable(
    result: _RequestResult,
    call: Callable[[], _Result],
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run non-transport provider work in the same bounded daemon pool."""

    try:
        result.value = call()
    except BaseException as error:  # propagate implementation failures
        result.error = error
    finally:
        result.completed_at = monotonic()
        _request_slots.release()
        result.done.set()


def _run_bounded(
    *,
    context: RetrievalContext,
    now: Callable[[], datetime],
    worker: _Worker,
    worker_name: str,
    deadline_message: str = "provider retrieval deadline exceeded",
    observe_result: _ResultObserver | None = None,
    prepare: _Preparation | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> _RequestResult:
    """Own one bounded worker lifecycle for every provider pipeline.

    The worker receives only a private result holder.  If it outlives the
    deadline, its result and any intermediate state remain isolated and are
    discarded by the caller.  ``observe_result`` runs at the two points where
    the caller can harvest worker progress: after timeout and after completion.
    Keeping those points here prevents transport and non-transport callers from
    growing separate slot, thread, and deadline implementations.
    """

    monotonic_start = monotonic()
    current = now()
    context.ensure_active(now=current)
    remaining = context.remaining_seconds(now=current)
    monotonic_deadline = monotonic_start + remaining
    retry_baseline = current_retry_count()
    prepared_release: Callable[[], None] | None = None
    slot_acquired = False
    try:
        if prepare is not None:
            # A provider-specific serialized client can wait for its own
            # access before this caller reserves a shared transport slot.
            # The absolute monotonic deadline is intentionally separate from
            # Requests' (connect, read) timeout tuple.
            prepared_release = prepare(monotonic_deadline)
        if monotonic() >= monotonic_deadline:
            raise DeadlineExceededError(deadline_message)
        slot_wait = max(0.0, monotonic_deadline - monotonic())
        if not _request_slots.acquire(timeout=slot_wait):
            raise DeadlineExceededError(deadline_message)
        slot_acquired = True

        result = _RequestResult(monotonic=monotonic)

        def run_prepared(holder: _RequestResult) -> None:
            try:
                worker(holder)
            finally:
                if holder.completed_at is None:
                    holder.completed_at = monotonic()
                if prepared_release is not None:
                    prepared_release()

        worker_thread = threading.Thread(
            target=run_prepared,
            args=(result,),
            daemon=True,
            name=worker_name,
        )
        worker_thread.start()
    except BaseException:
        if slot_acquired:
            _request_slots.release()
        if prepared_release is not None:
            prepared_release()
        raise

    def observe() -> None:
        """Harvest worker retry progress before exposing its result."""

        retry_count = result.retry_count_at(monotonic_deadline)
        for _ in range(max(0, retry_count - retry_baseline)):
            increment_retry_count()
        if observe_result is not None:
            observe_result(result, monotonic_deadline)

    remaining = max(0.0, monotonic_deadline - monotonic())
    if not result.done.wait(timeout=remaining):
        observe()
        raise DeadlineExceededError(deadline_message)

    observe()
    if (
        result.completed_at is None
        or result.completed_at > monotonic_deadline
    ):
        raise DeadlineExceededError(deadline_message)
    if result.error is not None:
        raise result.error
    return result


def run_bounded(
    *,
    context: RetrievalContext,
    now: Callable[[], datetime],
    call: Callable[[], _Result],
    monotonic: Callable[[], float] = time.monotonic,
) -> _Result:
    """Run a potentially blocking provider pipeline under an absolute deadline."""

    result = _run_bounded(
        context=context,
        now=now,
        worker=lambda holder: _run_callable(holder, call, monotonic=monotonic),
        worker_name="statsplus-provider-pipeline",
        monotonic=monotonic,
    )
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
    return (
        min(float(connect), _MAX_CONNECT_TIMEOUT_SECONDS, remaining),
        min(float(read), _MAX_READ_TIMEOUT_SECONDS, remaining),
    )


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
    error_policy: TransportErrorPolicy,
    monotonic: Callable[[], float] | None = None,
) -> _Result:
    """Execute one instrumented JSON request under an absolute deadline.

    The Requests ``(connect, read)`` timeout is still passed through so normal
    sessions retain their retry and socket behavior.  The caller also waits
    on a bounded daemon worker using a monotonic deadline, because a custom
    session may ignore or reinterpret those two phases.  The worker owns the
    complete response pipeline (status handling, JSON decoding, and provider
    parsing), so late pipeline work cannot block the caller or be returned.
    """

    request_monotonic = monotonic or time.monotonic
    try:
        current = now()
        bounded = bounded_timeout(
            context,
            timeout,
            now=current,
            provider=provider,
        )
        with provider_call(
            provider,
            operation,
            cache_status=context.cache_status,
            request_id=context.request_id,
        ) as tracker:

            request_lease: Any | None = None
            acquire_request = getattr(session, "acquire_request", None)

            def prepare_request(deadline: float) -> Callable[[], None] | None:
                """Acquire an optional serialized client before transport slots."""

                nonlocal request_lease
                if not callable(acquire_request):
                    return None
                request_lease = acquire_request(deadline)
                release = getattr(request_lease, "release", None)
                request = getattr(request_lease, "get", None)
                if not callable(release) or not callable(request):
                    if callable(release):
                        release()
                    raise TypeError("request lease must expose get() and release()")

                def release_request() -> None:
                    current_lease = request_lease
                    if current_lease is not None:
                        current_release = getattr(current_lease, "release", None)
                        if callable(current_release):
                            current_release()

                return release_request

            def get_request(url_value: str, **kwargs: Any) -> Any:
                """Use the lease when a session supplies the safe contract."""

                nonlocal request_lease
                if request_lease is None:
                    return session.get(url_value, **kwargs)
                if getattr(request_lease, "_released", False):
                    # Serialized sessions release their lock immediately after
                    # each response so response decoding does not block other
                    # detail workers.  A retry therefore reserves a fresh
                    # lease, still bounded by the same absolute context.
                    request_lease = acquire_request(
                        request_monotonic()
                        + context.remaining_seconds(now=now())
                    )
                return request_lease.get(url_value, **kwargs)

            def observe_result(result: _RequestResult, _deadline: float) -> None:
                """Move worker-only response facts into telemetry."""

                tracker.status_code = result.status_code

            result = _run_bounded(
                context=context,
                now=now,
                worker=lambda holder: _run_request(
                    holder,
                    session,
                    url,
                    params,
                    bounded,
                    context,
                    now,
                    parse,
                    error_policy.invalid_json_message,
                    request_get=get_request,
                    monotonic=request_monotonic,
                ),
                worker_name=f"statsplus-{provider}-request",
                deadline_message=f"{provider} retrieval deadline exceeded",
                observe_result=observe_result,
                prepare=prepare_request,
                monotonic=request_monotonic,
            )
            return result.value
    except DeadlineExceededError as error:
        if error_policy.failure_factory is not None:
            raise error_policy.failure_factory("deadline_exceeded", error) from error
        raise ProviderUnavailableError(error_policy.deadline_message, detail=error) from error
    except requests.exceptions.Timeout as error:
        if error_policy.failure_factory is not None:
            raise error_policy.failure_factory("timeout", error) from error
        raise ProviderUnavailableError(error_policy.timeout_message, detail=error) from error
    except requests.exceptions.HTTPError as error:
        if error_policy.failure_factory is not None:
            raise error_policy.failure_factory("http_error", error) from error
        raise ProviderUnavailableError(
            error_policy.unavailable_message,
            detail=error,
        ) from error
    except requests.exceptions.RequestException as error:
        if error_policy.failure_factory is not None:
            raise error_policy.failure_factory("request_error", error) from error
        raise ProviderUnavailableError(
            error_policy.unavailable_message,
            detail=error,
        ) from error


__all__ = [
    "TransportErrorPolicy",
    "bounded_timeout",
    "request_json",
    "run_bounded",
]
