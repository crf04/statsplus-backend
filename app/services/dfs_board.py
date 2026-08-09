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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable

import requests

from app.config.settings import ConfigurationError, RuntimeSettings
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

logger = logging.getLogger(__name__)

SUPPORTED_DFS_PROVIDERS = ("dabble", "prizepicks", "underdog")
DEFAULT_BOARD_DEADLINE_SECONDS = 15.0
DEFAULT_BOARD_MAX_CONCURRENCY = 3


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


class DFSProviderRegistry(Mapping[str, ProviderSnapshotProvider]):
    """Immutable name-to-provider registry supplied to the board service."""

    def __init__(self, providers: Mapping[str, ProviderSnapshotProvider]) -> None:
        values = DFSBoardService._build_registry(providers)
        self._values = MappingProxyType(
            {name: values[name] for name in sorted(values)}
        )

    def __getitem__(self, key: str) -> ProviderSnapshotProvider:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


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

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)

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

    @property
    def failure_reason(self) -> ProviderFailureReason | None:
        """Explicit alias for serializers that name the field fully."""

        return self.reason


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
    def outcomes(self) -> tuple[ProviderOutcome, ...]:
        """Short alias used by internal callers."""

        return self.provider_outcomes

    @property
    def snapshots(self) -> tuple[ProviderSnapshot, ...]:
        """Usable snapshots, retaining each provider's coherent observation."""

        return tuple(
            outcome.snapshot
            for outcome in self.provider_outcomes
            if outcome.usable and outcome.snapshot is not None
        )

    @property
    def usable_snapshots(self) -> tuple[ProviderSnapshot, ...]:
        return self.snapshots

    @property
    def provider_snapshots(self) -> tuple[ProviderSnapshot, ...]:
        return self.snapshots

    @property
    def usable(self) -> bool:
        """Whether at least one provider produced a usable snapshot."""

        return bool(self.snapshots)


class DFSBoardService:
    """Collect enabled DFS providers under one absolute retrieval deadline."""

    def __init__(
        self,
        providers: Mapping[str, ProviderSnapshotProvider]
        | Sequence[ProviderSnapshotProvider]
        | None = None,
        *,
        provider_registry: Mapping[str, ProviderSnapshotProvider] | None = None,
        enabled_provider_registry: Mapping[str, ProviderSnapshotProvider] | None = None,
        enabled_providers: Mapping[str, ProviderSnapshotProvider]
        | Iterable[str]
        | None = None,
        known_providers: Iterable[str] = SUPPORTED_DFS_PROVIDERS,
        max_concurrency: int = DEFAULT_BOARD_MAX_CONCURRENCY,
        max_workers: int | None = None,
        deadline_seconds: float = DEFAULT_BOARD_DEADLINE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        settings: RuntimeSettings | None = None,
    ) -> None:
        if enabled_provider_registry is not None:
            if provider_registry is not None or providers is not None:
                raise ValueError(
                    "provide one of providers, provider_registry, or enabled_provider_registry"
                )
            provider_registry = enabled_provider_registry
        if providers is not None and provider_registry is not None:
            raise ValueError("provide providers or provider_registry, not both")
        registry_input: Mapping[str, ProviderSnapshotProvider] | Sequence[ProviderSnapshotProvider]
        registry_input = provider_registry if provider_registry is not None else (providers or {})
        registry = self._build_registry(registry_input)

        if isinstance(enabled_providers, Mapping):
            if provider_registry is not None or providers is not None:
                raise ValueError("enabled provider mapping cannot accompany a registry")
            registry = self._build_registry(enabled_providers)
            enabled = tuple(registry)
        elif enabled_providers is None:
            enabled = tuple(registry)
        else:
            enabled = _normalize_names(enabled_providers)

        missing = tuple(name for name in enabled if name not in registry)
        if missing:
            raise ValueError(
                "enabled DFS providers are not present in the injected registry: "
                + ", ".join(missing)
            )
        if settings is not None and settings.environment == "production" and not enabled:
            raise ConfigurationError(
                "DFS_ENABLED_PROVIDERS must explicitly configure at least one provider in production"
            )

        concurrency = max_workers if max_workers is not None else max_concurrency
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
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
        self.max_concurrency = min(concurrency, max(1, len(self.enabled_providers)))
        self.deadline_seconds = float(deadline_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic

    @property
    def enabled_provider_registry(self) -> Mapping[str, ProviderSnapshotProvider]:
        """Alias exposing the injected registry as a read-only mapping."""

        return self.provider_registry

    @staticmethod
    def _build_registry(
        values: Mapping[str, ProviderSnapshotProvider] | Sequence[ProviderSnapshotProvider],
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

        registry = {}
        for provider in values:
            name = _provider_name(getattr(provider, "name", ""))
            if not callable(getattr(provider, "get_snapshot", None)):
                raise TypeError(f"DFS provider {name} must implement get_snapshot")
            if name in registry:
                raise ValueError(f"duplicate DFS provider {name}")
            registry[name] = provider
        return registry

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
        if context is None:
            now = self._clock_utc()
            context = RetrievalContext(
                deadline=now + timedelta(seconds=self.deadline_seconds)
            )
        elif not isinstance(context, RetrievalContext):
            raise TypeError("context must be RetrievalContext")

        generated_at = self._clock_utc()
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
                    self._harvest(name, holder, outcomes)
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

        deadline_reached = self._deadline_reached(context, board_deadline)
        if pending or active or deadline_reached:
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
        return DFSBoard(
            query=query,
            provider_outcomes=ordered,
            disabled_providers=self.disabled_providers,
            generated_at=generated_at,
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
            snapshot = self.provider_registry[name].get_snapshot(query, context)
            if self.monotonic() >= board_deadline or context.is_expired(
                now=self._clock_utc()
            ):
                holder["outcome"] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
                return
            holder["outcome"] = self._outcome_from_snapshot(name, snapshot)
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
            if self.monotonic() < board_deadline and not context.is_expired(
                now=self._clock_utc()
            ):
                holder["exception"] = error

    @staticmethod
    def _harvest(
        name: str,
        holder: Mapping[str, Any],
        outcomes: dict[str, ProviderOutcome],
    ) -> None:
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
            )
        return ProviderOutcome(
            provider=name,
            status=ProviderOutcomeStatus.PARTIAL,
            snapshot=snapshot,
            reason=cls._partial_reason(snapshot),
        )

    @staticmethod
    def _partial_reason(snapshot: ProviderSnapshot) -> ProviderFailureReason | None:
        codes = set(snapshot.coverage.warning_codes) | set(snapshot.coverage.skipped_reasons)
        if CoverageCode.PAGE_MALFORMED in codes or CoverageCode.FIXTURE_MALFORMED in codes:
            return ProviderFailureReason.MALFORMED_RESPONSE
        if codes & {
            CoverageCode.PAGE_FETCH_FAILED,
            CoverageCode.FIXTURE_FAILED,
            CoverageCode.FIXTURE_LIST_FAILED,
        }:
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

        detail_parts: list[str] = []
        for candidate in chain:
            detail_parts.append(str(candidate))
            candidate_detail = getattr(candidate, "detail", None)
            if candidate_detail:
                detail_parts.append(str(candidate_detail))
        detail = " ".join(detail_parts).casefold()
        if "deadline" in detail:
            return ProviderFailureReason.DEADLINE_EXCEEDED
        if "rate_limited" in detail or "rate limited" in detail or "429" in detail:
            return ProviderFailureReason.RATE_LIMITED
        if "access_denied" in detail or "access denied" in detail or "403" in detail:
            return ProviderFailureReason.ACCESS_DENIED
        if "malformed" in detail or "invalid response" in detail:
            return ProviderFailureReason.MALFORMED_RESPONSE
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


# Names used by the design contract and by callers that prefer a shorter term.
DFSBoardQuery = NBAMarketQuery
BoardQuery = DFSBoardQuery
ProviderRegistry = DFSProviderRegistry


__all__ = [
    "BoardQuery",
    "DEFAULT_BOARD_DEADLINE_SECONDS",
    "DFSBoard",
    "DFSBoardQuery",
    "DFSProviderRegistry",
    "DFSBoardService",
    "ProviderFailureReason",
    "ProviderOutcome",
    "ProviderOutcomeStatus",
    "ProviderRegistry",
    "SUPPORTED_DFS_PROVIDERS",
]
