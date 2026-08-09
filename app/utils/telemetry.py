"""Structured telemetry for provider seams and local normalization decisions.

One :class:`ProviderEvent` is emitted per instrumented provider seam.  Most
seams are upstream invocations; adapters may also explicitly instrument a
local normalization seam when no upstream invocation represents a terminal
failure.  The event is written to the application log as a single structured
line and retained in an in-process, bounded, thread-safe buffer so operators
and tests can inspect the contract without a logging backend.

Events carry the provider name, operation, duration, outcome, retry count,
cache status, and the ``request_id`` that correlates them with the incoming
app request.  Credentials, authorization headers, URLs, raw response bodies,
and exception messages are never captured; every event field is one of the
documented scalar slots in :class:`ProviderEvent`.

In addition to the event stream, this module keeps bounded counters that let
operator surfaces separate *provider* failures (observed at the provider
seams) from *application* failures (recorded by the central error handler),
without any error being counted twice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from app.utils.request_id import is_valid_request_id

logger = logging.getLogger(__name__)

PROVIDER_NBA_STATS = "nba_stats"
PROVIDER_PBP_STATS = "pbp_stats"
PROVIDER_DABBLE = "dabble"
PROVIDER_PRIZEPICKS = "prizepicks"
PROVIDER_UNDERDOG = "underdog"

# Keep provider operation names in one place.  These names are part of the
# telemetry contract: adapter methods, provider health checks, and the admin
# metrics surface must not quietly grow parallel spellings for the same
# upstream endpoint.  The recorded-fixture operation is included because it
# deliberately uses the production parser and emits the same event shape.
NBA_STATS_OPERATIONS = frozenset(
    {
        "health_probe",
        "player_game_logs",
        "player_game_logs_recorded",
        "player_roster",
        "player_roster_recorded",
        "league_opponent_team_stats",
        "league_opponent_shot_chart",
        "league_opponent_shooting_zone",
        "synergy_team_play_types",
        "synergy_player_play_types",
        "player_per36_stats",
        "player_shooting_zone",
        "player_shot_chart",
        "player_gamelogs_against",
    }
)
PBP_STATS_OPERATIONS = frozenset(
    {"get_totals_player", "get_totals_opponent", "health_probe"}
)
DABBLE_OPERATIONS = frozenset(
    {
        "competition_lookup",
        "competition_fixtures",
        "fixture_details",
        "snapshot_normalization",
    }
)
DABBLE_UPSTREAM_OPERATIONS = frozenset(
    {"competition_lookup", "competition_fixtures", "fixture_details"}
)
DABBLE_NORMALIZATION_OPERATIONS = frozenset({"snapshot_normalization"})
PRIZEPICKS_OPERATIONS = frozenset({"get_snapshot"})
UNDERDOG_OPERATIONS = frozenset({"get_snapshot"})
PROVIDER_OPERATION_CATALOG = {
    PROVIDER_NBA_STATS: NBA_STATS_OPERATIONS,
    PROVIDER_PBP_STATS: PBP_STATS_OPERATIONS,
    PROVIDER_DABBLE: DABBLE_OPERATIONS,
    PROVIDER_PRIZEPICKS: PRIZEPICKS_OPERATIONS,
    PROVIDER_UNDERDOG: UNDERDOG_OPERATIONS,
}

OUTCOME_SUCCESS = "success"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_MALFORMED = "malformed"
OUTCOME_ERROR = "error"

CACHE_HIT = "hit"
CACHE_MISS = "miss"
CACHE_DISABLED = "disabled"

#: Max events retained in the process-wide buffer; prevents unbounded growth.
EVENT_BUFFER_CAPACITY = 5000

_retry_local = threading.local()
_request_id_local = threading.local()
_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_board_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_buffer_lock = threading.Lock()

_provider_events_total = 0
_board_events_total = 0
_provider_failures: dict[tuple[str, str], int] = {}
_application_failures: dict[str, int] = {}
_cache_counts: dict[str, dict[str, int]] = {}

#: Injectable monotonic and wall-clock functions for deterministic tests.
_monotonic = time.monotonic


def _is_deadline_exceeded(error: BaseException) -> bool:
    """Recognize the shared deadline type without importing providers early."""

    # ``app.errors`` imports this module, while ``app.providers`` re-exports
    # adapters that import ``app.errors``.  Keep this import lazy to avoid
    # creating that package-initialization cycle at module import time.
    from app.providers.dfs import DeadlineExceededError

    return isinstance(error, DeadlineExceededError)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderResponseError(Exception):
    """An upstream provider payload could not be parsed into the expected shape.

    Raising this inside a :class:`ProviderTracker` records the event with the
    ``malformed`` outcome, keeping provider-data problems inside the provider
    failure counters rather than surfacing as unexpected application errors.
    """


@dataclass(frozen=True)
class ProviderEvent:
    """One structured upstream provider event.

    Every field is a scalar so the event can be logged and stored without
    importing provider responses, credentials, or exception messages.
    """

    provider: str
    operation: str
    outcome: str
    started_at: str
    duration_ms: float
    retry_count: int
    cache_status: str
    request_id: str
    status_code: int | None = None


@dataclass(frozen=True)
class BoardTelemetryEvent:
    """Bounded aggregate telemetry for one internal DFS board collection."""

    duration_ms: float
    outcome_counts: tuple[tuple[str, int], ...]
    failure_reason_counts: tuple[tuple[str, int], ...]
    fetched_count: int
    eligible_count: int
    normalized_count: int
    skipped_count: int
    started_at: str
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.duration_ms, (int, float)) or self.duration_ms < 0:
            raise ValueError("board telemetry duration must be non-negative")
        for labels, allowed in (
            (self.outcome_counts, {"complete", "partial", "failed"}),
            (
                self.failure_reason_counts,
                {
                    "timeout",
                    "deadline_exceeded",
                    "rate_limited",
                    "access_denied",
                    "upstream_error",
                    "malformed_response",
                },
            ),
        ):
            if any(
                not isinstance(label, str)
                or not label
                or label not in allowed
                or not isinstance(count, int)
                or count < 1
                for label, count in labels
            ):
                raise ValueError("board telemetry labels must be stable and bounded")
        if any(
            not isinstance(count, int) or count < 0
            for count in (
                self.fetched_count,
                self.eligible_count,
                self.normalized_count,
                self.skipped_count,
            )
        ):
            raise ValueError("board telemetry coverage counts must be non-negative")
        try:
            parsed = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("board telemetry started_at must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("board telemetry started_at must be timezone-aware")
        if self.request_id is not None and not is_valid_request_id(self.request_id):
            raise ValueError("board telemetry request_id is invalid")


class BoardTelemetryRecorder:
    """Typed recorder seam for board aggregates."""

    def record(self, event: BoardTelemetryEvent) -> None:
        raise NotImplementedError


class BoundedBoardTelemetryRecorder(BoardTelemetryRecorder):
    """Record board aggregates in the existing bounded event buffer."""

    def record(self, event: BoardTelemetryEvent) -> None:
        record_board_telemetry(event)


def record_board_telemetry(event: BoardTelemetryEvent) -> None:
    """Record one scalar-only aggregate in the dedicated bounded board buffer."""

    outcome_counts = dict(event.outcome_counts)
    reason_counts = dict(event.failure_reason_counts)
    payload = {
        "started_at": event.started_at,
        "duration_ms": float(event.duration_ms),
        "request_id": event.request_id,
        "outcome_complete": outcome_counts.get("complete", 0),
        "outcome_partial": outcome_counts.get("partial", 0),
        "outcome_failed": outcome_counts.get("failed", 0),
        "failure_timeout": reason_counts.get("timeout", 0),
        "failure_deadline_exceeded": reason_counts.get("deadline_exceeded", 0),
        "failure_rate_limited": reason_counts.get("rate_limited", 0),
        "failure_access_denied": reason_counts.get("access_denied", 0),
        "failure_upstream_error": reason_counts.get("upstream_error", 0),
        "failure_malformed_response": reason_counts.get("malformed_response", 0),
        "fetched_count": event.fetched_count,
        "eligible_count": event.eligible_count,
        "normalized_count": event.normalized_count,
        "skipped_count": event.skipped_count,
    }
    logger.info(
        "board_event outcome_complete=%d outcome_partial=%d outcome_failed=%d duration_ms=%.1f",
        payload["outcome_complete"],
        payload["outcome_partial"],
        payload["outcome_failed"],
        event.duration_ms,
    )
    with _buffer_lock:
        global _board_events_total
        _board_event_buffer.append(payload)
        _board_events_total += 1


def get_recorded_board_events() -> list[dict[str, Any]]:
    """Return a snapshot of dedicated bounded board aggregate events."""

    with _buffer_lock:
        return list(_board_event_buffer)


def snapshot_recent_board_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent bounded board aggregate events."""

    with _buffer_lock:
        return list(_board_event_buffer)[-limit:]


def set_clock(
    monotonic: Callable[[], float] | None = None,
    now_iso: Callable[[], str] | None = None,
) -> None:
    """Override the clocks used by provider events (deterministic tests).

    Pass ``None`` for a dimension to keep the current clock.
    """
    global _monotonic
    if monotonic is not None:
        _monotonic = monotonic
    if now_iso is not None:
        globals()["_now_iso"] = now_iso


def current_request_id() -> str:
    """Return the correlation ID for the current request, if any."""
    try:
        from flask import g, has_request_context

        if has_request_context():
            request_id = getattr(g, "request_id", None)
            if request_id:
                return request_id
    except Exception:
        pass
    return getattr(_request_id_local, "request_id", None) or "-"


def set_request_id(request_id: str) -> None:
    """Bind a correlation ID for the current thread (background workers)."""
    _request_id_local.request_id = request_id


def clear_request_id() -> None:
    """Clear a worker correlation ID so a thread cannot leak it to later work."""
    _request_id_local.__dict__.pop("request_id", None)


def reset_retry_count() -> None:
    _retry_local.count = 0


def increment_retry_count() -> None:
    count = getattr(_retry_local, "count", 0) + 1
    _retry_local.count = count
    progress_callback = getattr(_retry_local, "progress_callback", None)
    if progress_callback is not None:
        progress_callback(count)


def current_retry_count() -> int:
    return getattr(
        _retry_local,
        "count",
        0,
    )


def set_retry_progress_callback(callback: Callable[[int], None]) -> None:
    """Publish retry increments to a caller while its provider worker runs.

    Retry counters are intentionally thread-local so concurrent provider calls
    cannot contaminate each other's telemetry.  A bounded transport worker
    needs one additional, request-local observation point because its caller
    may time out before the worker finishes.  The callback lives on that same
    thread-local state and must be cleared by the worker when it exits.
    """

    _retry_local.progress_callback = callback


def clear_retry_progress_callback() -> None:
    """Remove a worker's retry progress callback from its thread-local state."""

    _retry_local.__dict__.pop("progress_callback", None)


def record_provider_event(event: ProviderEvent) -> None:
    """Record one provider event to the log and the bounded buffer.

    Provider failure counters are incremented here, at the provider seam, so
    the central error handler never needs to recount a provider error as an
    application failure.
    """
    payload = asdict(event)
    logger.info(
        "provider_event provider=%s operation=%s outcome=%s "
        "duration_ms=%.1f retry_count=%d cache_status=%s "
        "status_code=%s request_id=%s",
        event.provider,
        event.operation,
        event.outcome,
        event.duration_ms,
        event.retry_count,
        event.cache_status,
        event.status_code,
        event.request_id,
    )

    with _buffer_lock:
        _event_buffer.append(payload)
        global _provider_events_total, _provider_failures, _cache_counts
        _provider_events_total += 1
        if event.outcome != OUTCOME_SUCCESS:
            key = (event.provider, event.outcome)
            _provider_failures[key] = _provider_failures.get(key, 0) + 1
        per_provider = _cache_counts.setdefault(event.provider, {})
        per_provider[event.cache_status] = (
            per_provider.get(event.cache_status, 0) + 1
        )


def record_cached_provider_event(
    provider: str,
    operation: str,
    cache_status: str = CACHE_HIT,
) -> None:
    """Record an event for a response served entirely from the cache.

    Cache hits never reach an upstream provider, so no patience context
    manager is involved;  a zero-duration event keeps cache behaviour
    observable on the same shape as every other provider event.  Any retries
    accrued by an earlier call on this thread must not leak into the cache-hit
    event, so the counter is reset and the event reports ``retry_count=0``.
    """
    reset_retry_count()
    record_provider_event(
        ProviderEvent(
            provider=provider,
            operation=operation,
            outcome=OUTCOME_SUCCESS,
            started_at=_now_iso(),
            duration_ms=0.0,
            retry_count=current_retry_count(),
            cache_status=cache_status,
            request_id=current_request_id(),
            status_code=None,
        )
    )


def record_application_failure(code: str) -> None:
    """Count one application failure from the central error handling layer.

    Called with an internal failure code only;  provider failures are counted
    by :func:`record_provider_event` and must not reach this counter.
    """
    with _buffer_lock:
        _application_failures[code] = _application_failures.get(code, 0) + 1


def get_recorded_provider_events() -> list[dict[str, Any]]:
    """Return a snapshot of provider events captured in this process."""
    with _buffer_lock:
        return list(_event_buffer)


def snapshot_metrics() -> dict[str, Any]:
    """Return a bounded snapshot of the telemetry counters."""
    with _buffer_lock:
        provider_failures: dict[str, dict[str, int]] = {}
        for (provider, outcome), count in _provider_failures.items():
            provider_failures.setdefault(provider, {})[outcome] = count
        cache_counts = {
            provider: dict(per_status)
            for provider, per_status in _cache_counts.items()
        }
        return {
            "provider_events_total": _provider_events_total,
            "board_events_total": _board_events_total,
            "board_buffered_events": len(_board_event_buffer),
            "board_buffered_capacity": EVENT_BUFFER_CAPACITY,
            "provider_failures": provider_failures,
            "application_failures": dict(_application_failures),
            "cache": cache_counts,
            "buffered_events": len(_event_buffer),
            "buffered_capacity": EVENT_BUFFER_CAPACITY,
        }


def snapshot_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` provider events from the buffer."""
    with _buffer_lock:
        return list(_event_buffer)[-limit:]


def clear_recorded_provider_events() -> None:
    """Clear the recording buffer and counters (tests/operators).

    Resetting the counters and the buffer together keeps telemetry snapshots
    deterministic between isolated test cases.
    """
    global _provider_events_total, _board_events_total, _provider_failures, _application_failures, _cache_counts
    with _buffer_lock:
        _event_buffer.clear()
        _board_event_buffer.clear()
        _provider_events_total = 0
        _board_events_total = 0
        _provider_failures = {}
        _application_failures = {}
        _cache_counts = {}


class ProviderTracker:
    """Context manager that records one :class:`ProviderEvent` around a seam.

    Enclosing code can set ``tracker.status_code`` when an upstream response is
    available so the event reflects the HTTP status rather than only an
    exception type.
    """

    def __init__(
        self,
        provider: str,
        operation: str,
        *,
        cache_status: str = CACHE_MISS,
        request_id: str | None = None,
    ) -> None:
        if request_id is not None and not is_valid_request_id(request_id):
            raise ValueError("provider request_id is invalid")
        self.provider = provider
        self.operation = operation
        self.cache_status = cache_status
        self.request_id = request_id
        self.status_code: int | None = None

    def __enter__(self) -> "ProviderTracker":
        reset_retry_count()
        self.status_code = None
        self._started = _monotonic()
        self._started_at = _now_iso()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        duration_ms = (_monotonic() - self._started) * 1000.0
        outcome = OUTCOME_SUCCESS
        if exc is not None:
            if isinstance(exc, ProviderResponseError):
                outcome = OUTCOME_MALFORMED
            elif _is_deadline_exceeded(exc):
                outcome = OUTCOME_TIMEOUT
            elif isinstance(exc, requests.exceptions.Timeout):
                outcome = OUTCOME_TIMEOUT
            elif isinstance(exc, requests.exceptions.RequestException):
                outcome = OUTCOME_HTTP_ERROR
            else:
                outcome = OUTCOME_ERROR

        record_provider_event(
            ProviderEvent(
                provider=self.provider,
                operation=self.operation,
                outcome=outcome,
                started_at=self._started_at,
                duration_ms=duration_ms,
                retry_count=current_retry_count(),
                cache_status=self.cache_status,
                request_id=(
                    self.request_id
                    if self.request_id is not None
                    else current_request_id()
                ),
                status_code=self.status_code,
            )
        )
        return False


def provider_call(
    provider: str,
    operation: str,
    *,
    cache_status: str = CACHE_MISS,
    request_id: str | None = None,
) -> ProviderTracker:
    """Instrument one upstream provider invocation.

    Usage::

        with provider_call(PROVIDER_NBA_STATS, "get_game_logs") as tracker:
            tracker.status_code = ...
            result = endpoint.get_data_frames()[0]
    """
    return ProviderTracker(
        provider,
        operation,
        cache_status=cache_status,
        request_id=request_id,
    )


def provider_normalization_call(
    provider: str,
    operation: str,
    *,
    cache_status: str = CACHE_MISS,
    request_id: str | None = None,
) -> ProviderTracker:
    """Instrument an explicit local provider-normalization seam.

    The event and counter shape intentionally matches :func:`provider_call`.
    Adapters decide locally whether this seam is needed, so an upstream
    malformed or timeout event is never recounted as a normalization failure.
    """

    return ProviderTracker(
        provider,
        operation,
        cache_status=cache_status,
        request_id=request_id,
    )


__all__ = [
    "CACHE_DISABLED",
    "CACHE_HIT",
    "CACHE_MISS",
    "EVENT_BUFFER_CAPACITY",
    "OUTCOME_ERROR",
    "OUTCOME_HTTP_ERROR",
    "OUTCOME_MALFORMED",
    "OUTCOME_SUCCESS",
    "OUTCOME_TIMEOUT",
    "PROVIDER_NBA_STATS",
    "PROVIDER_PBP_STATS",
    "PROVIDER_DABBLE",
    "DABBLE_OPERATIONS",
    "DABBLE_UPSTREAM_OPERATIONS",
    "DABBLE_NORMALIZATION_OPERATIONS",
    "PROVIDER_PRIZEPICKS",
    "PROVIDER_UNDERDOG",
    "NBA_STATS_OPERATIONS",
    "PBP_STATS_OPERATIONS",
    "PRIZEPICKS_OPERATIONS",
    "UNDERDOG_OPERATIONS",
    "PROVIDER_OPERATION_CATALOG",
    "ProviderEvent",
    "BoardTelemetryEvent",
    "BoardTelemetryRecorder",
    "BoundedBoardTelemetryRecorder",
    "get_recorded_board_events",
    "record_board_telemetry",
    "snapshot_recent_board_events",
    "ProviderResponseError",
    "ProviderTracker",
    "clear_recorded_provider_events",
    "current_request_id",
    "current_retry_count",
    "set_retry_progress_callback",
    "clear_retry_progress_callback",
    "get_recorded_provider_events",
    "increment_retry_count",
    "provider_call",
    "provider_normalization_call",
    "record_application_failure",
    "record_cached_provider_event",
    "record_provider_event",
    "reset_retry_count",
    "set_clock",
    "set_request_id",
    "clear_request_id",
    "snapshot_metrics",
    "snapshot_recent_events",
]
