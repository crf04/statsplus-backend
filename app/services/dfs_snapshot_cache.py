"""Redis-backed caching for immutable DFS provider snapshots.

The board collector operates on provider snapshots, not on serialized boards.
This module owns the small cache boundary around one injected provider: Redis
holds a strict JSON representation of complete normalized snapshots, while the
provider remains the source of truth whenever a value is missing, stale, or
unusable.
"""

from __future__ import annotations

import atexit
import json
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import fields, is_dataclass
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from math import isfinite
from threading import Lock, local
from typing import Any, Callable, Mapping, Protocol

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    AppearanceEvidence,
    CompetitionEvidence,
    CoverageEvidence,
    DeadlineExceededError,
    EventEvidence,
    LeagueEvidence,
    MalformedProviderResponseError,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    Selection,
    SelectionModifier,
    SnapshotStatus,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
)
from app.utils.telemetry import ProviderResponseError


SNAPSHOT_SCHEMA = "statsplus.provider_snapshot"
SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_FRESH_SECONDS = 5 * 60
DEFAULT_STALE_IF_ERROR_SECONDS = 30 * 60


class SnapshotCacheError(ValueError):
    """The Redis payload is not a compatible provider-snapshot document."""


def _encoded(value: Any) -> Any:
    """Encode the immutable model tree into JSON-safe primitive values."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value):
        return {field.name: _encoded(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_encoded(item) for item in value]
    if isinstance(value, list):
        return [_encoded(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _encoded(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported provider snapshot value: {type(value).__name__}")


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotCacheError(f"snapshot {label} must be an object")
    return value


def _keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise SnapshotCacheError(f"snapshot {label} schema is invalid")


def _nested(
    value: Any,
    constructor: type[Any],
    expected: set[str],
    *,
    label: str,
) -> Any:
    if value is None:
        return None
    data = _mapping(value, label=label)
    _keys(data, expected, label=label)
    try:
        return constructor(**data)
    except (TypeError, ValueError, KeyError) as error:
        raise SnapshotCacheError(f"snapshot {label} value is invalid") from error


def _team(value: Any, *, label: str) -> TeamEvidence | None:
    return _nested(
        value,
        TeamEvidence,
        {"provider_id", "canonical_id", "name", "abbreviation"},
        label=label,
    )


def _athlete(value: Any) -> AthleteEvidence | None:
    if value is None:
        return None
    data = _mapping(value, label="athlete")
    _keys(data, {"provider_id", "canonical_id", "name", "team"}, label="athlete")
    data = dict(data)
    data["team"] = _team(data["team"], label="athlete.team")
    return _nested(
        data,
        AthleteEvidence,
        {"provider_id", "canonical_id", "name", "team"},
        label="athlete",
    )


def _appearance(value: Any) -> AppearanceEvidence | None:
    return _nested(
        value,
        AppearanceEvidence,
        {"provider_id", "appearance_type", "label"},
        label="appearance",
    )


def _event(value: Any) -> EventEvidence | None:
    if value is None:
        return None
    data = _mapping(value, label="event")
    expected = {
        "provider_id",
        "canonical_id",
        "label",
        "starts_at",
        "ends_at",
        "updated_at",
        "home_team",
        "away_team",
        "status_label",
    }
    _keys(data, expected, label="event")
    data = dict(data)
    data["home_team"] = _team(data["home_team"], label="event.home_team")
    data["away_team"] = _team(data["away_team"], label="event.away_team")
    return _nested(data, EventEvidence, expected, label="event")


def _statistic(value: Any) -> StatisticEvidence | None:
    if value is not None:
        data = _mapping(value, label="statistic")
        _keys(
            data,
            {"provider_id", "canonical_id", "label", "components"},
            label="statistic",
        )
        if not isinstance(data["components"], list):
            raise SnapshotCacheError("snapshot statistic components schema is invalid")
    return _nested(
        value,
        StatisticEvidence,
        {"provider_id", "canonical_id", "label", "components"},
        label="statistic",
    )


def _league(value: Any) -> LeagueEvidence | None:
    return _nested(
        value,
        LeagueEvidence,
        {"provider_id", "canonical_id", "label"},
        label="league",
    )


def _sport(value: Any) -> SportEvidence | None:
    return _nested(
        value,
        SportEvidence,
        {"provider_id", "canonical_id", "label"},
        label="sport",
    )


def _competition(value: Any) -> CompetitionEvidence | None:
    if value is None:
        return None
    data = _mapping(value, label="competition")
    expected = {"provider_id", "canonical_id", "label", "sport"}
    _keys(data, expected, label="competition")
    data = dict(data)
    data["sport"] = _sport(data["sport"])
    return _nested(data, CompetitionEvidence, expected, label="competition")


def _threshold(value: Any) -> MarketThreshold | None:
    return _nested(
        value,
        MarketThreshold,
        {"value", "unit", "original_value"},
        label="threshold",
    )


def _modifier(value: Any) -> SelectionModifier:
    data = _mapping(value, label="selection modifier")
    expected = {"value", "kind", "scope", "label"}
    _keys(data, expected, label="selection modifier")
    result = _nested(data, SelectionModifier, expected, label="selection modifier")
    assert result is not None  # the input is an object, not null
    return result


def _selection(value: Any) -> Selection:
    data = _mapping(value, label="selection")
    expected = {
        "selection_id",
        "label",
        "direction",
        "direction_label",
        "status",
        "modifiers",
        "american_price",
        "decimal_price",
    }
    _keys(data, expected, label="selection")
    modifiers = data["modifiers"]
    if not isinstance(modifiers, list):
        raise SnapshotCacheError("snapshot selection modifiers schema is invalid")
    decoded = dict(data)
    decoded["modifiers"] = tuple(_modifier(item) for item in modifiers)
    result = _nested(decoded, Selection, expected, label="selection")
    assert result is not None
    return result


def _market(value: Any) -> PlayerProjectionMarket:
    data = _mapping(value, label="market")
    expected = {
        "provider",
        "market_id",
        "athlete",
        "event",
        "team",
        "opponent",
        "league",
        "competition",
        "sport",
        "statistic",
        "threshold",
        "status",
        "status_label",
        "variant",
        "variant_label",
        "scoring_period",
        "scoring_period_label",
        "starts_at",
        "updated_at",
        "selections",
        "appearance",
    }
    _keys(data, expected, label="market")
    decoded = dict(data)
    decoded["athlete"] = _athlete(data["athlete"])
    decoded["event"] = _event(data["event"])
    decoded["team"] = _team(data["team"], label="market.team")
    decoded["opponent"] = _team(data["opponent"], label="market.opponent")
    decoded["league"] = _league(data["league"])
    decoded["competition"] = _competition(data["competition"])
    decoded["sport"] = _sport(data["sport"])
    decoded["statistic"] = _statistic(data["statistic"])
    decoded["threshold"] = _threshold(data["threshold"])
    if isinstance(decoded["scoring_period"], str):
        decoded["scoring_period"] = decoded["scoring_period"].replace("_", " ")
    selections = data["selections"]
    if not isinstance(selections, list):
        raise SnapshotCacheError("snapshot market selections schema is invalid")
    decoded["selections"] = tuple(_selection(item) for item in selections)
    decoded["appearance"] = _appearance(data["appearance"])
    result = _nested(decoded, PlayerProjectionMarket, expected, label="market")
    assert result is not None
    return result


def _coverage(value: Any) -> CoverageEvidence:
    data = _mapping(value, label="coverage")
    expected = {
        "fetched_count",
        "eligible_count",
        "normalized_count",
        "skipped_count",
        "pagination_complete",
        "fanout_complete",
        "expected_total",
        "warning_codes",
        "skipped_reasons",
        "diagnostic_details",
    }
    _keys(data, expected, label="coverage")
    for key in ("warning_codes", "skipped_reasons", "diagnostic_details"):
        if not isinstance(data[key], list):
            raise SnapshotCacheError(f"snapshot coverage {key} schema is invalid")
    result = _nested(
        dict(data),
        CoverageEvidence,
        expected,
        label="coverage",
    )
    assert result is not None
    return result


def serialize_provider_snapshot(snapshot: ProviderSnapshot) -> str:
    """Serialize one complete immutable snapshot using a strict versioned schema."""

    if not isinstance(snapshot, ProviderSnapshot):
        raise TypeError("snapshot must be ProviderSnapshot")
    if snapshot.status is not SnapshotStatus.COMPLETE:
        raise ValueError("only complete provider snapshots may be cached")
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "contract_version": snapshot.contract_version,
        "provider": snapshot.provider,
        "status": snapshot.status.value,
        "markets": _encoded(snapshot.markets),
        "coverage": _encoded(snapshot.coverage),
        "retrieved_at": _encoded(snapshot.retrieved_at),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def deserialize_provider_snapshot(
    payload: str | bytes | bytearray,
    *,
    expected_contract_version: str | None = None,
) -> ProviderSnapshot:
    """Validate and decode a Redis payload into a fresh immutable snapshot."""

    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            raise SnapshotCacheError("snapshot payload is not UTF-8") from error
    if not isinstance(payload, str):
        raise SnapshotCacheError("snapshot payload must be JSON text")
    try:
        raw = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise SnapshotCacheError("snapshot payload is not valid JSON") from error
    data = _mapping(raw, label="root")
    expected = {
        "schema",
        "schema_version",
        "contract_version",
        "provider",
        "status",
        "markets",
        "coverage",
        "retrieved_at",
    }
    _keys(data, expected, label="root")
    if data["schema"] != SNAPSHOT_SCHEMA or data["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotCacheError("snapshot schema version is incompatible")
    contract_version = data["contract_version"]
    if not isinstance(contract_version, str) or not contract_version.strip():
        raise SnapshotCacheError("snapshot contract version is invalid")
    if expected_contract_version is not None and contract_version != expected_contract_version:
        raise SnapshotCacheError("snapshot contract version is incompatible")
    if data["status"] != SnapshotStatus.COMPLETE.value:
        raise SnapshotCacheError("only complete snapshots may be cached")
    markets = data["markets"]
    if not isinstance(markets, list):
        raise SnapshotCacheError("snapshot markets schema is invalid")
    decoded = {
        "provider": data["provider"],
        "status": data["status"],
        "markets": tuple(_market(item) for item in markets),
        "coverage": _coverage(data["coverage"]),
        "retrieved_at": data["retrieved_at"],
        "contract_version": contract_version,
    }
    try:
        snapshot = ProviderSnapshot(**decoded)
    except (TypeError, ValueError, MalformedProviderResponseError) as error:
        raise SnapshotCacheError("snapshot values are incompatible") from error
    if snapshot.status is not SnapshotStatus.COMPLETE:
        raise SnapshotCacheError("cached snapshot is not complete")
    return snapshot


class _FutureLike(Protocol):
    def done(self) -> bool: ...

    def result(self, timeout: float | None = None) -> Any: ...

    def cancel(self) -> bool: ...


class _Executor(Protocol):
    def submit(self, fn: Callable[[], Any]) -> _FutureLike: ...


@dataclass(frozen=True, slots=True)
class SnapshotCacheResult:
    """One provider response together with bounded cache provenance."""

    snapshot: ProviderSnapshot
    cache_status: str
    age_seconds: float
    refresh_failure_reason: str | None = None
    refresh_failed_at: datetime | None = None

    @property
    def retrieved_at(self) -> datetime:
        """Expose the source timestamp without hiding it behind cache state."""

        return self.snapshot.retrieved_at

    @property
    def age(self) -> float:
        """Short alias useful to callers rendering cache age."""

        return self.age_seconds

    @property
    def failure_reason(self) -> str | None:
        """Short alias for the sanitized refresh failure reason."""

        return self.refresh_failure_reason

    @property
    def failure_at(self) -> datetime | None:
        """Short alias for the controlled time of a failed refresh."""

        return self.refresh_failed_at


@dataclass(slots=True)
class _Flight:
    future: _FutureLike


class ProviderSnapshotCacheCoordinator:
    """Coordinate one in-process refresh per provider/query cache key.

    The coordinator deliberately has no Redis locking.  It only suppresses
    duplicate work within one Python worker; separate workers may refresh the
    same key independently.
    """

    def __init__(
        self,
        *,
        executor: _Executor | None = None,
        max_workers: int = 3,
        redis_client: Any | None = None,
        enabled: bool | None = None,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
        stale_if_error_seconds: float = DEFAULT_STALE_IF_ERROR_SECONDS,
        contract_version: str = "1",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("snapshot cache max_workers must be a positive integer")
        self._owned_executor = executor is None
        self._executor: _Executor = executor or ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="statsplus-dfs-cache",
        )
        self._lock = Lock()
        self._flights: dict[str, _Flight] = {}
        self.redis_client = redis_client
        self.enabled = enabled
        self.fresh_seconds = fresh_seconds
        self.stale_if_error_seconds = stale_if_error_seconds
        self.contract_version = contract_version
        self.clock = clock
        if self._owned_executor:
            atexit.register(self.shutdown)

    def decorate(
        self,
        provider_name: str,
        provider: ProviderSnapshotProvider,
        **overrides: Any,
    ) -> "ProviderSnapshotCache":
        """Wrap one provider with this coordinator's shared cache policy."""

        values = {
            "redis_client": self.redis_client,
            "enabled": self.enabled,
            "fresh_seconds": self.fresh_seconds,
            "stale_if_error_seconds": self.stale_if_error_seconds,
            "contract_version": self.contract_version,
            "clock": self.clock,
            "coordinator": self,
        }
        values.update(overrides)
        return ProviderSnapshotCache(provider, provider_name=provider_name, **values)

    wrap = decorate

    def submit(
        self,
        key: str,
        function: Callable[[], ProviderSnapshot],
    ) -> tuple[_FutureLike, bool]:
        """Return the existing flight or submit one and become its owner."""

        with self._lock:
            current = self._flights.get(key)
            if current is not None:
                return current.future, False
            future = self._executor.submit(function)
            flight = _Flight(future=future)
            self._flights[key] = flight

        add_callback = getattr(future, "add_done_callback", None)
        if callable(add_callback):
            add_callback(lambda completed, cache_key=key: self._retire(cache_key, completed))
        if future.done():
            self._retire(key, future)
        return future, True

    def _retire(self, key: str, future: _FutureLike) -> None:
        with self._lock:
            flight = self._flights.get(key)
            if flight is not None and flight.future is future:
                self._flights.pop(key, None)

    def pending_count(self) -> int:
        """Return a bounded diagnostic count of active single-flight refreshes."""

        with self._lock:
            return len(self._flights)

    def shutdown(self) -> None:
        """Release an owned executor during process teardown/tests."""

        shutdown = getattr(self._executor, "shutdown", None)
        if callable(shutdown):
            shutdown(wait=False, cancel_futures=True)


class ProviderSnapshotCache:
    """Injectable cache decorator for one normalized DFS provider."""

    def __init__(
        self,
        provider: ProviderSnapshotProvider,
        *,
        provider_name: str | None = None,
        redis_client: Any | None = None,
        enabled: bool | None = None,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
        stale_if_error_seconds: float = DEFAULT_STALE_IF_ERROR_SECONDS,
        contract_version: str = "1",
        adapter_contract_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
        coordinator: ProviderSnapshotCacheCoordinator | None = None,
        executor: _Executor | None = None,
        operation: str = "get_snapshot",
    ) -> None:
        if not callable(getattr(provider, "get_snapshot", None)):
            raise TypeError("provider must implement get_snapshot")
        name = provider_name or getattr(provider, "provider_name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("provider_name must be a non-empty string")
        name = name.strip().casefold()
        if isinstance(fresh_seconds, bool) or not isinstance(fresh_seconds, (int, float)):
            raise ValueError("fresh_seconds must be a positive number")
        if isinstance(stale_if_error_seconds, bool) or not isinstance(
            stale_if_error_seconds, (int, float)
        ):
            raise ValueError("stale_if_error_seconds must be a positive number")
        if (
            fresh_seconds <= 0
            or stale_if_error_seconds <= 0
            or not isfinite(float(fresh_seconds))
            or not isfinite(float(stale_if_error_seconds))
        ):
            raise ValueError("snapshot cache windows must be positive")
        provider_contract = getattr(provider, "adapter_contract_version", None)
        if provider_contract is None:
            provider_contract = getattr(provider, "contract_version", None)
        contract = adapter_contract_version or (
            provider_contract
            if contract_version == "1" and isinstance(provider_contract, str)
            else contract_version
        )
        if not isinstance(contract, str) or not contract.strip():
            raise ValueError("contract_version must be a non-empty string")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-empty string")

        self.provider = provider
        self.provider_name = name
        self.redis_client = redis_client
        self.enabled = (
            bool(redis_client is not None)
            if enabled is None
            else bool(enabled) and redis_client is not None
        )
        self.fresh_seconds = float(fresh_seconds)
        self.stale_if_error_seconds = float(stale_if_error_seconds)
        self.contract_version = contract.strip()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.coordinator = coordinator or ProviderSnapshotCacheCoordinator(executor=executor)
        self.operation = operation.strip()
        self._last_result = local()

    @property
    def last_result(self) -> SnapshotCacheResult:
        """Return the most recent result on the current provider worker thread."""

        result = getattr(self._last_result, "value", None)
        if not isinstance(result, SnapshotCacheResult):
            raise RuntimeError("snapshot cache has not served a result on this thread")
        return result

    def get_last_result(self) -> SnapshotCacheResult | None:
        """Return thread-local provenance for board collectors without raising."""

        result = getattr(self._last_result, "value", None)
        return result if isinstance(result, SnapshotCacheResult) else None

    @property
    def cache_info(self) -> SnapshotCacheResult:
        """Alias for ``last_result`` used by board integrations."""

        return self.last_result

    def cache_key(self, query: NBAMarketQuery) -> str:
        """Build a semantic provider/query key with the adapter contract version."""

        if not isinstance(query, NBAMarketQuery):
            raise TypeError("query must be NBAMarketQuery")
        query_payload = {
            "sport": query.sport,
            "league": query.league,
            "market_statuses": sorted(status.value for status in query.market_statuses),
            "pregame_only": query.pregame_only,
        }
        digest = sha256(
            json.dumps(query_payload, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"statsplus:dfs:snapshot:v{self.contract_version}:{self.provider_name}:{digest}"

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Return a fresh, direct, or stale complete snapshot as appropriate."""

        if not isinstance(query, NBAMarketQuery):
            raise TypeError("query must be NBAMarketQuery")
        if not isinstance(context, RetrievalContext):
            raise TypeError("context must be RetrievalContext")
        # Provenance is per call/thread, not a fallback data store.
        self._last_result.__dict__.pop("value", None)
        now = self._clock_utc()
        if not self.enabled:
            self._record_cache_status("disabled")
            return self._direct_refresh(query, context, stale=None, now=now, cache_status="disabled")

        key = self.cache_key(query)
        cached: ProviderSnapshot | None = None
        read_status = "miss"
        try:
            raw = self.redis_client.get(key)
        except Exception:
            return self._direct_refresh(query, context, stale=None, now=now, cache_status="error")
        if raw is not None:
            try:
                cached = deserialize_provider_snapshot(
                    raw,
                    expected_contract_version=self.contract_version,
                )
            except SnapshotCacheError:
                read_status = "miss"
                delete = getattr(self.redis_client, "delete", None)
                if callable(delete):
                    try:
                        delete(key)
                    except Exception:
                        pass

        if cached is not None:
            age = self._age_seconds(cached, now)
            if age < self.fresh_seconds:
                self._set_result(cached, "hit", age)
                self._record_cache_hit()
                return cached
            if age <= self.stale_if_error_seconds:
                read_status = "stale_candidate"
                return self._direct_refresh(
                    query,
                    context,
                    stale=(cached, age),
                    now=now,
                    cache_status=read_status,
                )

        return self._direct_refresh(
            query,
            context,
            stale=None,
            now=now,
            cache_status=read_status,
        )

    def get_snapshot_with_metadata(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> SnapshotCacheResult:
        """Return the same provider result with explicit cache provenance."""

        snapshot = self.get_snapshot(query, context)
        del snapshot
        return self.last_result

    get_snapshot_with_cache_info = get_snapshot_with_metadata

    def _direct_refresh(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
        *,
        stale: tuple[ProviderSnapshot, float] | None,
        now: datetime,
        cache_status: str,
    ) -> ProviderSnapshot:
        key = self.cache_key(query)
        future, owner = self.coordinator.submit(
            key,
            lambda: self.provider.get_snapshot(query, context),
        )
        try:
            snapshot = self._await(future, context, owner=owner)
        except _EXPECTED_REFRESH_ERRORS as error:
            if stale is not None:
                cached, _initial_age = stale
                failure_at = self._clock_utc()
                age = self._age_seconds(cached, failure_at)
                if age <= self.stale_if_error_seconds:
                    self._set_result(
                        cached,
                        "stale",
                        age,
                        refresh_failure_reason=self._failure_reason(error),
                        refresh_failed_at=failure_at,
                    )
                    self._record_cache_status("stale")
                    return cached
            self._record_cache_status(
                "error" if cache_status == "error" else cache_status
            )
            raise
        except BaseException:
            # Preserve implementation defects.  The board contract deliberately
            # distinguishes expected upstream failures from programming bugs.
            raise

        if not isinstance(snapshot, ProviderSnapshot):
            raise TypeError("DFS provider get_snapshot must return ProviderSnapshot")
        if snapshot.provider != self.provider_name:
            raise ValueError("DFS provider snapshot provider does not match cache provider")
        age = self._age_seconds(snapshot, self._clock_utc())
        refresh_late = self._clock_utc() > context.deadline
        if snapshot.status is SnapshotStatus.COMPLETE:
            if (
                self.enabled
                and owner
                and not refresh_late
                and snapshot.contract_version == self.contract_version
            ):
                try:
                    self._write_redis(
                        key,
                        serialize_provider_snapshot(snapshot),
                        max(1, int(self.stale_if_error_seconds)),
                    )
                except Exception:
                    self._set_result(snapshot, "error", age)
                    self._record_cache_status("error")
                    return snapshot
            self._set_result(
                snapshot,
                cache_status if cache_status != "stale_candidate" else "miss",
                age,
            )
        else:
            # Partial observations are useful to this caller, but are never
            # written over the last complete Redis value.
            self._set_result(snapshot, cache_status if cache_status != "stale_candidate" else "miss", age)
        self._record_cache_status(self.last_result.cache_status)
        return snapshot

    def _await(
        self,
        future: _FutureLike,
        context: RetrievalContext,
        *,
        owner: bool,
    ) -> ProviderSnapshot:
        """Wait while preserving the absolute board deadline and cancellation."""

        while not future.done():
            now = self._clock_utc()
            if now >= context.deadline:
                if owner:
                    future.cancel()
                raise DeadlineExceededError("provider retrieval deadline exceeded")
            remaining = max(0.0, (context.deadline - now).total_seconds())
            try:
                result = future.result(timeout=min(0.05, remaining))
            except TimeoutError:
                continue
            except CancelledError as error:
                raise DeadlineExceededError("provider retrieval deadline exceeded") from error
            return result
        try:
            return future.result(timeout=0)
        except CancelledError as error:
            raise DeadlineExceededError("provider retrieval deadline exceeded") from error

    def _set_result(
        self,
        snapshot: ProviderSnapshot,
        cache_status: str,
        age: float,
        *,
        refresh_failure_reason: str | None = None,
        refresh_failed_at: datetime | None = None,
    ) -> None:
        self._last_result.value = SnapshotCacheResult(
            snapshot=snapshot,
            cache_status=cache_status,
            age_seconds=max(0.0, float(age)),
            refresh_failure_reason=refresh_failure_reason,
            refresh_failed_at=refresh_failed_at,
        )

    def _write_redis(self, key: str, payload: str, ttl: int) -> None:
        setex = getattr(self.redis_client, "setex", None)
        if callable(setex):
            setex(key, ttl, payload)
            return
        setter = getattr(self.redis_client, "set", None)
        if not callable(setter):
            raise AttributeError("Redis client does not support setex or set")
        setter(key, payload, ex=ttl)

    def _record_cache_hit(self) -> None:
        from app.utils.telemetry import CACHE_HIT, record_cached_provider_event

        record_cached_provider_event(self.provider_name, self.operation, CACHE_HIT)

    def _record_cache_status(self, status: str) -> None:
        from app.utils.telemetry import record_cache_status

        record_cache_status(self.provider_name, status)

    def _clock_utc(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot cache clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _age_seconds(snapshot: ProviderSnapshot, now: datetime) -> float:
        return max(0.0, (now - snapshot.retrieved_at).total_seconds())

    @staticmethod
    def _failure_reason(error: BaseException) -> str:
        # Keep stale fallback diagnostics bounded and independent of provider
        # exception text/credentials.
        if isinstance(error, DeadlineExceededError):
            return "deadline_exceeded"
        if isinstance(error, requests.exceptions.Timeout) or isinstance(error, TimeoutError):
            return "timeout"
        if isinstance(error, (MalformedProviderResponseError, ProviderResponseError)):
            return "malformed_response"
        if isinstance(error, requests.exceptions.HTTPError):
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 429:
                return "rate_limited"
            if status in {401, 403}:
                return "access_denied"
        if isinstance(error, ProviderUnavailableError):
            reason = getattr(error, "provider_reason", None)
            if reason in {
                "timeout",
                "deadline_exceeded",
                "rate_limited",
                "access_denied",
                "malformed_response",
                "upstream_error",
            }:
                return str(reason)
        return "upstream_error"


_EXPECTED_REFRESH_ERRORS = (
    DeadlineExceededError,
    ProviderUnavailableError,
    ProviderResponseError,
    MalformedProviderResponseError,
    TimeoutError,
    requests.exceptions.RequestException,
)


def decorate_provider(
    provider: ProviderSnapshotProvider,
    *,
    provider_name: str,
    **kwargs: Any,
) -> ProviderSnapshotCache:
    """Build a cache decorator using the shared public provider seam."""

    return ProviderSnapshotCache(provider, provider_name=provider_name, **kwargs)


# Descriptive aliases keep the injected seam discoverable to callers that
# refer to the component as a decorator/coordinator pair.
ProviderSnapshotCacheDecorator = ProviderSnapshotCache
SnapshotCacheCoordinator = ProviderSnapshotCacheCoordinator
CachedProviderSnapshot = SnapshotCacheResult


__all__ = [
    "DEFAULT_FRESH_SECONDS",
    "DEFAULT_STALE_IF_ERROR_SECONDS",
    "SNAPSHOT_SCHEMA",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotCacheError",
    "deserialize_provider_snapshot",
    "serialize_provider_snapshot",
    "ProviderSnapshotCache",
    "ProviderSnapshotCacheCoordinator",
    "ProviderSnapshotCacheDecorator",
    "SnapshotCacheCoordinator",
    "CachedProviderSnapshot",
    "SnapshotCacheResult",
    "decorate_provider",
]
