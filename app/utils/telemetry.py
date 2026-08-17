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
import math
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
PROVIDER_ROTOWIRE = "rotowire"

# Keep provider operation names in one place.  These names are part of the
# telemetry contract: adapter methods, provider health checks, and the admin
# metrics surface must not quietly grow parallel spellings for the same
# upstream endpoint.  The recorded-fixture operation is included because it
# deliberately uses the production parser and emits the same event shape.
NBA_STATS_OPERATIONS = frozenset(
    {
        "health_probe",
        "player_game_logs",
        "player_game_logs_season",
        "player_game_logs_recorded",
        "player_diets_recorded",
        "player_roster",
        "player_roster_recorded",
        "league_opponent_team_stats",
        "league_opponent_shot_chart",
        "league_opponent_shooting_zone",
        "team_game_log",
        "league_player_shot_type",
        "synergy_team_play_types",
        "synergy_player_play_types",
        "player_per36_stats",
        "player_shooting_zone",
        "player_shot_chart",
        "player_gamelogs_against",
        "schedule_whole_season",
    }
)
PBP_STATS_OPERATIONS = frozenset(
    {
        "get_totals_player",
        "get_totals_player_diet",
        "get_totals_opponent",
        "health_probe",
        "player_game_logs",
        "game_player_stats",
    }
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
ROTOWIRE_OPERATIONS = frozenset({"get_injuries"})
PROVIDER_OPERATION_CATALOG = {
    PROVIDER_NBA_STATS: NBA_STATS_OPERATIONS,
    PROVIDER_PBP_STATS: PBP_STATS_OPERATIONS,
    PROVIDER_DABBLE: DABBLE_OPERATIONS,
    PROVIDER_PRIZEPICKS: PRIZEPICKS_OPERATIONS,
    PROVIDER_UNDERDOG: UNDERDOG_OPERATIONS,
    PROVIDER_ROTOWIRE: ROTOWIRE_OPERATIONS,
}

OUTCOME_SUCCESS = "success"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_MALFORMED = "malformed"
OUTCOME_ERROR = "error"

CACHE_HIT = "hit"
CACHE_MISS = "miss"
CACHE_DISABLED = "disabled"
CACHE_STALE = "stale"
CACHE_ERROR = "error"

#: Max events retained in the process-wide buffer; prevents unbounded growth.
EVENT_BUFFER_CAPACITY = 5000

_retry_local = threading.local()
_request_id_local = threading.local()
_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_board_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_board_request_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_player_pool_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_player_game_log_event_buffer: deque[dict[str, Any]] = deque(
    maxlen=EVENT_BUFFER_CAPACITY
)
_injury_event_buffer: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER_CAPACITY)
_buffer_lock = threading.Lock()

_provider_events_total = 0
_board_events_total = 0
_board_request_events_total = 0
_player_pool_events_total = 0
_player_game_log_events_total = 0
_injury_events_total = 0
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


@dataclass(frozen=True)
class PlayerPoolTelemetryEvent:
    """Bounded counts for markets dropped while building a Player Pool."""

    unknown_stat_label_count: int
    unjoined_athlete_count: int
    unjoined_event_count: int
    team_mismatch_count: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.unknown_stat_label_count,
                self.unjoined_athlete_count,
                self.unjoined_event_count,
                self.team_mismatch_count,
            )
        ):
            raise ValueError("player pool telemetry counts must be non-negative integers")


@dataclass(frozen=True)
class PlayerGameLogTelemetryEvent:
    """Scalar-only coverage counts for one player-game-log refresh."""

    source_row_count: int
    published_row_count: int
    unjoined_athlete_count: int
    unjoined_event_count: int
    team_mismatch_count: int
    unsupported_phase_count: int = 0
    malformed_row_count: int = 0
    rejected_publication_count: int = 0
    duplicate_row_count: int = 0
    recovered_shrink_row_count: int = 0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.source_row_count,
                self.published_row_count,
                self.unjoined_athlete_count,
                self.unjoined_event_count,
                self.team_mismatch_count,
                self.unsupported_phase_count,
                self.malformed_row_count,
                self.rejected_publication_count,
                self.duplicate_row_count,
                self.recovered_shrink_row_count,
            )
        ):
            raise ValueError(
                "player game log telemetry counts must be non-negative integers"
            )


@dataclass(frozen=True)
class InjuryTelemetryEvent:
    """Bounded reconciliation and board-conflict counts for one report read."""

    unmatched_entry_count: int
    unresolved_team_entry_count: int
    board_conflict_count: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.unmatched_entry_count,
                self.unresolved_team_entry_count,
                self.board_conflict_count,
            )
        ):
            raise ValueError("injury telemetry counts must be non-negative integers")


#: Closed label vocabularies for one published board read.  Everything an
#: operator can group a board request by is one of these strings: no athlete,
#: event, market, selection, or provider-source ID, and no upstream text, can
#: become a metric dimension.
#: Every outcome one authenticated board request can end in, and the single
#: HTTP status each of them means.  The pairing is closed in both directions, so
#: a dashboard can count statuses by outcome without two names for one thing:
#: an ``unavailable`` recorded as a 500, or a 404 recorded as ``served``, is a
#: recording bug rather than an operational signal, and it fails where it is
#: written.
BOARD_REQUEST_STATUSES = {
    "served": 200,
    "not_modified": 304,
    "invalid": 400,
    "too_large": 400,
    "disabled": 404,
    "error": 500,
    "unavailable": 503,
}
BOARD_REQUEST_OUTCOMES = frozenset(BOARD_REQUEST_STATUSES)
BOARD_PROVIDER_STATUSES = frozenset({"complete", "partial", "failed"})
BOARD_FAILURE_REASONS = frozenset(
    {
        "timeout",
        "deadline_exceeded",
        "rate_limited",
        "access_denied",
        "upstream_error",
        "malformed_response",
    }
)
BOARD_FRESHNESS_STATES = frozenset({"fresh", "stale", "unknown"})
BOARD_CACHE_STATES = frozenset(
    {CACHE_HIT, CACHE_MISS, CACHE_DISABLED, CACHE_STALE, CACHE_ERROR, "unset"}
)
BOARD_AVAILABILITY_STATES = frozenset(
    {
        "available",
        "catalog_missing",
        "catalog_stale",
        "catalog_not_configured",
        # A read refused before a board existed reports no catalog state.
        "unknown",
    }
)


@dataclass(frozen=True)
class BoardRequestEvent:
    """Bounded aggregate telemetry for one published DFS Board request.

    It carries latency, the HTTP outcome, the provider outcomes and reasons
    behind it, the cache and freshness states the board was served from, and
    the coverage counts -- all as counts over closed label vocabularies.
    """

    duration_ms: float
    outcome: str
    status_code: int
    comparison_availability: str
    provider_status_counts: tuple[tuple[str, int], ...] = ()
    failure_reason_counts: tuple[tuple[str, int], ...] = ()
    freshness_counts: tuple[tuple[str, int], ...] = ()
    cache_counts: tuple[tuple[str, int], ...] = ()
    group_count: int = 0
    market_count: int = 0
    unresolved_count: int = 0
    disabled_provider_count: int = 0
    started_at: str = ""
    request_id: str | None = None

    def __post_init__(self) -> None:
        # A duration is measured, and a count is counted.  A boolean is neither,
        # however willingly Python treats it as one, and a NaN or an infinity is
        # not a latency an operator can average -- so each of them is refused
        # here rather than reaching a buffer, a log line, or a dashboard.
        if isinstance(self.duration_ms, bool) or not isinstance(
            self.duration_ms, (int, float)
        ):
            raise ValueError("board request duration must be a real number")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("board request duration must be finite and non-negative")
        if self.outcome not in BOARD_REQUEST_STATUSES:
            raise ValueError("board request outcome must be a bounded label")
        if self.comparison_availability not in BOARD_AVAILABILITY_STATES:
            raise ValueError("board request availability must be a bounded label")
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise ValueError("board request status code must be an integer")
        if self.status_code != BOARD_REQUEST_STATUSES[self.outcome]:
            raise ValueError("board request outcome and status code must be one pair")
        for labels, allowed in (
            (self.provider_status_counts, BOARD_PROVIDER_STATUSES),
            (self.failure_reason_counts, BOARD_FAILURE_REASONS),
            (self.freshness_counts, BOARD_FRESHNESS_STATES),
            (self.cache_counts, BOARD_CACHE_STATES),
        ):
            if any(
                label not in allowed
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 1
                for label, count in labels
            ):
                raise ValueError("board request labels must be stable and bounded")
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in (
                self.group_count,
                self.market_count,
                self.unresolved_count,
                self.disabled_provider_count,
            )
        ):
            raise ValueError("board request counts must be non-negative")
        try:
            parsed = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("board request started_at must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("board request started_at must be timezone-aware")
        if self.request_id is not None and not is_valid_request_id(self.request_id):
            raise ValueError("board request request_id is invalid")


def record_board_request_event(event: BoardRequestEvent) -> None:
    """Record one scalar-only board request aggregate in its bounded buffer."""

    payload = {
        "started_at": event.started_at,
        "duration_ms": float(event.duration_ms),
        "request_id": event.request_id,
        "outcome": event.outcome,
        "status_code": event.status_code,
        "comparison_availability": event.comparison_availability,
        "provider_status_counts": dict(event.provider_status_counts),
        "failure_reason_counts": dict(event.failure_reason_counts),
        "freshness_counts": dict(event.freshness_counts),
        "cache_counts": dict(event.cache_counts),
        "group_count": event.group_count,
        "market_count": event.market_count,
        "unresolved_count": event.unresolved_count,
        "disabled_provider_count": event.disabled_provider_count,
    }
    logger.info(
        "board_request outcome=%s status=%d duration_ms=%.1f availability=%s "
        "groups=%d markets=%d",
        event.outcome,
        event.status_code,
        event.duration_ms,
        event.comparison_availability,
        event.group_count,
        event.market_count,
    )
    with _buffer_lock:
        global _board_request_events_total
        _board_request_event_buffer.append(payload)
        _board_request_events_total += 1


def get_recorded_board_request_events() -> list[dict[str, Any]]:
    """Return a snapshot of the bounded board request aggregates."""

    with _buffer_lock:
        return list(_board_request_event_buffer)


def snapshot_recent_board_request_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent bounded board request aggregates.

    Each entry is the same scalar-only payload the buffer holds: closed label
    counts and coverage counts, never an athlete, event, market, or selection
    identity, and never upstream text.
    """

    with _buffer_lock:
        return list(_board_request_event_buffer)[-limit:]


class BoardTelemetryRecorder:
    """Typed recorder seam for board aggregates."""

    def record(self, event: BoardTelemetryEvent) -> None:
        raise NotImplementedError


class BoundedBoardTelemetryRecorder(BoardTelemetryRecorder):
    """Record board aggregates in the existing bounded event buffer."""

    def record(self, event: BoardTelemetryEvent) -> None:
        record_board_telemetry(event)


class PlayerPoolTelemetryRecorder:
    """Typed recorder seam for Player Pool aggregates."""

    def record(self, event: PlayerPoolTelemetryEvent) -> None:
        raise NotImplementedError


class BoundedPlayerPoolTelemetryRecorder(PlayerPoolTelemetryRecorder):
    """Record Player Pool drop counts in a bounded scalar-only buffer."""

    def record(self, event: PlayerPoolTelemetryEvent) -> None:
        payload = asdict(event)
        logger.info(
            "player_pool_event unknown_stat_label_count=%d unjoined_athlete_count=%d "
            "unjoined_event_count=%d team_mismatch_count=%d",
            event.unknown_stat_label_count,
            event.unjoined_athlete_count,
            event.unjoined_event_count,
            event.team_mismatch_count,
        )
        with _buffer_lock:
            global _player_pool_events_total
            _player_pool_event_buffer.append(payload)
            _player_pool_events_total += 1


def snapshot_recent_player_pool_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent scalar-only Player Pool drop aggregates."""

    with _buffer_lock:
        return list(_player_pool_event_buffer)[-limit:]


class PlayerGameLogTelemetryRecorder:
    """Typed recorder seam for player-game-log coverage aggregates."""

    def record(self, event: PlayerGameLogTelemetryEvent) -> None:
        raise NotImplementedError


class BoundedPlayerGameLogTelemetryRecorder(PlayerGameLogTelemetryRecorder):
    """Record privacy-safe player-game-log counts in a bounded buffer."""

    def record(self, event: PlayerGameLogTelemetryEvent) -> None:
        payload = asdict(event)
        logger.info(
            "player_game_log_event source_row_count=%d published_row_count=%d "
            "unjoined_athlete_count=%d unjoined_event_count=%d "
            "team_mismatch_count=%d unsupported_phase_count=%d "
            "malformed_row_count=%d "
            "rejected_publication_count=%d duplicate_row_count=%d "
            "recovered_shrink_row_count=%d",
            event.source_row_count,
            event.published_row_count,
            event.unjoined_athlete_count,
            event.unjoined_event_count,
            event.team_mismatch_count,
            event.unsupported_phase_count,
            event.malformed_row_count,
            event.rejected_publication_count,
            event.duplicate_row_count,
            event.recovered_shrink_row_count,
        )
        with _buffer_lock:
            global _player_game_log_events_total
            _player_game_log_event_buffer.append(payload)
            _player_game_log_events_total += 1


class InjuryTelemetryRecorder:
    """Typed recorder seam for injury reconciliation aggregates."""

    def record(self, event: InjuryTelemetryEvent) -> None:
        raise NotImplementedError


class BoundedInjuryTelemetryRecorder(InjuryTelemetryRecorder):
    """Record injury counts without retaining player or game identities."""

    def record(self, event: InjuryTelemetryEvent) -> None:
        payload = asdict(event)
        logger.info(
            "injury_event unmatched_entry_count=%d "
            "unresolved_team_entry_count=%d board_conflict_count=%d",
            event.unmatched_entry_count,
            event.unresolved_team_entry_count,
            event.board_conflict_count,
        )
        with _buffer_lock:
            global _injury_events_total
            _injury_event_buffer.append(payload)
            _injury_events_total += 1


def snapshot_recent_injury_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent scalar-only injury reconciliation aggregates."""

    with _buffer_lock:
        return list(_injury_event_buffer)[-limit:]


def snapshot_recent_player_game_log_events(
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent scalar-only player-game-log coverage aggregates."""

    with _buffer_lock:
        return list(_player_game_log_event_buffer)[-limit:]


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

    # Cache decision counters belong to the cache seam alone.  A retrieval that
    # makes several real upstream calls records several provider events but
    # still exactly one cache decision, so this does not touch _cache_counts.
    with _buffer_lock:
        _event_buffer.append(payload)
        global _provider_events_total, _provider_failures
        _provider_events_total += 1
        if event.outcome != OUTCOME_SUCCESS:
            key = (event.provider, event.outcome)
            _provider_failures[key] = _provider_failures.get(key, 0) + 1


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

    This is the one seam where a cache decision and a provider event describe
    the same moment, so the decision is counted here explicitly.
    """
    reset_retry_count()
    record_cache_status(provider, cache_status)
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


def record_cache_status(provider: str, cache_status: str) -> None:
    """Record one bounded cache decision without fabricating an upstream call.

    Snapshot caching sits outside the provider adapter seam.  Miss, disabled,
    stale, and backend-error decisions therefore update the existing bounded
    cache counters without adding a duplicate provider event.
    """

    allowed = {CACHE_HIT, CACHE_MISS, CACHE_DISABLED, CACHE_STALE, CACHE_ERROR}
    if cache_status not in allowed:
        cache_status = CACHE_ERROR
    with _buffer_lock:
        per_provider = _cache_counts.setdefault(provider, {})
        per_provider[cache_status] = per_provider.get(cache_status, 0) + 1


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
            "board_request_events_total": _board_request_events_total,
            "player_pool_events_total": _player_pool_events_total,
            "player_pool_buffered_events": len(_player_pool_event_buffer),
            "player_game_log_events_total": _player_game_log_events_total,
            "player_game_log_buffered_events": len(_player_game_log_event_buffer),
            "injury_events_total": _injury_events_total,
            "injury_buffered_events": len(_injury_event_buffer),
            "board_request_buffered_events": len(_board_request_event_buffer),
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
    global _provider_events_total, _board_events_total, _board_request_events_total
    global _player_pool_events_total, _player_game_log_events_total, _injury_events_total
    global _provider_failures, _application_failures, _cache_counts
    with _buffer_lock:
        _event_buffer.clear()
        _board_event_buffer.clear()
        _board_request_event_buffer.clear()
        _player_pool_event_buffer.clear()
        _player_game_log_event_buffer.clear()
        _injury_event_buffer.clear()
        _provider_events_total = 0
        _board_events_total = 0
        _board_request_events_total = 0
        _player_pool_events_total = 0
        _player_game_log_events_total = 0
        _injury_events_total = 0
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
    "CACHE_ERROR",
    "CACHE_HIT",
    "CACHE_MISS",
    "CACHE_STALE",
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
    "PROVIDER_ROTOWIRE",
    "NBA_STATS_OPERATIONS",
    "PBP_STATS_OPERATIONS",
    "PRIZEPICKS_OPERATIONS",
    "UNDERDOG_OPERATIONS",
    "ROTOWIRE_OPERATIONS",
    "PROVIDER_OPERATION_CATALOG",
    "ProviderEvent",
    "BOARD_AVAILABILITY_STATES",
    "BOARD_CACHE_STATES",
    "BOARD_FAILURE_REASONS",
    "BOARD_FRESHNESS_STATES",
    "BOARD_PROVIDER_STATUSES",
    "BOARD_REQUEST_OUTCOMES",
    "BOARD_REQUEST_STATUSES",
    "BoardRequestEvent",
    "record_board_request_event",
    "get_recorded_board_request_events",
    "snapshot_recent_board_request_events",
    "BoardTelemetryEvent",
    "BoardTelemetryRecorder",
    "BoundedBoardTelemetryRecorder",
    "get_recorded_board_events",
    "record_board_telemetry",
    "snapshot_recent_board_events",
    "PlayerPoolTelemetryEvent",
    "PlayerPoolTelemetryRecorder",
    "BoundedPlayerPoolTelemetryRecorder",
    "snapshot_recent_player_pool_events",
    "PlayerGameLogTelemetryEvent",
    "PlayerGameLogTelemetryRecorder",
    "BoundedPlayerGameLogTelemetryRecorder",
    "snapshot_recent_player_game_log_events",
    "InjuryTelemetryEvent",
    "InjuryTelemetryRecorder",
    "BoundedInjuryTelemetryRecorder",
    "snapshot_recent_injury_events",
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
    "record_cache_status",
    "record_provider_event",
    "reset_retry_count",
    "set_clock",
    "set_request_id",
    "clear_request_id",
    "snapshot_metrics",
    "snapshot_recent_events",
]
