"""Internal, deadline-bounded collection of NBA DFS provider snapshots.

The board collector is deliberately smaller than a public board API.  It owns
provider selection, bounded fan-out, expected provider-failure translation,
and deterministic ordering.  Adapters remain the owners of wire formats and
return one immutable :class:`ProviderSnapshot` per observation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable

import requests

from app.config.settings import ConfigurationError, RuntimeSettings
from app.dfs_catalog import DFS_PROVIDER_NAMES
from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    CoverageCode,
    DeadlineExceededError,
    MalformedProviderResponseError,
    NBAMarketQuery,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    SnapshotStatus,
)
from app.utils.telemetry import ProviderResponseError
from app.utils.telemetry import (
    BoardTelemetryEvent,
    BoardTelemetryRecorder,
    BoundedBoardTelemetryRecorder,
)

logger = logging.getLogger(__name__)

DEFAULT_BOARD_DEADLINE_SECONDS = 15.0
DEFAULT_BOARD_MAX_CONCURRENCY = 3

__all__ = [
    "DFSBoard",
    "DFSBoardService",
    "ProviderFailureReason",
    "ProviderOutcome",
    "ProviderOutcomeStatus",
]

# CoverageCode is a closed shared vocabulary. Keep the board's semantic
# classification closed too, so newly reviewed malformed/pagination codes
# cannot silently fall through to ``upstream_error``.
_MALFORMED_COVERAGE_CODES = frozenset(
    {
        CoverageCode.CONFLICTING_SOURCE_IDENTITY,
        CoverageCode.FIXTURE_MALFORMED,
        CoverageCode.MISSING_COMPETITION_ID,
        CoverageCode.MALFORMED_RECORD,
        CoverageCode.PAGE_MALFORMED,
        CoverageCode.PAGE_METADATA_MISMATCH,
        CoverageCode.PAGINATION_EXPECTED_TOTAL_CHANGED,
        CoverageCode.PAGINATION_METADATA_MALFORMED,
        CoverageCode.PAGINATION_PAGE_MISMATCH,
        CoverageCode.PAGINATION_TOTAL_PAGES_CHANGED,
    }
)
_UPSTREAM_COVERAGE_CODES = frozenset(
    {
        CoverageCode.FIXTURE_FAILED,
        CoverageCode.FIXTURE_LIST_FAILED,
        CoverageCode.PAGE_FETCH_FAILED,
    }
)


class ProviderOutcomeStatus(str, Enum):
    """Stable status for one enabled provider attempt."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ProviderFailureReason(str, Enum):
    """Safe, provider-independent reasons exposed by the collector."""

    TIMEOUT = "timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    UPSTREAM_ERROR = "upstream_error"
    MALFORMED_RESPONSE = "malformed_response"


def _provider_name(value: Any) -> str:
    """Normalize one provider name without exposing arbitrary source labels."""

    name = value.strip().casefold() if isinstance(value, str) else ""
    if not name:
        raise ValueError("DFS provider name must be a non-empty string")
    return name


def _normalize_names(values: Iterable[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _provider_name(value)
        if name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """One provider's complete, partial, or failed retrieval observation."""

    provider: str
    status: ProviderOutcomeStatus | str
    snapshot: ProviderSnapshot | None = None
    reason: ProviderFailureReason | str | None = None
    cache_status: str | None = None
    cache_retrieved_at: datetime | str | None = None
    cache_age_seconds: float | None = None
    cache_failure_reason: str | None = None
    cache_failure_at: datetime | str | None = None

    def __post_init__(self) -> None:
        provider = _provider_name(self.provider)
        try:
            status = (
                self.status
                if isinstance(self.status, ProviderOutcomeStatus)
                else ProviderOutcomeStatus(self.status)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("provider outcome status is invalid") from error
        if self.snapshot is not None and not isinstance(self.snapshot, ProviderSnapshot):
            raise ValueError("provider outcome snapshot must be ProviderSnapshot or None")
        if self.snapshot is not None and self.snapshot.provider != provider:
            raise ValueError("provider outcome snapshot provider must match outcome provider")
        if status in {ProviderOutcomeStatus.COMPLETE, ProviderOutcomeStatus.PARTIAL}:
            if self.snapshot is None:
                raise ValueError("usable provider outcomes require a snapshot")
            expected = (
                SnapshotStatus.COMPLETE
                if status is ProviderOutcomeStatus.COMPLETE
                else SnapshotStatus.PARTIAL
            )
            if self.snapshot.status is not expected:
                raise ValueError("provider outcome status must match snapshot status")
        elif self.snapshot is not None:
            raise ValueError("failed provider outcomes cannot carry a snapshot")

        reason = self.reason
        if reason is not None:
            try:
                reason = (
                    reason
                    if isinstance(reason, ProviderFailureReason)
                    else ProviderFailureReason(reason)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("provider outcome reason is invalid") from error
        if status is ProviderOutcomeStatus.FAILED and reason is None:
            raise ValueError("failed provider outcomes require a reason")

        cache_status = self.cache_status
        if cache_status is not None and cache_status not in {
            "hit",
            "miss",
            "disabled",
            "stale",
            "error",
        }:
            raise ValueError("provider outcome cache_status is invalid")
        cache_failure_reason = self.cache_failure_reason
        if cache_failure_reason is not None and (
            not isinstance(cache_failure_reason, str) or not cache_failure_reason.strip()
        ):
            raise ValueError("provider outcome cache_failure_reason is invalid")
        cache_age = self.cache_age_seconds
        if cache_age is not None and (
            isinstance(cache_age, bool)
            or not isinstance(cache_age, (int, float))
            or cache_age < 0
        ):
            raise ValueError("provider outcome cache_age_seconds must be non-negative")
        cache_retrieved_at = self.cache_retrieved_at
        if cache_retrieved_at is not None:
            if isinstance(cache_retrieved_at, str):
                try:
                    cache_retrieved_at = datetime.fromisoformat(
                        cache_retrieved_at.replace("Z", "+00:00")
                    )
                except ValueError as error:
                    raise ValueError("provider outcome cache_retrieved_at is invalid") from error
            if not isinstance(cache_retrieved_at, datetime) or cache_retrieved_at.tzinfo is None:
                raise ValueError("provider outcome cache_retrieved_at must be timezone-aware")
            cache_retrieved_at = cache_retrieved_at.astimezone(timezone.utc)
        cache_failure_at = self.cache_failure_at
        if cache_failure_at is not None:
            if isinstance(cache_failure_at, str):
                try:
                    cache_failure_at = datetime.fromisoformat(
                        cache_failure_at.replace("Z", "+00:00")
                    )
                except ValueError as error:
                    raise ValueError("provider outcome cache_failure_at is invalid") from error
            if not isinstance(cache_failure_at, datetime) or cache_failure_at.tzinfo is None:
                raise ValueError("provider outcome cache_failure_at must be timezone-aware")
            cache_failure_at = cache_failure_at.astimezone(timezone.utc)

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "cache_status", cache_status)
        object.__setattr__(self, "cache_failure_reason", cache_failure_reason)
        object.__setattr__(self, "cache_retrieved_at", cache_retrieved_at)
        object.__setattr__(self, "cache_age_seconds", cache_age)
        object.__setattr__(self, "cache_failure_at", cache_failure_at)

    @property
    def usable(self) -> bool:
        """Whether this outcome contributes one coherent provider snapshot."""

        return self.snapshot is not None and self.status in {
            ProviderOutcomeStatus.COMPLETE,
            ProviderOutcomeStatus.PARTIAL,
        }

    @property
    def coverage(self):
        """Expose adapter coverage without copying or flattening its evidence."""

        return self.snapshot.coverage if self.snapshot is not None else None


@dataclass(frozen=True, slots=True)
class DFSBoard:
    """Deterministic collection result for one board retrieval attempt."""

    query: NBAMarketQuery
    provider_outcomes: tuple[ProviderOutcome, ...]
    disabled_providers: tuple[str, ...] = ()
    generated_at: datetime | str = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    contract_version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.query, NBAMarketQuery):
            raise ValueError("DFS board query must be NBAMarketQuery")
        outcomes = tuple(self.provider_outcomes)
        if any(not isinstance(outcome, ProviderOutcome) for outcome in outcomes):
            raise ValueError("DFS board outcomes must be ProviderOutcome values")
        if tuple(sorted(outcome.provider for outcome in outcomes)) != tuple(
            outcome.provider for outcome in outcomes
        ):
            raise ValueError("DFS board provider outcomes must be deterministic")
        disabled = _normalize_names(self.disabled_providers)
        if tuple(sorted(disabled)) != disabled:
            raise ValueError("DFS board disabled providers must be deterministic")
        if set(disabled) & {outcome.provider for outcome in outcomes}:
            raise ValueError("a provider cannot be enabled and disabled")
        generated = self.generated_at
        if isinstance(generated, str):
            generated = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if not isinstance(generated, datetime) or generated.tzinfo is None:
            raise ValueError("DFS board generated_at must be timezone-aware")
        if not isinstance(self.contract_version, str) or not self.contract_version.strip():
            raise ValueError("DFS board contract_version must be non-empty")
        object.__setattr__(self, "provider_outcomes", outcomes)
        object.__setattr__(self, "disabled_providers", disabled)
        object.__setattr__(self, "generated_at", generated.astimezone(timezone.utc))
        object.__setattr__(self, "contract_version", self.contract_version.strip())

    @property
    def snapshots(self) -> tuple[ProviderSnapshot, ...]:
        """Usable snapshots, retaining each provider's coherent observation."""

        return tuple(
            outcome.snapshot
            for outcome in self.provider_outcomes
            if outcome.usable and outcome.snapshot is not None
        )

    @property
    def usable(self) -> bool:
        """Whether at least one provider produced a usable snapshot."""

        return bool(self.snapshots)


class DFSBoardService:
    """Collect enabled DFS providers under one absolute retrieval deadline."""

    def __init__(
        self,
        provider_registry: Mapping[str, ProviderSnapshotProvider],
        *,
        known_providers: Iterable[str] = DFS_PROVIDER_NAMES,
        max_concurrency: int = DEFAULT_BOARD_MAX_CONCURRENCY,
        deadline_seconds: float = DEFAULT_BOARD_DEADLINE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        settings: RuntimeSettings | None = None,
        telemetry_recorder: BoardTelemetryRecorder | None = None,
        snapshot_cache: Callable[
            [str, ProviderSnapshotProvider], ProviderSnapshotProvider
        ]
        | None = None,
    ) -> None:
        registry = self._build_registry(provider_registry)
        decorator = snapshot_cache
        if decorator is not None:
            decorate = getattr(decorator, "decorate", None)
            if callable(decorate):
                decorator = decorate
            if not callable(decorator):
                raise TypeError("DFS snapshot cache decorator must be callable")
            registry = {
                name: decorator(name, provider)
                for name, provider in registry.items()
            }
            registry = self._build_registry(registry)
        enabled = tuple(registry)
        if settings is not None and settings.environment == "production" and not enabled:
            raise ConfigurationError(
                "DFS_ENABLED_PROVIDERS must explicitly configure at least one provider in production"
            )

        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("DFS board max_concurrency must be a positive integer")
        if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool):
            raise ValueError("DFS board deadline_seconds must be positive")
        if deadline_seconds <= 0:
            raise ValueError("DFS board deadline_seconds must be positive")

        known = _normalize_names(known_providers)
        self.provider_registry = MappingProxyType(
            {name: registry[name] for name in sorted(registry)}
        )
        self.enabled_providers = tuple(sorted(enabled))
        self.disabled_providers = tuple(sorted(set(known) - set(self.enabled_providers)))
        self.max_concurrency = min(max_concurrency, max(1, len(self.enabled_providers)))
        self.deadline_seconds = min(float(deadline_seconds), DEFAULT_BOARD_DEADLINE_SECONDS)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.telemetry_recorder = telemetry_recorder or BoundedBoardTelemetryRecorder()

    @staticmethod
    def _build_registry(
        values: Mapping[str, ProviderSnapshotProvider],
    ) -> dict[str, ProviderSnapshotProvider]:
        if isinstance(values, Mapping):
            registry: dict[str, ProviderSnapshotProvider] = {}
            for raw_name, provider in values.items():
                name = _provider_name(raw_name)
                if not callable(getattr(provider, "get_snapshot", None)):
                    raise TypeError(f"DFS provider {name} must implement get_snapshot")
                if name in registry:
                    raise ValueError(f"duplicate DFS provider {name}")
                registry[name] = provider
            return registry

        raise TypeError("DFS provider registry must be a mapping")

    def get_board(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext | None = None,
    ) -> DFSBoard:
        """Retrieve enabled snapshots with one shared absolute deadline.

        Expected upstream failures become failed outcomes.  A provider that
        raises an implementation defect is deliberately not caught.  Running
        provider threads are daemonized and their late results are isolated
        from the returned board.
        """

        if not isinstance(query, NBAMarketQuery):
            raise TypeError("query must be NBAMarketQuery")
        generated_at = self._clock_utc()
        if context is None:
            context = RetrievalContext(
                deadline=generated_at + timedelta(seconds=self.deadline_seconds)
            )
        elif not isinstance(context, RetrievalContext):
            raise TypeError("context must be RetrievalContext")
        # Adapters receive a child context so a caller cannot accidentally
        # extend this board's absolute fifteen-second ceiling.
        context = RetrievalContext(
            deadline=min(
                context.deadline,
                generated_at + timedelta(seconds=self.deadline_seconds),
            ),
            request_id=context.request_id,
        )
        start = self.monotonic()
        remaining = context.remaining_seconds(now=generated_at)
        board_deadline = start + remaining
        names = list(self.enabled_providers)
        outcomes: dict[str, ProviderOutcome] = {}
        pending = list(names)
        active: dict[str, tuple[threading.Thread, dict[str, Any]]] = {}

        while pending or active:
            while pending and len(active) < self.max_concurrency:
                if self._deadline_reached(context, board_deadline):
                    break
                name = pending.pop(0)
                holder: dict[str, Any] = {}
                thread = threading.Thread(
                    target=self._retrieve_one,
                    args=(name, query, context, board_deadline, holder),
                    daemon=True,
                    name=f"statsplus-dfs-{name}",
                )
                active[name] = (thread, holder)
                thread.start()

            completed = [
                name for name, (thread, _holder) in active.items() if not thread.is_alive()
            ]
            if completed:
                for name in completed:
                    thread, holder = active.pop(name)
                    thread.join(timeout=0)
                    self._harvest(name, holder, outcomes, board_deadline)
                continue

            if self._deadline_reached(context, board_deadline):
                break
            wait_for = min(0.01, max(0.0, board_deadline - self.monotonic()))
            if not wait_for:
                break
            # A short join gives completed workers priority while preserving
            # the hard caller-side deadline when a provider ignores it.
            next_thread = next(iter(active.values()))[0]
            next_thread.join(timeout=wait_for)

        # A worker may publish a result in the same instant that the caller
        # observes the deadline. Drain holders whose completion timestamp is
        # at or before the boundary before assigning deadline failures.
        for name in list(active):
            thread, holder = active[name]
            completed_at = holder.get("completed_at")
            if completed_at is not None and completed_at <= board_deadline:
                active.pop(name)
                thread.join(timeout=0)
                self._harvest(name, holder, outcomes, board_deadline)

        if pending or active or self._deadline_reached(context, board_deadline):
            for name in pending:
                outcomes[name] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
            for name in active:
                outcomes[name] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )

        ordered = tuple(outcomes[name] for name in names if name in outcomes)
        board = DFSBoard(
            query=query,
            provider_outcomes=ordered,
            disabled_providers=self.disabled_providers,
            generated_at=generated_at,
        )
        self._record_telemetry(
            self.monotonic() - start,
            board,
            started_at=generated_at.isoformat(),
            request_id=context.request_id,
        )
        return board

    def _record_telemetry(
        self,
        duration: float,
        board: DFSBoard,
        *,
        started_at: str,
        request_id: str | None,
    ) -> None:
        outcome_counts = Counter(outcome.status.value for outcome in board.provider_outcomes)
        reason_counts = Counter(
            outcome.reason.value
            for outcome in board.provider_outcomes
            if outcome.reason is not None
        )
        fetched = eligible = normalized = skipped = 0
        for outcome in board.provider_outcomes:
            if outcome.coverage is not None:
                fetched += outcome.coverage.fetched_count
                eligible += outcome.coverage.eligible_count
                normalized += outcome.coverage.normalized_count
                skipped += outcome.coverage.skipped_count
        self.telemetry_recorder.record(
            BoardTelemetryEvent(
                duration_ms=max(0.0, duration * 1000.0),
                outcome_counts=tuple(sorted(outcome_counts.items())),
                failure_reason_counts=tuple(sorted(reason_counts.items())),
                fetched_count=fetched,
                eligible_count=eligible,
                normalized_count=normalized,
                skipped_count=skipped,
                started_at=started_at,
                request_id=request_id,
            )
        )

    def _retrieve_one(
        self,
        name: str,
        query: NBAMarketQuery,
        context: RetrievalContext,
        board_deadline: float,
        holder: dict[str, Any],
    ) -> None:
        """Run one provider in an isolated daemon worker."""

        try:
            if self.monotonic() >= board_deadline:
                holder["outcome"] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
                return
            context.ensure_active(now=self._clock_utc())
            provider = self.provider_registry[name]
            snapshot = provider.get_snapshot(query, context)
            if self.monotonic() > board_deadline:
                holder["outcome"] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
                return
            cache_result = None
            get_last_result = getattr(provider, "get_last_result", None)
            if callable(get_last_result):
                cache_result = get_last_result()
            if cache_result is None:
                # Keep the narrow existing provider seam compatible with
                # injected test doubles that override this class method.
                holder["outcome"] = self._outcome_from_snapshot(name, snapshot)
            else:
                holder["outcome"] = self._outcome_from_snapshot(
                    name,
                    snapshot,
                    cache_result=cache_result,
                )
        except (
            DeadlineExceededError,
            ProviderUnavailableError,
            ProviderResponseError,
            MalformedProviderResponseError,
            TimeoutError,
            requests.exceptions.RequestException,
        ) as error:
            if self.monotonic() >= board_deadline or context.is_expired(
                now=self._clock_utc()
            ):
                reason = ProviderFailureReason.DEADLINE_EXCEEDED
            else:
                reason = self._failure_reason(error)
            holder["outcome"] = ProviderOutcome(
                provider=name,
                status=ProviderOutcomeStatus.FAILED,
                reason=reason,
            )
        except BaseException as error:
            # The main thread re-raises defects from workers as soon as their
            # worker completes.  A late defect is isolated just like a late
            # provider result because the board deadline has already passed.
            holder["exception"] = error
        finally:
            holder["completed_at"] = self.monotonic()

    @staticmethod
    def _harvest(
        name: str,
        holder: Mapping[str, Any],
        outcomes: dict[str, ProviderOutcome],
        board_deadline: float,
    ) -> None:
        completed_at = holder.get("completed_at")
        if not isinstance(completed_at, (int, float)) or completed_at > board_deadline:
            outcomes[name] = ProviderOutcome(
                provider=name,
                status=ProviderOutcomeStatus.FAILED,
                reason=ProviderFailureReason.DEADLINE_EXCEEDED,
            )
            return
        error = holder.get("exception")
        if error is not None:
            raise error
        outcome = holder.get("outcome")
        if not isinstance(outcome, ProviderOutcome):
            raise RuntimeError(f"DFS provider {name} did not produce an outcome")
        outcomes[name] = outcome
        logger.info(
            "dfs_board_provider provider=%s status=%s reason=%s fetched=%s "
            "eligible=%s normalized=%s skipped=%s",
            outcome.provider,
            outcome.status.value,
            outcome.reason.value if outcome.reason is not None else "none",
            outcome.coverage.fetched_count if outcome.coverage is not None else 0,
            outcome.coverage.eligible_count if outcome.coverage is not None else 0,
            outcome.coverage.normalized_count if outcome.coverage is not None else 0,
            outcome.coverage.skipped_count if outcome.coverage is not None else 0,
        )

    @classmethod
    def _outcome_from_snapshot(
        cls,
        name: str,
        snapshot: ProviderSnapshot,
        *,
        cache_result: Any | None = None,
    ) -> ProviderOutcome:
        if not isinstance(snapshot, ProviderSnapshot):
            raise TypeError("DFS provider get_snapshot must return ProviderSnapshot")
        if snapshot.provider != name:
            raise ValueError("DFS provider snapshot provider does not match registry name")
        if snapshot.status is SnapshotStatus.COMPLETE:
            return ProviderOutcome(
                provider=name,
                status=ProviderOutcomeStatus.COMPLETE,
                snapshot=snapshot,
                cache_status=getattr(cache_result, "cache_status", None),
                cache_retrieved_at=getattr(cache_result, "retrieved_at", None),
                cache_age_seconds=getattr(cache_result, "age_seconds", None),
                cache_failure_reason=getattr(
                    cache_result, "refresh_failure_reason", None
                ),
                cache_failure_at=getattr(cache_result, "refresh_failed_at", None),
            )
        return ProviderOutcome(
            provider=name,
            status=ProviderOutcomeStatus.PARTIAL,
            snapshot=snapshot,
            reason=cls._partial_reason(snapshot),
            cache_status=getattr(cache_result, "cache_status", None),
            cache_retrieved_at=getattr(cache_result, "retrieved_at", None),
            cache_age_seconds=getattr(cache_result, "age_seconds", None),
            cache_failure_reason=getattr(cache_result, "refresh_failure_reason", None),
            cache_failure_at=getattr(cache_result, "refresh_failed_at", None),
        )

    @staticmethod
    def _partial_reason(snapshot: ProviderSnapshot) -> ProviderFailureReason | None:
        codes = set(snapshot.coverage.warning_codes) | set(snapshot.coverage.skipped_reasons)
        if codes & _MALFORMED_COVERAGE_CODES:
            return ProviderFailureReason.MALFORMED_RESPONSE
        if codes & _UPSTREAM_COVERAGE_CODES:
            return ProviderFailureReason.UPSTREAM_ERROR
        return ProviderFailureReason.UPSTREAM_ERROR if not snapshot.coverage.is_complete else None

    @staticmethod
    def _failure_reason(error: BaseException) -> ProviderFailureReason:
        """Classify expected failures without retaining their diagnostic text."""

        chain: list[BaseException] = []
        current: BaseException | None = error
        while current is not None and len(chain) < 12:
            chain.append(current)
            current = current.__cause__ or current.__context__

        for candidate in chain:
            if isinstance(candidate, DeadlineExceededError):
                return ProviderFailureReason.DEADLINE_EXCEEDED
            if isinstance(candidate, (ProviderResponseError, MalformedProviderResponseError)):
                return ProviderFailureReason.MALFORMED_RESPONSE

        for candidate in chain:
            if isinstance(candidate, requests.exceptions.HTTPError):
                status = getattr(getattr(candidate, "response", None), "status_code", None)
                if status == 429:
                    return ProviderFailureReason.RATE_LIMITED
                if status in {401, 403}:
                    return ProviderFailureReason.ACCESS_DENIED
                return ProviderFailureReason.UPSTREAM_ERROR
            if isinstance(candidate, requests.exceptions.Timeout):
                return ProviderFailureReason.TIMEOUT
            if isinstance(candidate, TimeoutError):
                return ProviderFailureReason.TIMEOUT

        typed_reasons = {
            "deadline_exceeded": ProviderFailureReason.DEADLINE_EXCEEDED,
            "timeout": ProviderFailureReason.TIMEOUT,
            "rate_limited": ProviderFailureReason.RATE_LIMITED,
            "access_denied": ProviderFailureReason.ACCESS_DENIED,
            "malformed": ProviderFailureReason.MALFORMED_RESPONSE,
            "malformed_response": ProviderFailureReason.MALFORMED_RESPONSE,
            "upstream_error": ProviderFailureReason.UPSTREAM_ERROR,
        }
        for candidate in chain:
            typed_reason = typed_reasons.get(getattr(candidate, "provider_reason", None))
            if typed_reason is not None:
                return typed_reason
        return ProviderFailureReason.UPSTREAM_ERROR

    def _clock_utc(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("DFS board clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _deadline_reached(
        self,
        context: RetrievalContext,
        board_deadline: float,
    ) -> bool:
        """Observe both monotonic and injected wall clocks without extending work."""

        return self.monotonic() >= board_deadline or context.is_expired(
            now=self._clock_utc()
        )


__all__ = [
    "DEFAULT_BOARD_DEADLINE_SECONDS",
    "DFSBoard",
    "DFSBoardService",
    "ProviderFailureReason",
    "ProviderOutcome",
    "ProviderOutcomeStatus",
]
