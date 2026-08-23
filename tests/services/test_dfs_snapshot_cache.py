"""TDD contract tests for the injected DFS provider-snapshot cache."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Lock, Thread, current_thread

import pytest

from app.domain.statistics import (
    MatchReason,
    MatchState,
    ScoringPeriod,
    StatisticMatch,
)
from app.providers.dfs import (
    CoverageEvidence,
    DeadlineExceededError,
    MarketStatus,
    AthleteEvidence,
    AppearanceEvidence,
    EventEvidence,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    PriceKind,
    PriceScope,
    ProviderSnapshot,
    RetrievalContext,
    Selection,
    SelectionDirection,
    SelectionModifier,
    StatisticEvidence,
    TeamEvidence,
    SnapshotStatus,
)
from app.services.dfs_snapshot_cache import (
    COMPARE_AND_DELETE_SCRIPT,
    SNAPSHOT_SCHEMA_VERSION,
    deserialize_provider_snapshot,
    ProviderSnapshotCache,
    ProviderSnapshotCacheCoordinator,
    SnapshotCacheError,
    SnapshotCacheResult,
    serialize_provider_snapshot,
)
from app.services.dfs_board import DFSBoardService
from app.utils import telemetry
from app.utils.telemetry import ProviderResponseError


_RETRIEVED_AT = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)


def _snapshot(provider: str = "dabble") -> ProviderSnapshot:
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=(),
        coverage=CoverageEvidence(
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=_RETRIEVED_AT,
    )


def _canonical(payload: dict) -> str:
    """Re-serialize a mutated payload with the exact canonical wire bytes.

    A pretty-printed or key-reordered re-dump is rejected outright, so a test
    for one specific field must keep the rest of the document canonical.
    """

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def test_complete_provider_snapshot_serializes_and_round_trips_immutably() -> None:
    original = _snapshot()

    payload = serialize_provider_snapshot(original)
    restored = deserialize_provider_snapshot(payload, expected_contract_version="1")

    assert isinstance(payload, str)
    assert restored == original
    assert restored is not original
    assert restored.status is SnapshotStatus.COMPLETE
    assert restored.retrieved_at == _RETRIEVED_AT


def test_snapshot_codec_preserves_nested_decimal_and_evidence_values() -> None:
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="market-1",
        athlete=AthleteEvidence(
            provider_id="athlete-1",
            canonical_id=23,
            name="LeBron James",
            team=TeamEvidence(provider_id="team-1", name="Los Angeles Lakers"),
        ),
        event=EventEvidence(
            provider_id="game-1",
            starts_at=_RETRIEVED_AT,
            home_team=TeamEvidence(abbreviation="LAL"),
        ),
        statistic=StatisticEvidence(label="Points", components=("points",)),
        threshold=MarketThreshold(
            value=Decimal("25.500"),
            unit="points",
            original_value="25.5 pts",
        ),
        selections=(
            Selection(
                selection_id="selection-1",
                direction="higher",
                modifiers=(
                    SelectionModifier(
                        value=Decimal("1.10"),
                        kind="multiplier",
                        scope="selection",
                    ),
                ),
            ),
        ),
        appearance=AppearanceEvidence(provider_id="appearance-1"),
    )
    original = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=_RETRIEVED_AT,
    )

    restored = deserialize_provider_snapshot(serialize_provider_snapshot(original))

    assert restored == original
    assert restored.markets[0].threshold is not None
    assert restored.markets[0].threshold.value == Decimal("25.500")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("schema_version", True),
        lambda payload: payload.__setitem__("provider", "DABBLE"),
        lambda payload: payload["query"].__setitem__("sport", "nba"),
        lambda payload: payload["query"].__setitem__("pregame_only", 1),
    ],
)
def test_snapshot_codec_rejects_noncanonical_wire_values(mutate) -> None:
    payload = json.loads(serialize_provider_snapshot(_snapshot()))
    mutate(payload)

    with pytest.raises(ValueError):
        deserialize_provider_snapshot(
            _canonical(payload),
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


def _market_payload_snapshot() -> ProviderSnapshot:
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="market-1",
        athlete=AthleteEvidence(
            provider_id="athlete-1",
            name="LeBron James",
            team=TeamEvidence(provider_id="team-1", name="Los Angeles Lakers"),
        ),
        event=EventEvidence(
            provider_id="game-1",
            home_team=TeamEvidence(abbreviation="LAL"),
        ),
        statistic=StatisticEvidence(label="Points", components=("points",)),
        threshold=MarketThreshold(value=Decimal("25.5"), unit="points"),
        selections=(Selection(selection_id="selection-1", direction="higher"),),
    )
    return ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=_RETRIEVED_AT,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        # A constructor would quietly drop a non-string nested name.
        lambda market: market["athlete"]["team"].__setitem__("name", 123),
        lambda market: market["event"]["home_team"].__setitem__("abbreviation", "lal"),
        lambda market: market["athlete"].__setitem__("name", "  LeBron James  "),
        # Aliases must not be canonicalized on the way out of Redis.
        lambda market: market.__setitem__("status", "open"),
        lambda market: market.__setitem__("variant", "main"),
        lambda market: market.__setitem__("scoring_period", "full game"),
        lambda market: market["selections"][0].__setitem__("direction", "over"),
        lambda market: market["statistic"].__setitem__("components", [" points "]),
        lambda market: market["threshold"].__setitem__("value", 25.5),
        # Statistic resolution happens above the cache, so a match on the wire
        # is not a value this codec wrote.
        lambda market: market.__setitem__("statistic_match", {}),
    ],
)
def test_snapshot_codec_rejects_invalid_or_aliased_nested_wire_fields(mutate) -> None:
    payload = json.loads(serialize_provider_snapshot(_market_payload_snapshot()))
    mutate(payload["markets"][0])

    with pytest.raises(ValueError):
        deserialize_provider_snapshot(
            _canonical(payload),
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


def _selection_snapshot(selection: Selection) -> ProviderSnapshot:
    return replace(
        _market_payload_snapshot(),
        markets=(replace(_market_payload_snapshot().markets[0], selections=(selection,)),),
    )


@pytest.mark.parametrize(
    "selection",
    [
        # The provider named no direction at all, so the market carries the
        # closed UNKNOWN value and no provider label to preserve.
        Selection(selection_id="s-1"),
        Selection(selection_id="s-1", direction=SelectionDirection.UNKNOWN),
        Selection(selection_id="s-1", direction=SelectionDirection.HIGHER),
        Selection(selection_id="s-1", direction="higher"),
        # An unfamiliar provider word is retained verbatim as the label.
        Selection(selection_id="s-1", direction="over"),
        Selection(selection_id="s-1", direction="provider-specific"),
        Selection(selection_id="s-1", direction=None, direction_label="unknown"),
    ],
)
def test_snapshot_codec_round_trips_every_selection_direction(selection) -> None:
    original = _selection_snapshot(selection)

    payload = serialize_provider_snapshot(original)
    restored = deserialize_provider_snapshot(
        payload,
        expected_contract_version="1",
        expected_provider="dabble",
        expected_query=NBAMarketQuery(),
    )

    assert restored == original
    decoded = restored.markets[0].selections[0]
    assert decoded.direction is selection.direction
    assert decoded.direction_label == selection.direction_label


@pytest.mark.parametrize(
    "mutate",
    [
        # An alias the constructor would canonicalize into a different pair of
        # values is corrupt wire data, not a value this codec ever wrote.
        lambda selection: selection.update({"direction": "over", "direction_label": None}),
        lambda selection: selection.update(
            {"direction": "Unknown", "direction_label": None}
        ),
        lambda selection: selection.update(
            {"direction": "unknown", "direction_label": "higher"}
        ),
        lambda selection: selection.update({"direction": 1, "direction_label": None}),
    ],
)
def test_snapshot_codec_rejects_a_noncanonical_wire_direction(mutate) -> None:
    payload = json.loads(
        serialize_provider_snapshot(_selection_snapshot(Selection(selection_id="s-1")))
    )
    mutate(payload["markets"][0]["selections"][0])

    with pytest.raises(ValueError):
        deserialize_provider_snapshot(
            _canonical(payload),
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


def test_snapshot_codec_refuses_to_write_a_resolved_market() -> None:
    snapshot = _market_payload_snapshot()
    resolved = replace(
        snapshot,
        markets=(
            replace(
                snapshot.markets[0],
                statistic_match=StatisticMatch(
                    state=MatchState.UNMAPPED,
                    evidence=StatisticEvidence(label="Points"),
                    scoring_period=ScoringPeriod.UNKNOWN,
                    reason=MatchReason.UNKNOWN_SCORING_PERIOD,
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="unresolved"):
        serialize_provider_snapshot(resolved)


def test_snapshot_codec_rejects_noncanonical_contract_version_whitespace() -> None:
    payload = json.loads(serialize_provider_snapshot(_snapshot()))
    payload["contract_version"] = " 1 "

    with pytest.raises(ValueError):
        deserialize_provider_snapshot(_canonical(payload))


@pytest.mark.parametrize(
    "mutate",
    [
        # A conflicting duplicate at the root: the last value would silently win.
        lambda text: '{"schema":"not-statsplus",' + text[1:],
        lambda text: '{"provider":"underdog",' + text[1:],
        lambda text: '{"schema_version":2,' + text[1:],
        # Nested objects are just as forgeable as the root document.
        lambda text: text.replace('"team":{', '"team":{"name":"Boston Celtics",', 1),
        lambda text: text.replace('"query":{', '"query":{"pregame_only":false,', 1),
        lambda text: text.replace(
            '"selections":[{', '"selections":[{"american_price":-115,', 1
        ),
    ],
)
def test_snapshot_codec_rejects_duplicate_wire_keys_at_any_depth(mutate) -> None:
    """A duplicate key means two conflicting documents share one payload."""

    payload = mutate(serialize_provider_snapshot(_market_payload_snapshot()))

    with pytest.raises(ValueError):
        deserialize_provider_snapshot(
            payload,
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: f"  {text}",
        lambda text: f"{text}\n",
        lambda text: "\ufeff" + text,
        # A semantically equal document whose bytes are not the canonical ones.
        lambda text: json.dumps(json.loads(text), indent=2, sort_keys=True),
        lambda text: json.dumps(json.loads(text), sort_keys=True),
        lambda text: text.replace('"dabble"', '"\\u0064abble"', 1),
    ],
)
def test_snapshot_codec_requires_the_exact_canonical_wire_bytes(mutate) -> None:
    """The payload must be the bytes this codec writes, not an equal re-dump."""

    payload = mutate(serialize_provider_snapshot(_snapshot()))

    with pytest.raises(ValueError):
        deserialize_provider_snapshot(
            payload,
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        # int(float("inf")) raises OverflowError, not ValueError.
        lambda text: text.replace('"american_price":null', '"american_price":1e309', 1),
        # Converting this timestamp to UTC leaves the representable range.
        lambda text: text.replace(
            '"starts_at":null', '"starts_at":"9999-12-31T23:59:59-05:00"', 1
        ),
    ],
)
def test_snapshot_codec_rejects_values_no_domain_constructor_can_represent(mutate) -> None:
    """A numeric or domain conversion failure is corrupt data, not a defect."""

    payload = mutate(serialize_provider_snapshot(_market_payload_snapshot()))

    with pytest.raises(SnapshotCacheError):
        deserialize_provider_snapshot(
            payload,
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


#: Nested far past any plausible interpreter recursion limit, so the parser
#: fails the same way on every platform this runs on.
_EXCESSIVE_NESTING_DEPTH = 100_000


def _deeply_nested_payload() -> str:
    return "[" * _EXCESSIVE_NESTING_DEPTH + "]" * _EXCESSIVE_NESTING_DEPTH


def test_snapshot_codec_rejects_excessively_nested_payloads() -> None:
    """Deep nesting exhausts the parser's stack, not the caller's error path."""

    with pytest.raises(SnapshotCacheError):
        deserialize_provider_snapshot(
            _deeply_nested_payload(),
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


#: Past the interpreter's digit limit for integer string conversion, so the
#: parser rejects the token with a plain ValueError rather than a
#: JSONDecodeError.
_OVERSIZED_INTEGER = "1" * 5000


def test_snapshot_codec_rejects_oversized_integer_tokens() -> None:
    """A digit-limit ValueError is corrupt wire data, not a defect."""

    payload = serialize_provider_snapshot(_market_payload_snapshot()).replace(
        '"american_price":null', f'"american_price":{_OVERSIZED_INTEGER}', 1
    )

    with pytest.raises(SnapshotCacheError):
        deserialize_provider_snapshot(
            payload,
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


#: The label fields a market keeps exactly as given.  Every other label is
#: normalized, so a non-string there already fails the canonical comparison.
_MARKET_LABEL_FIELDS = ("status_label", "variant_label", "scoring_period_label")

#: ``json.dumps`` writes these back unchanged, so a payload carrying one would
#: round-trip as canonical unless the codec refuses the token outright.
_NONSTANDARD_CONSTANTS = (float("nan"), float("inf"), float("-inf"))


def _payload_with_market_label(field: str, value: object) -> str:
    """Write a market label field with Python's permissive JSON encoder."""

    payload = json.loads(serialize_provider_snapshot(_market_payload_snapshot()))
    payload["markets"][0][field] = value
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


@pytest.mark.parametrize("field", _MARKET_LABEL_FIELDS)
@pytest.mark.parametrize("value", [*_NONSTANDARD_CONSTANTS, 5, 5.5, True, ["over"]])
def test_snapshot_codec_rejects_nonstring_market_labels(field, value) -> None:
    """A market keeps these labels verbatim, so the wire type must be exact."""

    with pytest.raises(SnapshotCacheError):
        deserialize_provider_snapshot(
            _payload_with_market_label(field, value),
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


@pytest.mark.parametrize("value", [*_NONSTANDARD_CONSTANTS, 7])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda market, value: market["event"].__setitem__("status_label", value),
        lambda market, value: market["event"].__setitem__("label", value),
        lambda market, value: market["athlete"]["team"].__setitem__("name", value),
        lambda market, value: market["statistic"].__setitem__("label", value),
        lambda market, value: market["selections"][0].__setitem__("label", value),
        lambda market, value: market["threshold"].__setitem__("original_value", value),
    ],
)
def test_snapshot_codec_rejects_nonstring_nested_text_fields(mutate, value) -> None:
    """Every optional text field on the wire is a string or null, nothing else."""

    payload = json.loads(serialize_provider_snapshot(_market_payload_snapshot()))
    mutate(payload["markets"][0], value)

    with pytest.raises(SnapshotCacheError):
        deserialize_provider_snapshot(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            expected_contract_version="1",
            expected_provider="dabble",
            expected_query=NBAMarketQuery(),
        )


@pytest.mark.parametrize("value", _NONSTANDARD_CONSTANTS)
def test_snapshot_codec_never_writes_a_nonstandard_constant(value) -> None:
    """The encoder must not emit a token no standard JSON reader accepts."""

    snapshot = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(PlayerProjectionMarket(provider="dabble", status_label=value),),
        coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
        retrieved_at=_RETRIEVED_AT,
    )

    with pytest.raises(ValueError):
        serialize_provider_snapshot(snapshot)


def test_snapshot_codec_keeps_the_duplicate_key_failure_distinct() -> None:
    """Mapping parser failures must not swallow the duplicate-key decision."""

    payload = serialize_provider_snapshot(_snapshot()).replace(
        '"provider":"dabble"', '"provider":"dabble","provider":"dabble"', 1
    )

    with pytest.raises(SnapshotCacheError, match="duplicate keys"):
        deserialize_provider_snapshot(payload, expected_contract_version="1")


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.deleted: list[str] = []
        self.get_error: BaseException | None = None
        self.set_error: BaseException | None = None

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        if self.get_error is not None:
            raise self.get_error
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str) -> bool:
        if self.set_error is not None:
            raise self.set_error
        self.values[key] = value
        self.ttls[key] = ttl
        self.set_calls.append((key, value, ttl))
        return True

    def eval(self, script: str, numkeys: int, key: str, argument: str) -> int:
        # Only the module's compare-and-delete script is expected here.
        assert script == COMPARE_AND_DELETE_SCRIPT
        assert numkeys == 1
        if self.values.get(key) != argument:
            return 0
        del self.values[key]
        self.deleted.append(key)
        return 1


class ControlledClock:
    def __init__(self, now: datetime = _RETRIEVED_AT) -> None:
        self.now_value = now

    def now(self) -> datetime:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)


class FakeProvider:
    def __init__(self, snapshot: ProviderSnapshot, *, error: BaseException | None = None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[tuple[NBAMarketQuery, RetrievalContext]] = []
        self.lock = Lock()

    def get_snapshot(
        self, query: NBAMarketQuery, context: RetrievalContext
    ) -> ProviderSnapshot:
        with self.lock:
            self.calls.append((query, context))
        if self.error is not None:
            raise self.error
        return self.snapshot


def _context() -> RetrievalContext:
    # Deadline semantics are covered with injected clocks below;  the ordinary
    # cases must not become wall-clock dependent.
    return RetrievalContext(
        deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
        request_id="cache-test",
    )


def test_fresh_cache_hit_uses_provider_query_key_and_exposes_age() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )

    first = cache.get_snapshot(NBAMarketQuery(), _context())
    clock.advance(60)
    second = cache.get_snapshot(NBAMarketQuery(), _context())

    assert first == second == provider.snapshot
    assert len(provider.calls) == 1
    assert len(redis.set_calls) == 1
    assert redis.set_calls[0][2] == 1800
    # One read per call, plus the read the first call's refresh makes inside
    # its own flight before reaching the provider.
    assert len(redis.get_calls) == 3
    assert cache.last_result.cache_status == "hit"
    assert cache.last_result.age_seconds == 60
    assert cache.cache_key(NBAMarketQuery()).startswith(
        "statsplus:dfs:snapshot:v1:dabble:"
    )


def test_corrupt_or_incompatible_payload_is_a_miss_and_is_replaced() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = '{"schema":"not-statsplus"}'

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert len(provider.calls) == 1
    assert len(redis.set_calls) == 1
    assert redis.values[key] == redis.set_calls[0][1]

    redis.values[key] = serialize_provider_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
            retrieved_at=_RETRIEVED_AT,
            contract_version="old",
        )
    )
    cache.get_snapshot(NBAMarketQuery(), _context())
    assert len(provider.calls) == 2


def test_current_partial_result_never_replaces_last_complete_snapshot() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    complete = _snapshot()
    partial = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.PARTIAL,
        markets=(PlayerProjectionMarket(provider="dabble"),),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            skipped_count=0,
            pagination_complete=False,
        ),
        retrieved_at=_RETRIEVED_AT,
    )
    provider = FakeProvider(partial)
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(complete)
    clock.advance(600)

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is partial
    assert redis.values[key] == serialize_provider_snapshot(complete)
    assert redis.set_calls == []
    assert cache.last_result.cache_status == "miss"


def test_stale_complete_is_served_only_after_total_refresh_failure_with_metadata() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(
        _snapshot(),
        error=TimeoutError("upstream unavailable"),
    )
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(_snapshot())
    clock.advance(600)

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result == provider.snapshot
    assert len(provider.calls) == 1
    assert cache.last_result.cache_status == "stale"
    assert cache.last_result.age_seconds == 600
    assert cache.last_result.refresh_failure_reason == "timeout"
    assert cache.last_result.refresh_failed_at == clock.now_value
    assert cache.last_result.retrieved_at == _RETRIEVED_AT


def test_stale_failure_reason_is_sanitized_for_malformed_provider_payloads() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=ProviderResponseError("raw body leaked"))
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(_snapshot())
    clock.advance(600)

    cache.get_snapshot(NBAMarketQuery(), _context())

    assert cache.last_result.refresh_failure_reason == "malformed_response"
    assert "raw body" not in repr(cache.last_result)


def _signal_when_a_follower_joins(cache: ProviderSnapshotCache) -> Event:
    """Report the moment a caller adopts an existing flight instead of owning one.

    Starting a second thread does not establish that it reached the
    coordinator. If the owner retires its flight first, the follower opens a
    second one and the provider is called twice, so a test that releases the
    owner on thread start alone is asserting against a schedule it never
    arranged. Waiting on this event arranges it.
    """

    joined = Event()
    coordinator_submit = cache.coordinator.submit

    def submitting(*args, **kwargs):
        flight, owner = coordinator_submit(*args, **kwargs)
        if not owner:
            joined.set()
        return flight, owner

    cache.coordinator.submit = submitting  # type: ignore[method-assign]
    return joined


def test_concurrent_same_key_refresh_is_single_flight_and_publishes_once() -> None:
    redis = FakeRedis()
    provider = FakeProvider(_snapshot())
    started = Event()
    release = Event()

    original_get_snapshot = provider.get_snapshot

    def blocking_get_snapshot(query, context):
        started.set()
        # Outlasts the wait for the follower to join, so the owner is released
        # by the test rather than by its own timeout.
        release.wait(timeout=5)
        return original_get_snapshot(query, context)

    provider.get_snapshot = blocking_get_snapshot  # type: ignore[method-assign]
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=ControlledClock().now,
    )
    results: list[ProviderSnapshot] = []
    errors: list[BaseException] = []

    joined = _signal_when_a_follower_joins(cache)

    def retrieve() -> None:
        try:
            results.append(cache.get_snapshot(NBAMarketQuery(), _context()))
        except BaseException as error:  # pragma: no cover - diagnostic assertion below
            errors.append(error)

    first = Thread(target=retrieve)
    second = Thread(target=retrieve)
    first.start()
    assert started.wait(timeout=1)
    second.start()
    assert joined.wait(timeout=2)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert results == [provider.snapshot, provider.snapshot]
    assert len(provider.calls) == 1
    assert len(redis.set_calls) == 1


class ManualFuture:
    def __init__(self, *, cancel_result: bool = True) -> None:
        self.value: ProviderSnapshot | None = None
        self.error: BaseException | None = None
        self.cancelled = False
        self.cancel_result = cancel_result
        self.callbacks: list[object] = []

    def done(self) -> bool:
        return self.value is not None or self.error is not None or self.cancelled

    def result(self, timeout: float | None = None) -> ProviderSnapshot:
        del timeout
        if self.cancelled:
            from concurrent.futures import CancelledError

            raise CancelledError()
        if self.error is not None:
            raise self.error
        if self.value is None:
            raise TimeoutError()
        return self.value

    def cancel(self) -> bool:
        if not self.cancel_result:
            return False
        self.cancelled = True
        for callback in self.callbacks:
            callback(self)
        return True

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def complete(self, value: ProviderSnapshot) -> None:
        self.value = value
        for callback in self.callbacks:
            callback(self)


class ManualExecutor:
    def __init__(self, *, cancel_result: bool = True) -> None:
        self.future = ManualFuture(cancel_result=cancel_result)

    def submit(self, function):
        self.function = function
        return self.future


class CompletedFuture:
    def __init__(self, value: object) -> None:
        self.value = value

    def done(self) -> bool:
        return True

    def result(self, timeout: float | None = None) -> object:
        del timeout
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    def cancel(self) -> bool:
        return False


class ImmediateExecutor:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    def submit(self, function):
        self.calls += 1
        del function
        return CompletedFuture(self.value)


def test_deadline_cancels_pending_refresh_and_late_result_is_not_published() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    executor = ManualExecutor()
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        executor=executor,
    )
    result: dict[str, BaseException] = {}
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="cache-test",
    )

    thread = Thread(
        target=lambda: _capture_error(
            result,
            lambda: cache.get_snapshot(NBAMarketQuery(), context),
        )
    )
    thread.start()
    clock.advance(2)
    thread.join(timeout=2)
    assert isinstance(result["error"], TimeoutError)
    assert executor.future.cancelled

    executor.future.complete(_snapshot())
    assert redis.set_calls == []


def _run_follower(cache: ProviderSnapshotCache, *, seconds: float = 20) -> dict[str, object]:
    """Join an active flight from another thread and capture its outcome."""

    result: dict[str, object] = {}
    follower = Thread(
        target=lambda: _capture_value(
            result,
            lambda: cache.get_snapshot(
                NBAMarketQuery(),
                RetrievalContext(
                    deadline=_RETRIEVED_AT + timedelta(seconds=seconds),
                    request_id="follower",
                ),
            ),
        ),
        # A follower that is left waiting on an undecided flight must fail the
        # assertion below rather than hang the suite at interpreter exit.
        daemon=True,
    )
    follower.start()
    follower.join(timeout=2)
    assert not follower.is_alive()
    return result


def test_uncancellable_owner_deadline_failure_is_shared_with_later_followers() -> None:
    """Work the owner could not cancel never becomes another caller's success.

    The follower's deadline is far later than the owner's, but the flight has
    already been decided: it adopts that failure verbatim.
    """

    redis = FakeRedis()
    clock = ControlledClock()
    executor = ManualExecutor(cancel_result=False)
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        executor=executor,
    )
    owner_error: dict[str, BaseException] = {}
    owner_context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="owner-timeout",
    )
    owner = Thread(
        target=lambda: _capture_error(
            owner_error,
            lambda: cache.get_snapshot(NBAMarketQuery(), owner_context),
        )
    )
    owner.start()
    clock.advance(2)
    owner.join(timeout=2)

    assert isinstance(owner_error["error"], TimeoutError)
    assert not executor.future.cancelled
    assert cache.coordinator.pending_count() == 1

    follower_result = _run_follower(cache)

    assert follower_result["error"] is owner_error["error"]
    assert "value" not in follower_result
    # The uncancellable refresh is still running, so the key stays active.
    assert cache.coordinator.pending_count() == 1

    executor.future.complete(_snapshot())

    assert cache.coordinator.pending_count() == 0
    assert redis.set_calls == []
    assert redis.values == {}


def _capture_value(target: dict[str, object], function) -> None:
    try:
        target["value"] = function()
    except BaseException as error:  # pragma: no cover - diagnostic assertion below
        target["error"] = error


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        (object(), TypeError),
        (_snapshot("underdog"), ValueError),
        (
            ProviderSnapshot(
                provider="dabble",
                status=SnapshotStatus.COMPLETE,
                markets=(),
                coverage=CoverageEvidence(
                    pagination_complete=True,
                    fanout_complete=True,
                ),
                retrieved_at=_RETRIEVED_AT,
                contract_version="2",
            ),
            ValueError,
        ),
    ],
)
def test_invalid_refresh_result_resolves_and_retires_flight(value, error_type) -> None:
    redis = FakeRedis()
    executor = ImmediateExecutor(value)
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=redis,
        executor=executor,
    )

    with pytest.raises(error_type):
        cache.get_snapshot(NBAMarketQuery(), _context())

    assert cache.coordinator.pending_count() == 0
    assert redis.values == {}
    with pytest.raises(error_type):
        cache.get_snapshot(NBAMarketQuery(), _context())
    assert executor.calls == 2


def test_deadline_after_redis_read_does_not_return_fresh_value() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(_snapshot())

    original_get = redis.get

    def late_get(read_key: str):
        value = original_get(read_key)
        clock.advance(2)
        return value

    redis.get = late_get  # type: ignore[method-assign]
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="late-read",
    )

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), context)

    assert provider.calls == []


def test_stale_follower_receives_complete_cache_result_metadata() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    started = Event()
    release = Event()
    original_get_snapshot = provider.get_snapshot

    def blocking_failure(query, context):
        started.set()
        release.wait(timeout=2)
        return original_get_snapshot(query, context)

    provider.get_snapshot = blocking_failure  # type: ignore[method-assign]
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(_snapshot())
    clock.advance(600)
    context = _context()
    results: list[SnapshotCacheResult] = []
    errors: list[BaseException] = []

    def retrieve() -> None:
        try:
            results.append(cache.get_snapshot_with_metadata(NBAMarketQuery(), context))
        except BaseException as error:  # pragma: no cover - diagnostic assertion below
            errors.append(error)

    first = Thread(target=retrieve)
    second = Thread(target=retrieve)
    first.start()
    assert started.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0].cache_status == "stale"
    assert results[0].age_seconds == 600
    assert results[0].refresh_failure_reason == "timeout"
    assert results[0].refresh_failed_at == clock.now_value


def test_late_cleanup_does_not_delete_a_newer_redis_value() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    newer = serialize_provider_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(
                pagination_complete=True,
                fanout_complete=True,
            ),
            retrieved_at=_RETRIEVED_AT + timedelta(seconds=1),
        )
    )
    original_setex = redis.setex

    def publish_then_race(write_key: str, ttl: int, payload: str) -> bool:
        result = original_setex(write_key, ttl, payload)
        redis.values[write_key] = newer
        clock.advance(2)
        return result

    redis.setex = publish_then_race  # type: ignore[method-assign]
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="late-write",
    )

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), context)

    assert redis.values[key] == newer
    assert redis.deleted == []


def test_deadline_late_work_is_never_published_or_observed() -> None:
    """Work that finishes at or after the deadline must never reach Redis.

    Publication is decided once, before the write, so no concurrent reader can
    observe a value that a later retraction would have to remove again.
    """

    redis = FakeRedis()
    clock = ControlledClock()
    reader_ready = Event()
    work_done = Event()

    def late_work(query, context):
        del query, context
        reader_ready.wait(timeout=2)
        clock.advance(2)  # the provider finished past the caller's deadline
        work_done.set()
        return _snapshot()

    provider = FakeProvider(_snapshot())
    provider.get_snapshot = late_work  # type: ignore[method-assign]
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="late-work",
    )
    observations: set[str | None] = set()
    stop = Event()

    def observe() -> None:
        reader_ready.set()
        while not stop.is_set():
            observations.add(redis.values.get(key))
        observations.add(redis.values.get(key))

    reader = Thread(target=observe)
    reader.start()
    owner_error: dict[str, BaseException] = {}
    _capture_error(
        owner_error,
        lambda: cache.get_snapshot(NBAMarketQuery(), context),
    )
    stop.set()
    reader.join(timeout=2)

    assert work_done.is_set()
    assert isinstance(owner_error["error"], TimeoutError)
    assert redis.set_calls == []
    assert redis.deleted == []
    assert observations == {None}


def test_publication_decided_before_the_deadline_is_never_retracted() -> None:
    """A write that returns late still holds pre-deadline work, so it stands.

    Retracting it would create exactly the window where a concurrent reader
    sees a value that then disappears.
    """

    redis = FakeRedis()
    clock = ControlledClock()
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    original_setex = redis.setex

    def slow_setex(write_key: str, ttl: int, payload: str) -> bool:
        result = original_setex(write_key, ttl, payload)
        clock.advance(2)
        return result

    redis.setex = slow_setex  # type: ignore[method-assign]
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="late-write",
    )

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), context)

    assert redis.deleted == []
    assert redis.values[key] == redis.set_calls[0][1]
    assert deserialize_provider_snapshot(redis.values[key]) == _snapshot()


def test_redis_read_failure_past_the_deadline_calls_no_provider() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )

    def slow_failing_get(read_key: str):
        del read_key
        clock.advance(2)
        raise ConnectionError("redis unavailable")

    redis.get = slow_failing_get  # type: ignore[method-assign]
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="slow-failing-read",
    )

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), context)

    assert provider.calls == []


def test_follower_adopts_the_owner_failure_instead_of_its_own_stale_copy() -> None:
    """Followers take the owner's decision; they never race on their own read."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    started = Event()
    release = Event()
    original_get_snapshot = provider.get_snapshot

    def blocking_failure(query, context):
        started.set()
        release.wait(timeout=2)
        return original_get_snapshot(query, context)

    provider.get_snapshot = blocking_failure  # type: ignore[method-assign]
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    context = _context()
    errors: list[BaseException] = []
    results: list[ProviderSnapshot] = []

    def retrieve() -> None:
        try:
            results.append(cache.get_snapshot(NBAMarketQuery(), context))
        except BaseException as error:
            errors.append(error)

    owner = Thread(target=retrieve)
    owner.start()
    assert started.wait(timeout=1)
    # The owner read an empty key;  a stale value appears only for the follower.
    stale_payload = serialize_provider_snapshot(_snapshot())
    redis.values[key] = stale_payload
    clock.advance(600)
    joined = Event()
    original_submit = cache.coordinator.submit

    def observed_submit(flight_key, function):
        flight, is_owner = original_submit(flight_key, function)
        if not is_owner:
            joined.set()
        return flight, is_owner

    cache.coordinator.submit = observed_submit  # type: ignore[method-assign]
    follower = Thread(target=retrieve)
    follower.start()
    assert joined.wait(timeout=1)
    release.set()
    owner.join(timeout=2)
    follower.join(timeout=2)

    assert results == []
    assert len(errors) == 2
    assert all(isinstance(error, TimeoutError) for error in errors)
    assert redis.values[key] == stale_payload


def test_unusable_cached_payload_is_only_deleted_while_unchanged() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    newer = serialize_provider_snapshot(_snapshot())
    original_get = redis.get

    def read_then_race(read_key: str):
        value = original_get(read_key)
        redis.values[read_key] = newer
        return value

    redis.get = read_then_race  # type: ignore[method-assign]
    redis.values[key] = '{"schema":"not-statsplus"}'

    cache.get_snapshot(NBAMarketQuery(), _context())

    assert redis.deleted == []
    # The racing writer publishes a usable value before the refresh reads again
    # inside its flight, so that value is adopted and the provider is spared.
    # The unusable payload is gone, replaced by the racing write rather than by
    # a delete, which is what leaves `deleted` empty.
    assert provider.calls == []


@pytest.mark.parametrize("late_value", [_snapshot(), _snapshot("underdog"), object()])
def test_late_result_only_retires_an_abandoned_flight(late_value) -> None:
    """Whatever the late result is, it decides nothing for anyone."""

    redis = FakeRedis()
    clock = ControlledClock()
    executor = ManualExecutor(cancel_result=False)
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        executor=executor,
    )
    owner_error: dict[str, BaseException] = {}
    owner = Thread(
        target=lambda: _capture_error(
            owner_error,
            lambda: cache.get_snapshot(
                NBAMarketQuery(),
                RetrievalContext(
                    deadline=_RETRIEVED_AT + timedelta(seconds=1),
                    request_id="owner-timeout",
                ),
            ),
        )
    )
    owner.start()
    clock.advance(2)
    owner.join(timeout=2)
    assert isinstance(owner_error["error"], TimeoutError)

    follower_result = _run_follower(cache)
    executor.future.complete(late_value)

    assert follower_result["error"] is owner_error["error"]
    assert cache.coordinator.pending_count() == 0
    assert redis.set_calls == []
    assert redis.values == {}


def test_follower_deadline_never_abandons_the_owner_flight() -> None:
    """A follower gives up on its own budget without disturbing the refresh."""

    redis = FakeRedis()
    clock = ControlledClock()
    executor = ManualExecutor(cancel_result=False)
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        executor=executor,
    )
    owner_result: dict[str, object] = {}
    owner = Thread(
        target=lambda: _capture_value(
            owner_result,
            lambda: cache.get_snapshot(
                NBAMarketQuery(),
                RetrievalContext(
                    deadline=_RETRIEVED_AT + timedelta(seconds=100),
                    request_id="owner",
                ),
            ),
        ),
        daemon=True,
    )
    joined = Event()
    original_submit = cache.coordinator.submit

    def observed_submit(key, function):
        flight, is_owner = original_submit(key, function)
        if not is_owner:
            joined.set()
        return flight, is_owner

    owner.start()
    cache.coordinator.submit = observed_submit  # type: ignore[method-assign]
    follower_result: dict[str, object] = {}
    follower = Thread(
        target=lambda: _capture_value(
            follower_result,
            lambda: cache.get_snapshot(
                NBAMarketQuery(),
                RetrievalContext(
                    deadline=_RETRIEVED_AT + timedelta(seconds=3),
                    request_id="follower",
                ),
            ),
        ),
        daemon=True,
    )
    follower.start()
    assert joined.wait(timeout=2)
    clock.advance(4)
    follower.join(timeout=2)

    assert isinstance(follower_result["error"], TimeoutError)
    assert not executor.future.cancelled
    assert cache.coordinator.pending_count() == 1

    executor.future.complete(_snapshot())
    owner.join(timeout=2)

    assert owner_result["value"] == _snapshot()
    assert cache.coordinator.pending_count() == 0
    assert len(redis.set_calls) == 1


def test_detached_disabled_flight_records_no_late_hit_provenance() -> None:
    """A disabled cache cannot report a late refresh as a cache miss."""

    telemetry.clear_recorded_provider_events()
    try:
        clock = ControlledClock()
        executor = ManualExecutor(cancel_result=False)
        cache = ProviderSnapshotCache(
            FakeProvider(_snapshot()),
            provider_name="dabble",
            redis_client=None,
            enabled=False,
            clock=clock.now,
            executor=executor,
        )
        owner_error: dict[str, BaseException] = {}
        owner = Thread(
            target=lambda: _capture_error(
                owner_error,
                lambda: cache.get_snapshot(
                    NBAMarketQuery(),
                    RetrievalContext(
                        deadline=_RETRIEVED_AT + timedelta(seconds=1),
                        request_id="owner-timeout",
                    ),
                ),
            )
        )
        owner.start()
        clock.advance(2)
        owner.join(timeout=2)
        assert isinstance(owner_error["error"], TimeoutError)

        follower_result = _run_follower(cache)
        executor.future.complete(_snapshot())

        assert follower_result["error"] is owner_error["error"]
        assert cache.coordinator.pending_count() == 0
        # Two requests, two decisions, and neither invents a cache hit or miss.
        assert telemetry.snapshot_metrics()["cache"]["dabble"] == {"disabled": 2}
    finally:
        telemetry.clear_recorded_provider_events()


def test_abandoned_flight_retires_when_a_late_result_cannot_be_observed() -> None:
    class UnobservableFuture:
        def done(self) -> bool:
            return False

        def result(self, timeout: float | None = None) -> ProviderSnapshot:
            del timeout
            raise TimeoutError()

        def cancel(self) -> bool:
            return False

    class UnobservableExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def submit(self, function):
            self.calls += 1
            del function
            return UnobservableFuture()

    clock = ControlledClock()
    executor = UnobservableExecutor()
    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=FakeRedis(),
        clock=clock.now,
        executor=executor,
    )
    error: dict[str, BaseException] = {}
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=1),
        request_id="unobservable",
    )
    thread = Thread(
        target=lambda: _capture_error(
            error,
            lambda: cache.get_snapshot(NBAMarketQuery(), context),
        )
    )
    thread.start()
    clock.advance(2)
    thread.join(timeout=2)

    assert isinstance(error["error"], TimeoutError)
    assert cache.coordinator.pending_count() == 0


def test_each_snapshot_request_records_one_cache_decision_and_no_provider_event() -> None:
    telemetry.clear_recorded_provider_events()
    try:
        redis = FakeRedis()
        clock = ControlledClock()
        provider = FakeProvider(_snapshot())
        cache = ProviderSnapshotCache(
            provider,
            provider_name="dabble",
            redis_client=redis,
            clock=clock.now,
        )
        disabled = ProviderSnapshotCache(
            provider,
            provider_name="dabble",
            redis_client=None,
            enabled=False,
            clock=clock.now,
        )

        cache.get_snapshot(NBAMarketQuery(), _context())  # miss
        cache.get_snapshot(NBAMarketQuery(), _context())  # hit
        disabled.get_snapshot(NBAMarketQuery(), _context())  # disabled
        clock.advance(600)
        provider.error = TimeoutError("upstream unavailable")
        cache.get_snapshot(NBAMarketQuery(), _context())  # stale

        assert telemetry.get_recorded_provider_events() == []
        statuses = telemetry.snapshot_metrics()["cache"]["dabble"]
        assert statuses == {"miss": 1, "hit": 1, "disabled": 1, "stale": 1}
    finally:
        telemetry.clear_recorded_provider_events()


def test_nested_invalid_cached_field_is_a_miss_and_the_key_is_replaced() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    corrupt = json.loads(serialize_provider_snapshot(_market_payload_snapshot()))
    corrupt["markets"][0]["athlete"]["team"]["name"] = 123
    redis.values[key] = json.dumps(corrupt)

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert redis.deleted == [key]
    assert cache.last_result.cache_status == "miss"
    assert redis.values[key] == redis.set_calls[0][1]


def test_unrepresentable_cached_number_is_a_miss_and_never_reaches_the_board() -> None:
    """An OverflowError from a wire number must not escape the cache seam."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(_market_payload_snapshot()).replace(
        '"american_price":null', '"american_price":1e309', 1
    )

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert redis.deleted == [key]
    assert cache.last_result.cache_status == "miss"
    assert redis.values[key] == redis.set_calls[0][1]


@pytest.mark.parametrize("field", _MARKET_LABEL_FIELDS)
@pytest.mark.parametrize("value", [*_NONSTANDARD_CONSTANTS, 5])
def test_nonstring_cached_market_label_is_a_miss_and_the_key_is_replaced(
    field, value
) -> None:
    """A corrupt label must be deleted and refetched, not served to the board."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = _payload_with_market_label(field, value)

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert redis.deleted == [key]
    assert cache.last_result.cache_status == "miss"
    assert redis.values[key] == redis.set_calls[0][1]


def test_deeply_nested_cached_payload_is_a_miss_and_never_reaches_the_board() -> None:
    """A RecursionError from the parser must not escape the cache seam."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = _deeply_nested_payload()

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert redis.deleted == [key]
    assert cache.last_result.cache_status == "miss"
    assert redis.values[key] == redis.set_calls[0][1]


def test_provider_events_never_increment_cache_decision_counters() -> None:
    """One wrapper request is one cache decision, whatever the provider does."""

    telemetry.clear_recorded_provider_events()
    try:
        redis = FakeRedis()
        clock = ControlledClock()
        provider = FakeProvider(_snapshot())
        original_get_snapshot = provider.get_snapshot

        def multi_event_retrieval(query, context):
            # Dabble fans out over fixture details;  each real upstream call
            # records its own provider event with a cache status.
            for index in range(3):
                with telemetry.ProviderTracker("dabble", f"fixture_detail_{index}"):
                    pass
            return original_get_snapshot(query, context)

        provider.get_snapshot = multi_event_retrieval  # type: ignore[method-assign]
        cache = ProviderSnapshotCache(
            provider,
            provider_name="dabble",
            redis_client=redis,
            clock=clock.now,
        )

        cache.get_snapshot(NBAMarketQuery(), _context())

        events = telemetry.get_recorded_provider_events()
        assert len(events) == 3
        assert {event["cache_status"] for event in events} == {"miss"}
        assert telemetry.snapshot_metrics()["cache"]["dabble"] == {"miss": 1}
    finally:
        telemetry.clear_recorded_provider_events()


def test_cache_surface_keeps_no_undocumented_aliases() -> None:
    import app.services.dfs_snapshot_cache as module
    from app.services.dfs_board import ProviderOutcome

    for name in ("decorate_provider", "get_snapshot_with_cache_info", "cache_info", "wrap"):
        assert not hasattr(module, name)
        assert not hasattr(ProviderSnapshotCache, name)
        assert not hasattr(module.ProviderSnapshotCacheCoordinator, name)
    for name in ("age", "failure_reason", "failure_at"):
        assert not hasattr(SnapshotCacheResult, name)
    for name in ("cache_age", "cache_retrieved"):
        assert not hasattr(ProviderOutcome, name)


def _capture_error(target: dict[str, BaseException], function) -> None:
    try:
        function()
    except BaseException as error:
        target["error"] = error


def test_injected_cache_decorator_is_used_by_the_internal_board() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    service = DFSBoardService(
        provider_registry={"dabble": provider},
        snapshot_cache=lambda name, value: cache if name == "dabble" else value,
        clock=clock.now,
        monotonic=lambda: 0.0,
    )
    context = RetrievalContext(deadline=_RETRIEVED_AT + timedelta(seconds=10))

    first = service.get_board(NBAMarketQuery(), context)
    second = service.get_board(NBAMarketQuery(), context)

    assert first.provider_outcomes[0].cache_status == "miss"
    assert second.provider_outcomes[0].cache_status == "hit"
    assert second.provider_outcomes[0].cache_age_seconds == 0
    assert len(provider.calls) == 1


def test_disabled_cache_fails_open_without_an_in_process_stale_store() -> None:
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=None,
        enabled=False,
        clock=ControlledClock().now,
    )

    first = cache.get_snapshot(NBAMarketQuery(), _context())
    second = cache.get_snapshot(NBAMarketQuery(), _context())

    assert first is second is provider.snapshot
    assert len(provider.calls) == 2
    assert cache.last_result.cache_status == "disabled"


def test_redis_read_failure_does_not_turn_a_direct_failure_into_stale_fallback() -> None:
    redis = FakeRedis()
    redis.get_error = ConnectionError("redis unavailable")
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=ControlledClock().now,
    )

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), _context())

    assert len(provider.calls) == 1
    assert redis.set_calls == []


def test_redis_write_failure_returns_direct_snapshot_without_local_reuse() -> None:
    redis = FakeRedis()
    redis.set_error = ConnectionError("redis unavailable")
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=ControlledClock().now,
    )

    cache.get_snapshot(NBAMarketQuery(), _context())
    cache.get_snapshot(NBAMarketQuery(), _context())

    assert len(provider.calls) == 2
    assert cache.last_result.cache_status == "error"


def test_cache_windows_have_explicit_fresh_and_stale_boundaries() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        fresh_seconds=300,
        stale_if_error_seconds=1800,
    )
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(
        _snapshot()
    )

    clock.advance(300)
    cache.get_snapshot(NBAMarketQuery(), _context())
    assert cache.last_result.cache_status == "stale"

    clock.advance(1501)
    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), _context())
    assert len(provider.calls) == 2


def test_stale_window_is_rechecked_when_refresh_failure_arrives_late() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())

    def late_failure(query, context):
        del query, context
        clock.advance(10)
        raise TimeoutError("upstream unavailable")

    provider.get_snapshot = late_failure  # type: ignore[method-assign]
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        stale_if_error_seconds=1800,
    )
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(_snapshot())
    clock.advance(1795)

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), _context())

    assert cache.get_last_result() is None


def test_cache_telemetry_is_bounded_by_provider_and_status() -> None:
    telemetry.clear_recorded_provider_events()
    try:
        redis = FakeRedis()
        clock = ControlledClock()
        provider = FakeProvider(_snapshot())
        cache = ProviderSnapshotCache(
            provider,
            provider_name="dabble",
            redis_client=redis,
            clock=clock.now,
        )
        cache.get_snapshot(NBAMarketQuery(), _context())
        clock.advance(1)
        cache.get_snapshot(NBAMarketQuery(), _context())

        statuses = telemetry.snapshot_metrics()["cache"]["dabble"]
        assert statuses["miss"] == 1
        assert statuses["hit"] == 1
        assert set(statuses) <= {"hit", "miss", "disabled", "stale", "error"}
    finally:
        telemetry.clear_recorded_provider_events()


def test_oversized_cached_integer_is_a_miss_and_the_key_is_replaced() -> None:
    """A digit-limit ValueError from the parser must not escape the seam."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(_market_payload_snapshot()).replace(
        '"american_price":null', f'"american_price":{_OVERSIZED_INTEGER}', 1
    )

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert redis.deleted == [key]
    assert cache.last_result.cache_status == "miss"
    assert redis.values[key] == redis.set_calls[0][1]


def test_repeated_query_statuses_canonicalize_to_one_key_and_payload() -> None:
    """A repeated status names the same semantic query, not a second one."""

    canonical = NBAMarketQuery()
    repeated = NBAMarketQuery(market_statuses=("available", "available", "suspended"))

    assert repeated.market_statuses == (MarketStatus.AVAILABLE, MarketStatus.SUSPENDED)
    assert repeated == canonical

    cache = ProviderSnapshotCache(
        FakeProvider(_snapshot()),
        provider_name="dabble",
        redis_client=FakeRedis(),
        clock=ControlledClock().now,
    )
    assert cache.cache_key(repeated) == cache.cache_key(canonical)
    assert serialize_provider_snapshot(
        _snapshot(), repeated
    ) == serialize_provider_snapshot(_snapshot(), canonical)


def test_repeated_query_statuses_share_one_single_flight_refresh() -> None:
    redis = FakeRedis()
    provider = FakeProvider(_snapshot())
    clock = ControlledClock()
    started = Event()
    release = Event()

    original_get_snapshot = provider.get_snapshot

    def blocking_get_snapshot(query, context):
        started.set()
        # Outlasts the wait for the follower to join, so the owner is released
        # by the test rather than by its own timeout.
        release.wait(timeout=5)
        return original_get_snapshot(query, context)

    provider.get_snapshot = blocking_get_snapshot  # type: ignore[method-assign]
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    results: list[ProviderSnapshot] = []
    errors: list[BaseException] = []
    joined = _signal_when_a_follower_joins(cache)

    def retrieve(query: NBAMarketQuery) -> None:
        try:
            results.append(cache.get_snapshot(query, _context()))
        except BaseException as error:  # pragma: no cover - diagnostic assertion
            errors.append(error)

    first = Thread(target=retrieve, args=(NBAMarketQuery(),))
    second = Thread(
        target=retrieve,
        args=(NBAMarketQuery(market_statuses=("available", "available", "suspended")),),
    )
    first.start()
    assert started.wait(timeout=1)
    second.start()
    assert joined.wait(timeout=2)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert results == [provider.snapshot, provider.snapshot]
    assert len(provider.calls) == 1
    assert len(redis.set_calls) == 1


def _cache_stalled_between_read_and_flight(
    redis: FakeRedis, missed: Event, resume: Event
) -> None:
    """Hold the follower between its cache read and its flight registration.

    That gap is the whole window: a caller that misses while an owner is in
    flight can still register after the owner retires. Blocking the read
    reproduces it exactly rather than hoping the scheduler supplies it.

    ``missed`` reports that the read happened and returned nothing. Without it
    a late follower would read after the owner published, take an ordinary
    hit, and satisfy every assertion without ever entering the window.

    Only the follower's own read is held. The re-read the flight performs runs
    on a cache worker thread, so keying on the caller keeps the two apart.
    """

    original_get = redis.get
    held = {"already": False}

    def staged_get(key: str):
        value = original_get(key)
        if current_thread().name == "follower" and not held["already"]:
            held["already"] = True
            if value is None:
                missed.set()
            resume.wait(timeout=5)
        return value

    redis.get = staged_get  # type: ignore[method-assign]


def _blocking_until(provider: FakeProvider, entered: Event, release: Event) -> None:
    """Hold the owner inside the provider so a follower can read past it."""

    original_get_snapshot = provider.get_snapshot

    def blocking_get_snapshot(query, context):
        entered.set()
        release.wait(timeout=5)
        return original_get_snapshot(query, context)

    provider.get_snapshot = blocking_get_snapshot  # type: ignore[method-assign]


def test_a_late_owner_adopts_the_value_the_previous_owner_published() -> None:
    """The second owner must not repeat a refresh that already published."""

    redis = FakeRedis()
    provider = FakeProvider(_snapshot())
    owner_retired = Event()
    follower_missed = Event()
    in_provider = Event()
    owner_may_finish = Event()
    _cache_stalled_between_read_and_flight(redis, follower_missed, owner_retired)
    _blocking_until(provider, in_provider, owner_may_finish)

    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=ControlledClock().now,
    )
    results: list[ProviderSnapshot] = []
    errors: list[BaseException] = []
    # last_result is per thread, so each caller records its own provenance
    # where that provenance actually exists.
    served: dict[str, SnapshotCacheResult] = {}

    def retrieve() -> None:
        try:
            results.append(cache.get_snapshot(NBAMarketQuery(), _context()))
            served[current_thread().name] = cache.last_result
        except BaseException as error:  # pragma: no cover - diagnostic assertion
            errors.append(error)

    owner = Thread(target=retrieve, name="owner")
    follower = Thread(target=retrieve, name="follower")
    owner.start()
    assert in_provider.wait(timeout=5)
    # The follower reads while the owner is still in flight, so it misses, and
    # is then held short of registering a flight of its own.
    follower.start()
    # The follower has read and found nothing;  only now may the owner publish.
    assert follower_missed.wait(timeout=5)
    owner_may_finish.set()
    owner.join(timeout=5)
    # The owner has published and retired;  only now may the follower register.
    owner_retired.set()
    follower.join(timeout=5)

    assert errors == []
    assert results == [provider.snapshot, provider.snapshot]
    assert len(provider.calls) == 1
    # Adoption reports the hit it served and leaves the published value and its
    # expiry exactly where the publishing refresh put them.
    assert len(redis.set_calls) == 1
    assert redis.set_calls[0][2] == 1800
    assert served["follower"].cache_status == "hit"
    assert served["follower"].age_seconds == 0


def test_an_adopting_owner_is_still_bound_by_its_own_deadline() -> None:
    """Adoption is not an escape from the budget the caller arrived with."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    published = serialize_provider_snapshot(_snapshot())
    original_get = redis.get
    reads = {"count": 0}

    def publish_then_expire(read_key: str):
        reads["count"] += 1
        value = original_get(read_key)
        if reads["count"] == 1:
            # The caller misses, and a value lands before its flight looks.
            redis.values[read_key] = published
        elif reads["count"] == 2:
            # The flight's own read outlives the budget. The value it finds is
            # still fresh, so this is the adopting path rather than a refresh.
            clock.advance(1)
        return value

    redis.get = publish_then_expire  # type: ignore[method-assign]
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=0.5),
        request_id="cache-test",
    )

    with pytest.raises(DeadlineExceededError):
        cache.get_snapshot(NBAMarketQuery(), context)
    assert provider.calls == []
    assert redis.set_calls == []


def test_a_slow_second_read_calls_no_provider_past_the_deadline() -> None:
    """The read inside the flight bounds the provider exactly as the first does."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    original_get = redis.get
    reads = {"count": 0}

    def slow_second_read(read_key: str):
        reads["count"] += 1
        value = original_get(read_key)
        if reads["count"] == 2:
            # Nothing was published, so there is nothing to adopt;  the budget
            # is gone regardless and the provider must never be reached.
            clock.advance(1)
        return value

    redis.get = slow_second_read  # type: ignore[method-assign]
    context = RetrievalContext(
        deadline=_RETRIEVED_AT + timedelta(seconds=0.5),
        request_id="cache-test",
    )

    with pytest.raises(DeadlineExceededError):
        cache.get_snapshot(NBAMarketQuery(), context)
    assert provider.calls == []


def test_a_late_owner_still_refreshes_when_the_first_published_nothing() -> None:
    """A partial refresh is never written, so there is nothing to adopt."""

    partial = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.PARTIAL,
        markets=(PlayerProjectionMarket(provider="dabble"),),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            skipped_count=0,
            pagination_complete=False,
        ),
        retrieved_at=_RETRIEVED_AT,
    )
    redis = FakeRedis()
    provider = FakeProvider(partial)
    owner_retired = Event()
    follower_missed = Event()
    in_provider = Event()
    owner_may_finish = Event()
    _cache_stalled_between_read_and_flight(redis, follower_missed, owner_retired)
    _blocking_until(provider, in_provider, owner_may_finish)

    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=ControlledClock().now,
    )
    results: list[ProviderSnapshot] = []

    def retrieve() -> None:
        results.append(cache.get_snapshot(NBAMarketQuery(), _context()))

    owner = Thread(target=retrieve, name="owner")
    follower = Thread(target=retrieve, name="follower")
    owner.start()
    assert in_provider.wait(timeout=5)
    follower.start()
    # The follower has read and found nothing;  only now may the owner publish.
    assert follower_missed.wait(timeout=5)
    owner_may_finish.set()
    owner.join(timeout=5)
    owner_retired.set()
    follower.join(timeout=5)

    assert results == [partial, partial]
    assert redis.set_calls == []
    # Documented rather than desired:  adoption reads the published value, and
    # a partial refresh publishes none, so this caller repeats the work.
    assert len(provider.calls) == 2


def test_future_dated_cached_snapshot_is_a_miss_and_the_key_is_replaced() -> None:
    """A value observed after the current instant cannot be aged, so it is unusable."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )
    key = cache.cache_key(NBAMarketQuery())
    redis.values[key] = serialize_provider_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
            retrieved_at=_RETRIEVED_AT + timedelta(seconds=1),
        )
    )

    result = cache.get_snapshot(NBAMarketQuery(), _context())

    assert result is provider.snapshot
    assert redis.deleted == [key]
    assert cache.last_result.cache_status == "miss"
    assert cache.last_result.age_seconds == 0
    assert redis.values[key] == redis.set_calls[0][1]


def test_future_dated_refresh_is_never_published_or_served() -> None:
    """A provider snapshot dated past the clock breaks the temporal contract."""

    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
            retrieved_at=_RETRIEVED_AT + timedelta(seconds=1),
        )
    )
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
    )

    with pytest.raises(ValueError):
        cache.get_snapshot(NBAMarketQuery(), _context())

    assert redis.values == {}
    assert redis.set_calls == []
    assert cache.get_last_result() is None
    assert cache.coordinator.pending_count() == 0


# -- one freshness boundary, shared with the comparison board ----------------


def _boundary_cache(redis, clock, *, provider, fresh=300, stale=1800):
    return ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        fresh_seconds=fresh,
        stale_if_error_seconds=stale,
    )


@pytest.mark.parametrize(
    ("elapsed", "expected_status"),
    [
        (299.999999, "hit"),
        (300, "miss"),
        (300.000001, "miss"),
    ],
)
def test_the_fresh_window_endpoint_is_outside_the_window(
    elapsed, expected_status
) -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot())
    cache = _boundary_cache(redis, clock, provider=provider)
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(
        _snapshot()
    )
    clock.advance(elapsed)

    cache.get_snapshot(NBAMarketQuery(), _context())
    result = cache.last_result

    assert result.cache_status == expected_status
    assert isinstance(result.age_seconds, Decimal)
    assert result.age_seconds == Decimal(str(elapsed))
    assert len(provider.calls) == (0 if expected_status == "hit" else 1)


@pytest.mark.parametrize("elapsed", [1799.999999, 1800])
def test_the_stale_ceiling_endpoint_is_still_a_permitted_fallback(elapsed) -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    cache = _boundary_cache(redis, clock, provider=provider)
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(
        _snapshot()
    )
    clock.advance(elapsed)

    cache.get_snapshot(NBAMarketQuery(), _context())
    result = cache.last_result

    assert result.cache_status == "stale"
    assert result.age_seconds == Decimal(str(elapsed))
    assert result.refresh_failure_reason == "timeout"
    assert result.refresh_failed_at == clock.now_value


def test_one_microsecond_past_the_stale_ceiling_is_no_longer_served() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    cache = _boundary_cache(redis, clock, provider=provider)
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(
        _snapshot()
    )
    clock.advance(1800.000001)

    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), _context())
    assert cache.get_last_result() is None


@pytest.mark.parametrize(
    "windows",
    [
        {"fresh_seconds": 1e129},
        {"fresh_seconds": 1e-200},
        {"stale_if_error_seconds": 1e129},
        {"stale_if_error_seconds": 1e-200},
        {"fresh_seconds": float("inf")},
        {"fresh_seconds": True},
        {"fresh_seconds": 0},
        {"fresh_seconds": -1},
    ],
)
def test_cache_windows_outside_the_time_window_domain_are_refused(windows) -> None:
    with pytest.raises(ValueError):
        ProviderSnapshotCache(
            FakeProvider(_snapshot()),
            provider_name="dabble",
            redis_client=FakeRedis(),
            **windows,
        )


# -- one cache-window policy, wherever a runtime cache is built --------------


def test_a_direct_fresh_window_can_never_outlast_its_stale_ceiling() -> None:
    with pytest.raises(ValueError) as error:
        ProviderSnapshotCache(
            FakeProvider(_snapshot()),
            provider_name="dabble",
            redis_client=FakeRedis(),
            fresh_seconds=300,
            stale_if_error_seconds="299.999999",
        )

    assert "fresh_seconds" in str(error.value)
    assert "stale_if_error_seconds" in str(error.value)


def test_an_injected_coordinator_cannot_decorate_an_inverted_policy() -> None:
    coordinator = ProviderSnapshotCacheCoordinator(
        redis_client=FakeRedis(),
        fresh_seconds=1800,
        stale_if_error_seconds=1800,
    )

    with pytest.raises(ValueError):
        coordinator.decorate(
            "dabble", FakeProvider(_snapshot()), stale_if_error_seconds=300
        )
    coordinator.shutdown()


def test_a_coordinator_refuses_an_inverted_policy_when_it_is_built() -> None:
    with pytest.raises(ValueError):
        ProviderSnapshotCacheCoordinator(
            fresh_seconds=600, stale_if_error_seconds=300
        )


def test_a_fresh_window_equal_to_its_stale_ceiling_is_accepted() -> None:
    redis = FakeRedis()
    clock = ControlledClock()
    provider = FakeProvider(_snapshot(), error=TimeoutError("upstream unavailable"))
    cache = ProviderSnapshotCache(
        provider,
        provider_name="dabble",
        redis_client=redis,
        clock=clock.now,
        fresh_seconds=300,
        stale_if_error_seconds=300,
    )
    redis.values[cache.cache_key(NBAMarketQuery())] = serialize_provider_snapshot(
        _snapshot()
    )

    clock.advance(300)
    cache.get_snapshot(NBAMarketQuery(), _context())
    assert cache.last_result.cache_status == "stale"

    clock.advance(0.000001)
    with pytest.raises(TimeoutError):
        cache.get_snapshot(NBAMarketQuery(), _context())


def test_the_canonical_price_triple_survives_the_document_round_trip() -> None:
    priced = replace(
        _market_payload_snapshot(),
        markets=(
            replace(
                _market_payload_snapshot().markets[0],
                selections=(
                    Selection(
                        selection_id="selection-1",
                        direction="higher",
                        american_price=-112,
                        decimal_price="1.900",
                    ),
                ),
            ),
        ),
    )

    document = serialize_provider_snapshot(priced, NBAMarketQuery())
    payload = json.loads(document)
    decoded = deserialize_provider_snapshot(document, expected_query=NBAMarketQuery())
    selection = decoded.markets[0].selections[0]

    assert payload["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert payload["markets"][0]["selections"][0]["price_kind"] == "american"
    assert payload["markets"][0]["selections"][0]["price_value"] == "-112"
    assert payload["markets"][0]["selections"][0]["price_scope"] == "selection"
    assert (selection.price_kind, selection.price_value, selection.price_scope) == (
        PriceKind.AMERICAN,
        Decimal("-112"),
        PriceScope.SELECTION,
    )
    assert serialize_provider_snapshot(decoded, NBAMarketQuery()) == document


def test_a_document_is_read_and_re_encoded_in_the_version_it_declares() -> None:
    snapshot = replace(
        _market_payload_snapshot(),
        markets=(
            replace(
                _market_payload_snapshot().markets[0],
                selections=(
                    Selection(
                        selection_id="selection-1",
                        direction="higher",
                        modifiers=(
                            SelectionModifier("1.5", "multiplier", "selection"),
                        ),
                    ),
                ),
            ),
        ),
    )

    archived = serialize_provider_snapshot(snapshot, NBAMarketQuery(), schema_version=1)
    payload = json.loads(archived)

    assert payload["schema_version"] == 1
    assert "price_kind" not in payload["markets"][0]["selections"][0]

    # An archived document keeps its exact bytes, so its checksum keeps its
    # meaning, while the triple is still available to every reader.
    decoded = deserialize_provider_snapshot(archived, expected_query=NBAMarketQuery())
    selection = decoded.markets[0].selections[0]

    assert (selection.price_kind, selection.price_value, selection.price_scope) == (
        PriceKind.MULTIPLIER,
        Decimal("1.5"),
        PriceScope.ENTRY,
    )
    assert (
        serialize_provider_snapshot(decoded, NBAMarketQuery(), schema_version=1)
        == archived
    )
    with pytest.raises(ValueError, match="schema version is unsupported"):
        serialize_provider_snapshot(decoded, NBAMarketQuery(), schema_version=3)
    with pytest.raises(SnapshotCacheError, match="schema version is incompatible"):
        deserialize_provider_snapshot(
            _canonical({**payload, "schema_version": 3}),
            expected_query=NBAMarketQuery(),
        )


def _legacy_v1_document(*, provider: str, modifier_kind: str, mixed: bool) -> str:
    """A schema-version-1 document as pre-#109 code could legally archive it.

    v1 selections carry no price triple, and the old contract accepted any
    non-empty modifier kind (Underdog read a provider ``modifier_kind`` field
    verbatim) and never rejected a market whose sides priced in different
    forms.  These documents are already in production archives.
    """

    selections = [
        {
            "selection_id": "higher",
            "label": None,
            "direction": "higher",
            "direction_label": None,
            "status": None,
            "modifiers": [
                {"value": "1.5", "kind": modifier_kind, "scope": "selection", "label": None}
            ],
            "american_price": "-112",
            "decimal_price": None,
        }
    ]
    if mixed:
        # A second side priced in a different form -- forbidden for new
        # normalization, legal in a v1 archive.
        selections.append(
            {
                "selection_id": "lower",
                "label": None,
                "direction": "lower",
                "direction_label": None,
                "status": None,
                "modifiers": [
                    {"value": "2.0", "kind": "multiplier", "scope": "selection", "label": None}
                ],
                "american_price": None,
                "decimal_price": None,
            }
        )
    document = {
        "schema": "statsplus.provider_snapshot",
        "schema_version": 1,
        "contract_version": "1",
        "provider": provider,
        "status": "complete",
        "coverage": {
            "fetched_count": 1,
            "eligible_count": 1,
            "normalized_count": 1,
            "skipped_count": 0,
            "pagination_complete": True,
            "fanout_complete": True,
            "expected_total": None,
            "warning_codes": [],
            "skipped_reasons": [],
            "diagnostic_details": [],
        },
        "retrieved_at": "2026-08-09T20:00:00+00:00",
        "query": {
            "sport": "NBA",
            "league": "NBA",
            "market_statuses": ["available", "suspended"],
            "pregame_only": True,
        },
        "markets": [
            {
                "provider": provider,
                "market_id": "line-1",
                "athlete": None,
                "event": None,
                "team": None,
                "opponent": None,
                "league": None,
                "competition": None,
                "sport": None,
                "statistic": None,
                "threshold": None,
                "status": "available",
                "status_label": None,
                "variant": "standard",
                "variant_label": None,
                "scoring_period": "full_game",
                "scoring_period_label": None,
                "starts_at": None,
                "updated_at": None,
                "appearance": None,
                "statistic_match": None,
                "selections": selections,
            }
        ],
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def test_a_v1_document_with_a_legacy_modifier_kind_still_decodes() -> None:
    payload = _legacy_v1_document(
        provider="underdog", modifier_kind="boost", mixed=False
    )

    snapshot = deserialize_provider_snapshot(payload, expected_query=NBAMarketQuery())

    # The unreviewed kind is preserved verbatim, not remapped or rejected.
    modifier = snapshot.markets[0].selections[0].modifiers[0]
    assert modifier.kind == "boost"


def test_a_v1_document_priced_in_mixed_forms_still_decodes() -> None:
    payload = _legacy_v1_document(
        provider="underdog", modifier_kind="multiplier", mixed=True
    )

    snapshot = deserialize_provider_snapshot(payload, expected_query=NBAMarketQuery())

    market = snapshot.markets[0]
    # A known alias still normalizes, so the priced side is comparable.
    assert market.selections[0].price_kind is PriceKind.AMERICAN
    assert len(market.selections) == 2


def test_a_v2_document_still_enforces_the_closed_vocabularies() -> None:
    # The same shape at schema version 2 is new-format data and stays strict.
    payload = _legacy_v1_document(
        provider="underdog", modifier_kind="boost", mixed=False
    ).replace('"schema_version":1', '"schema_version":2')

    with pytest.raises(SnapshotCacheError):
        deserialize_provider_snapshot(payload, expected_query=NBAMarketQuery())
