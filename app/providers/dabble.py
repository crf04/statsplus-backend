"""Dabble adapter for the shared NBA Provider Snapshot contract.

Dabble has no supported public developer API.  The adapter keeps its current
read-only mobile feed behind the semantic :class:`NBAMarketQuery` seam.  Feed
discovery and fixture-detail fan-out are deliberately private so callers see
one coherent, typed snapshot rather than Dabble's competition and fixture
resources.
"""

from __future__ import annotations

import concurrent.futures
import copy
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, NoReturn
from urllib.parse import quote

import requests
from urllib3.util.retry import Retry

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    CompetitionEvidence,
    CoverageCode,
    CoverageRecordExcluded,
    CoverageRecordMalformed,
    DeadlineExceededError,
    EventEvidence,
    LeagueEvidence,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    ScoringPeriod,
    Selection,
    SelectionModifier,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
    _NormalizedBatch,
    normalize_market_status,
    _RecordCoverageAccumulator,
    _build_snapshot,
)
from app.providers.dfs_normalization import optional_text
from app.providers.dfs_transport import (
    TransportErrorPolicy,
    request_json,
    run_bounded,
)
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_DABBLE,
    ProviderResponseError,
    increment_retry_count,
    provider_normalization_call,
)

logger = logging.getLogger(__name__)

_STAT_ORDER = {
    "points": 0,
    "rebounds": 1,
    "assists": 2,
    "three-pointers-made": 3,
    "steals": 4,
    "blocks": 5,
    "turnovers": 6,
}
_BASKETBALL_LABELS = {"basketball", "nba"}
_NON_PLAYER_KINDS = {
    "team",
    "teams",
    "match",
    "matches",
    "game",
    "games",
    "future",
    "futures",
    "entry",
    "entry-placement",
    "entry_placement",
}


def canonical_stat_components(stats: Sequence[str]) -> list[str]:
    """Normalize components into a stable provider-independent order."""

    normalized = [stat.strip().casefold().replace("_", "-") for stat in stats]

    def sort_key(stat: str) -> tuple[str, int, str]:
        prefix = ""
        base = stat
        for period in ("first-half-", "second-half-", "first-quarter-", "second-quarter-"):
            if stat.startswith(period):
                prefix = period
                base = stat.removeprefix(period)
                break
        return prefix, _STAT_ORDER.get(base, len(_STAT_ORDER)), base

    return sorted(normalized, key=sort_key)


class _DabbleRetry(Retry):
    """Count safe HTTP retries in the provider telemetry event."""

    def increment(
        self,
        method=None,
        url=None,
        response=None,
        error=None,
        _pool=None,
        _stacktrace=None,
    ):
        next_retry = super().increment(
            method, url, response, error, _pool, _stacktrace
        )
        increment_retry_count()
        logger.warning(
            "Dabble retry after status=%s",
            getattr(response, "status", "error"),
        )
        return next_retry


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = _DabbleRetry(
        total=1,
        backoff_factor=0.1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD", "OPTIONS"),
    )
    session.mount(
        "https://",
        requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=retry,
        ),
    )
    session.headers.update(DabbleAdapter.DEFAULT_HEADERS)
    return session


class _SerializedSession:
    """Adapt one injected client whose ``get`` method is not thread-safe.

    The lock covers only the session operation that mutates Requests' shared
    request state.  Response decoding and provider parsing stay outside the
    lock, so callers retain bounded detail workers while deterministic test
    doubles can safely be injected as one session.
    """

    def __init__(self, session: requests.Session | Any) -> None:
        self._session = session
        self._get_lock = threading.Lock()

    def acquire_request(self, deadline: float) -> "_SerializedSessionLease":
        """Reserve serialized access until the transport request completes.

        ``deadline`` is an absolute monotonic timestamp supplied by the
        transport seam.  It is deliberately separate from Requests' HTTP
        timeout tuple so queued callers can abandon lock acquisition before
        they reserve a transport slot or begin upstream work.
        """

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._get_lock.acquire(timeout=remaining):
            raise DeadlineExceededError("provider retrieval deadline exceeded")
        if time.monotonic() >= deadline:
            self._get_lock.release()
            raise DeadlineExceededError("provider retrieval deadline exceeded")
        return _SerializedSessionLease(self, deadline)

    def get(self, url: str, **kwargs: Any) -> Any:
        with self._get_lock:
            return self._session.get(url, **kwargs)


class _SerializedSessionLease:
    """One deadline-bounded serialized access reservation."""

    def __init__(self, owner: _SerializedSession, deadline: float) -> None:
        self._owner = owner
        self._deadline = deadline
        self._released = False

    def get(self, url: str, **kwargs: Any) -> Any:
        if self._released:
            raise RuntimeError("serialized session lease is released")
        if time.monotonic() >= self._deadline:
            self.release()
            raise DeadlineExceededError("provider retrieval deadline exceeded")
        try:
            return self._owner._session.get(url, **kwargs)
        finally:
            # Match ``_SerializedSession.get``: response decoding and
            # provider parsing must not hold the shared-session lock.
            self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._owner._get_lock.release()


class _ThreadLocalSession:
    """Create one independent client for each worker thread using the source."""

    def __init__(self, factory: Callable[[], requests.Session | Any]) -> None:
        self._factory = factory
        self._local = threading.local()

    def get(self, url: str, **kwargs: Any) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._factory()
            self._local.session = session
        return session.get(url, **kwargs)


@dataclass(frozen=True, slots=True)
class _ParsedRows:
    rows: tuple[dict[str, Any], ...]
    skipped_count: int = 0


@dataclass(frozen=True, slots=True)
class _DetailResult:
    fixture: dict[str, Any]
    competition: dict[str, Any]
    detail: dict[str, Any] | None = None
    failure: "_DabbleTerminalFailure | None" = None


class _DabbleFailureSource(str, Enum):
    """Closed set of Dabble seams that can own a terminal failure."""

    COMPETITION_DISCOVERY = "competition_discovery"
    FIXTURE_DISCOVERY = "fixture_discovery"
    FIXTURE_DETAILS = "fixture_details"
    SNAPSHOT_NORMALIZATION = "snapshot_normalization"


class _DabbleFailureOwner(str, Enum):
    """The seam responsible for recording a terminal provider failure."""

    UPSTREAM = "upstream"
    LOCAL = "local"


class _DabbleFailureStatus(str, Enum):
    """Closed status vocabulary used for sanitized Dabble failure details."""

    DEADLINE_EXCEEDED = "deadline_exceeded"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    HTTP_ERROR = "http_error"
    UPSTREAM_ERROR = "upstream_error"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class _DabbleTerminalFailure:
    """Typed failure ownership carried across Dabble's private seams.

    ``error`` is retained only for local translation inside the adapter.  It
    is never copied into telemetry or public error details; callers use the
    closed ``status`` and the exception type when they need diagnostics.
    """

    source: _DabbleFailureSource
    owner: _DabbleFailureOwner
    status: _DabbleFailureStatus
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _DabbleDiscovery:
    """Immutable discovery facts handed to the bounded detail pipeline."""

    fixtures: tuple[tuple[dict[str, Any], dict[str, Any]], ...]
    skipped_count: int = 0
    warning_codes: tuple[CoverageCode, ...] = ()
    skipped_reasons: tuple[CoverageCode, ...] = ()
    diagnostic_details: tuple[str, ...] = ()
    fanout_complete: bool = True
    failures: tuple[_DabbleTerminalFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class _DabbleNormalization:
    """One isolated, fully normalized fixture-detail observation."""

    batch: _NormalizedBatch
    skipped_count: int = 0
    warning_codes: tuple[CoverageCode, ...] = ()
    skipped_reasons: tuple[CoverageCode, ...] = ()
    diagnostic_details: tuple[str, ...] = ()
    fanout_complete: bool = True
    failures: tuple[_DabbleTerminalFailure, ...] = ()


class _DabbleRequestFailure(Exception):
    """Expected failure while fetching one Dabble resource."""

    def __init__(self, status: _DabbleFailureStatus, detail: Any = None) -> None:
        if not isinstance(status, _DabbleFailureStatus):
            raise TypeError("Dabble request failure status must be typed")
        self.status = status
        self.detail = detail
        super().__init__(status.value)


class _DabbleLocalValidationError(ProviderResponseError):
    """A post-parse Dabble validation failure with no upstream event."""


@dataclass
class _MarketAccumulator:
    market: PlayerProjectionMarket
    selections: dict[tuple[Any, ...], Selection]
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class _PropFacts:
    """Validated, provider-shaped facts needed to build one Dabble market."""

    player_name: str
    status: MarketStatus
    status_label: Any | None
    raw_stats: tuple[str, ...]
    components: tuple[str, ...]
    scoring_period: ScoringPeriod | str
    period_label: str | None
    value: Decimal
    source_value: str
    line_type: str
    provider_athlete_id: str | None
    variant_label: str | None


@dataclass(frozen=True, slots=True)
class _PropIdentity:
    """Raw identity and relationship evidence selected from Dabble resources."""

    fixture_id: str
    market_id: str | None
    selection_id: str | None
    competition_id: str | None
    sport_id: str | None
    sport_label: str | None
    competition_label: str | None
    event_label: str | None
    starts_at: Any
    updated_at: Any
    team_provider_id: str | None
    team_name: str | None
    team_abbreviation: str | None


@dataclass(frozen=True, slots=True)
class _PropEvidence:
    """Immutable evidence graph shared by a market and its athlete."""

    team: TeamEvidence
    athlete: AthleteEvidence
    sport: SportEvidence
    competition: CompetitionEvidence
    league: LeagueEvidence


class DabbleAdapter:
    """Retrieve Dabble's eligible NBA player projection markets.

    Production requests use a thread-local session factory, which keeps the
    useful fixture fan-out while avoiding concurrent mutation of one Requests
    session.  An explicitly injected ``session`` is treated as a test or
    legacy client and its ``get`` calls are serialized.  Callers that provide
    a concurrent-safe client source should use ``session_factory`` instead.
    """

    BASE_URL = "https://api.dabble.com.au"
    PROVIDER_ID = "dabble"
    DETAIL_CONCURRENCY = 3
    CONNECT_TIMEOUT_SECONDS = 3.0
    READ_TIMEOUT_SECONDS = 8.0
    DEFAULT_HEADERS = {
        "Accept": "application/json",
        "Accept-Language": "en-AU,en;q=0.9",
        "User-Agent": "Dabble/1000041710 CFNetwork/3826.600.41.2.1 Darwin/24.6.0",
        "X-Device-ID": "00000000-0000-0000-0000-000000000000",
        "X-App-Version": "4.17.10+019ededb",
    }

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        session_factory: Callable[[], requests.Session | Any] | None = None,
        detail_concurrency: int = DETAIL_CONCURRENCY,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = READ_TIMEOUT_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if detail_concurrency < 1:
            raise ValueError("detail_concurrency must be at least 1")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("provider timeouts must be positive")
        if session is not None and session_factory is not None:
            raise ValueError("provide session or session_factory, not both")
        if session_factory is not None:
            self.session = _ThreadLocalSession(session_factory)
            self._request_session = self.session
        elif session is not None:
            # Keep the injected object visible for existing test seams while
            # routing actual calls through its concurrency-safe adapter.
            self.session = session
            self._request_session = _SerializedSession(session)
        else:
            self.session = _ThreadLocalSession(_build_session)
            self._request_session = self.session
        self.detail_concurrency = detail_concurrency
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.read_timeout_seconds = float(read_timeout_seconds)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._transport_error_policy = TransportErrorPolicy(
            deadline_message="Dabble retrieval deadline exceeded.",
            timeout_message="Dabble timed out while fetching lines.",
            unavailable_message="Dabble could not be reached.",
            invalid_json_message="Dabble returned invalid JSON",
            failure_factory=self._request_failure,
        )

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Return one complete or partial snapshot for the semantic NBA query."""

        if not isinstance(query, NBAMarketQuery):
            raise TypeError("query must be NBAMarketQuery")
        if not isinstance(context, RetrievalContext):
            raise TypeError("context must be RetrievalContext")

        retrieved_at = self._now_utc()
        discovery = self._discover_fixtures(query, context)
        if not discovery.fixtures:
            upstream_failure = self._first_failure(
                discovery.failures,
                owner=_DabbleFailureOwner.UPSTREAM,
            )
            if upstream_failure is not None:
                raise self._unavailable(upstream_failure)
            local_failure = self._first_failure(
                discovery.failures,
                owner=_DabbleFailureOwner.LOCAL,
            )
            if local_failure is not None:
                return self._normalization_failure(context, local_failure)
            return _build_snapshot(
                provider=self.PROVIDER_ID,
                markets=(),
                retrieved_at=retrieved_at,
                skipped_count=discovery.skipped_count,
                pagination_complete=True,
                fanout_complete=discovery.fanout_complete,
                warning_codes=discovery.warning_codes,
                skipped_reasons=discovery.skipped_reasons,
                diagnostic_details=discovery.diagnostic_details,
            )

        detail_results = self._fetch_details(discovery.fixtures, context)
        if detail_results and all(result.failure is not None for result in detail_results):
            local_failure = self._first_failure(
                tuple(result.failure for result in detail_results if result.failure),
                owner=_DabbleFailureOwner.LOCAL,
            )
            if local_failure is not None:
                return self._normalization_failure(context, local_failure)
            raise self._unavailable(self._first_detail_failure(detail_results))
        if context.is_expired(now=self._now_utc()):
            raise self._unavailable(
                _DabbleRequestFailure(_DabbleFailureStatus.DEADLINE_EXCEEDED)
            )

        normalization: _DabbleNormalization | None = None
        local_failure: _DabbleTerminalFailure | None = None
        upstream_failure: _DabbleTerminalFailure | None = None
        snapshot: ProviderSnapshot | None = None
        try:
            with provider_normalization_call(
                PROVIDER_DABBLE,
                "snapshot_normalization",
                cache_status=CACHE_DISABLED,
                request_id=context.request_id,
            ):
                normalization = run_bounded(
                    context=context,
                    now=self._now_utc,
                    call=lambda: self._normalize_detail_results(detail_results, query),
                )
                if any(
                    failure.status is _DabbleFailureStatus.DEADLINE_EXCEEDED
                    for failure in normalization.failures
                ) or context.is_expired(now=self._now_utc()):
                    raise DeadlineExceededError(
                        "Dabble snapshot normalization deadline exceeded"
                    )

                batch = normalization.batch
                warnings = (
                    *discovery.warning_codes,
                    *normalization.warning_codes,
                    *batch.warning_codes,
                )
                skipped_reasons = (
                    *discovery.skipped_reasons,
                    *normalization.skipped_reasons,
                    *batch.skipped_reasons,
                )
                diagnostic_details = (
                    *discovery.diagnostic_details,
                    *normalization.diagnostic_details,
                    *batch.diagnostic_details,
                )
                skipped_count = (
                    discovery.skipped_count
                    + normalization.skipped_count
                    + batch.skipped_count
                )
                fanout_complete = (
                    discovery.fanout_complete
                    and normalization.fanout_complete
                    and batch.malformed_count == 0
                    and not any(
                        failure.status is _DabbleFailureStatus.MALFORMED
                        for failure in (*discovery.failures, *normalization.failures)
                    )
                )

                detail_local_failure = self._first_failure(
                    normalization.failures,
                    owner=_DabbleFailureOwner.LOCAL,
                    source=_DabbleFailureSource.FIXTURE_DETAILS,
                )
                upstream_failure = self._first_failure(
                    (*discovery.failures, *normalization.failures),
                    owner=_DabbleFailureOwner.UPSTREAM,
                )
                if detail_local_failure is not None:
                    # A local post-parse validation failure has no upstream
                    # event.  Raise it through this local seam exactly once;
                    # the catch below can still return usable partial markets.
                    local_failure = detail_local_failure
                    raise self._failure_response(detail_local_failure)

                if not batch.markets:
                    local_failure = self._first_failure(
                        (*normalization.failures, *discovery.failures),
                        owner=_DabbleFailureOwner.LOCAL,
                    )
                    if local_failure is not None:
                        raise self._failure_response(local_failure)
                    if upstream_failure is None and not normalization.fanout_complete:
                        raise ProviderResponseError(
                            "Dabble produced no usable markets"
                        )
                    if upstream_failure is None:
                        snapshot = _build_snapshot(
                            provider=self.PROVIDER_ID,
                            markets=(),
                            retrieved_at=retrieved_at,
                            fetched_count=batch.fetched_count,
                            eligible_count=batch.eligible_count,
                            normalized_count=batch.normalized_count,
                            skipped_count=skipped_count,
                            pagination_complete=True,
                            fanout_complete=fanout_complete,
                            warning_codes=warnings,
                            skipped_reasons=skipped_reasons,
                            diagnostic_details=diagnostic_details,
                        )
                else:
                    snapshot = _build_snapshot(
                        provider=self.PROVIDER_ID,
                        markets=batch.markets,
                        retrieved_at=retrieved_at,
                        fetched_count=batch.fetched_count,
                        eligible_count=batch.eligible_count,
                        normalized_count=batch.normalized_count,
                        skipped_count=skipped_count,
                        pagination_complete=True,
                        fanout_complete=fanout_complete,
                        warning_codes=warnings,
                        skipped_reasons=skipped_reasons,
                        diagnostic_details=diagnostic_details,
                    )
        except DeadlineExceededError as error:
            raise self._unavailable(
                _DabbleRequestFailure(_DabbleFailureStatus.DEADLINE_EXCEEDED, error)
            ) from error
        except ProviderResponseError as error:
            if (
                normalization is not None
                and normalization.batch.markets
                and local_failure is not None
                and local_failure.source is _DabbleFailureSource.FIXTURE_DETAILS
            ):
                return _build_snapshot(
                    provider=self.PROVIDER_ID,
                    markets=normalization.batch.markets,
                    retrieved_at=retrieved_at,
                    fetched_count=normalization.batch.fetched_count,
                    eligible_count=normalization.batch.eligible_count,
                    normalized_count=normalization.batch.normalized_count,
                    skipped_count=(
                        discovery.skipped_count
                        + normalization.skipped_count
                        + normalization.batch.skipped_count
                    ),
                    pagination_complete=True,
                    fanout_complete=(
                        discovery.fanout_complete
                        and normalization.fanout_complete
                        and not (
                            discovery.failures
                            or normalization.batch.malformed_count > 0
                        )
                    ),
                    warning_codes=(
                        *discovery.warning_codes,
                        *normalization.warning_codes,
                        *normalization.batch.warning_codes,
                    ),
                    skipped_reasons=(
                        *discovery.skipped_reasons,
                        *normalization.skipped_reasons,
                        *normalization.batch.skipped_reasons,
                    ),
                    diagnostic_details=(
                        *discovery.diagnostic_details,
                        *normalization.diagnostic_details,
                        *normalization.batch.diagnostic_details,
                    ),
                )
            raise self._unavailable(error) from error
        if upstream_failure is not None and snapshot is None:
            raise self._unavailable(upstream_failure)
        assert snapshot is not None
        return snapshot

    def _discover_fixtures(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> _DabbleDiscovery:
        """Discover eligible fixtures and own discovery coverage decisions."""

        warning_codes: list[CoverageCode] = []
        skipped_reasons: list[CoverageCode] = []
        skipped_count = 0
        fanout_complete = True
        failures: list[_DabbleTerminalFailure] = []

        def warn(code: CoverageCode) -> None:
            if code not in warning_codes:
                warning_codes.append(code)

        def skip(reason: CoverageCode, *, warning: bool = False) -> None:
            nonlocal skipped_count
            skipped_count += 1
            if reason not in skipped_reasons:
                skipped_reasons.append(reason)
            if warning:
                warn(reason)

        try:
            competitions_result = self._request_json(
                context,
                "competition_lookup",
                "/competitions",
                params={"name": query.league},
                parser=self._parse_competitions,
            )
        except (_DabbleRequestFailure, ProviderResponseError) as error:
            raise self._unavailable(error) from error

        competitions = competitions_result.rows
        if competitions_result.skipped_count:
            warn(CoverageCode.MALFORMED_RECORD)
            for _ in range(competitions_result.skipped_count):
                skip(CoverageCode.MALFORMED_RECORD)
            fanout_complete = False
            failures.append(
                self._local_failure(
                    _DabbleFailureSource.COMPETITION_DISCOVERY,
                    "Dabble competition records were malformed",
                )
            )

        nba_competitions: list[dict[str, Any]] = []
        for competition in competitions:
            if not self._is_nba_competition(competition, query):
                skip(CoverageCode.NON_NBA_COMPETITION)
                continue
            if not competition.get("id"):
                skip(CoverageCode.MISSING_COMPETITION_ID, warning=True)
                fanout_complete = False
                failures.append(
                    self._local_failure(
                        _DabbleFailureSource.COMPETITION_DISCOVERY,
                        "Dabble competition id was missing",
                    )
                )
                continue
            nba_competitions.append(competition)

        fixtures: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for competition in nba_competitions:
            if context.is_expired(now=self._now_utc()):
                raise self._unavailable(
                    _DabbleRequestFailure(_DabbleFailureStatus.DEADLINE_EXCEEDED)
                )
            try:
                fixtures_result = self._request_json(
                    context,
                    "competition_fixtures",
                    "/frontend-api/competitions/"
                    f"{quote(str(competition['id']), safe='')}/sport-fixtures",
                    params={"includeInPlay": "false"},
                    parser=self._parse_fixtures,
                )
            except (_DabbleRequestFailure, ProviderResponseError) as error:
                failure = self._failure_from_exception(
                    error,
                    source=_DabbleFailureSource.FIXTURE_DISCOVERY,
                    owner=_DabbleFailureOwner.UPSTREAM,
                )
                if failure.status is _DabbleFailureStatus.DEADLINE_EXCEEDED:
                    raise self._unavailable(failure) from error
                failures.append(failure)
                fanout_complete = False
                warn(CoverageCode.FIXTURE_LIST_FAILED)
                skip(CoverageCode.FIXTURE_LIST_FAILED)
                logger.warning("Dabble fixture list failed: %s", type(error).__name__)
                continue
            if fixtures_result.skipped_count:
                warn(CoverageCode.MALFORMED_RECORD)
                for _ in range(fixtures_result.skipped_count):
                    skip(CoverageCode.MALFORMED_RECORD)
                fanout_complete = False
                failures.append(
                    self._local_failure(
                        _DabbleFailureSource.FIXTURE_DISCOVERY,
                        "Dabble fixture records were malformed",
                    )
                )
            for fixture in fixtures_result.rows:
                if not self._fixture_matches_query(fixture):
                    skip(CoverageCode.NON_NBA_SPORT)
                    continue
                try:
                    status = normalize_market_status(fixture.get("status")).value
                except ValueError:
                    skip(CoverageCode.INELIGIBLE_STATUS)
                    continue
                if status.value not in query.market_statuses:
                    skip(CoverageCode.INELIGIBLE_STATUS)
                    continue
                fixtures.append((fixture, competition))

        if context.is_expired(now=self._now_utc()):
            raise self._unavailable(
                _DabbleRequestFailure(_DabbleFailureStatus.DEADLINE_EXCEEDED)
            )
        return _DabbleDiscovery(
            fixtures=tuple(fixtures),
            skipped_count=skipped_count,
            warning_codes=tuple(warning_codes),
            skipped_reasons=tuple(skipped_reasons),
            fanout_complete=fanout_complete,
            failures=tuple(failures),
        )

    def _normalize_detail_results(
        self,
        detail_results: Sequence[_DetailResult],
        query: NBAMarketQuery,
    ) -> _DabbleNormalization:
        """Normalize and merge details in caller-isolated state."""

        merged_markets: dict[tuple[Any, ...], _MarketAccumulator] = {}
        conflicted_market_keys: set[tuple[Any, ...]] = set()
        warning_codes: list[CoverageCode] = []
        skipped_reasons: list[CoverageCode] = []
        diagnostic_details: list[str] = []
        record_coverage = _RecordCoverageAccumulator()
        skipped_count = 0
        fanout_complete = True
        failures: list[_DabbleTerminalFailure] = []

        def merge(
            normalized: tuple[tuple[Any, ...], PlayerProjectionMarket, Selection],
        ) -> None:
            self._merge_normalized_prop(
                normalized,
                warning_codes,
                merged_markets,
                conflicted_market_keys,
            )

        for result in detail_results:
            if result.failure is not None:
                failure = result.failure
                failures.append(failure)
                fanout_complete = False
                if failure.status is _DabbleFailureStatus.MALFORMED:
                    warning_codes.append(CoverageCode.FIXTURE_MALFORMED)
                    skipped_reasons.append(CoverageCode.FIXTURE_MALFORMED)
                else:
                    warning_codes.append(CoverageCode.FIXTURE_FAILED)
                    skipped_reasons.append(CoverageCode.FIXTURE_FAILED)
                skipped_count += 1
                continue
            assert result.detail is not None
            detail = result.detail
            props = detail.get("playerProps")
            if not isinstance(props, list):
                fanout_complete = False
                warning_codes.append(CoverageCode.FIXTURE_MALFORMED)
                skipped_reasons.append(CoverageCode.FIXTURE_MALFORMED)
                skipped_count += 1
                failures.append(
                    self._local_failure(
                        _DabbleFailureSource.FIXTURE_DETAILS,
                        "Dabble fixture details were malformed after parsing",
                    )
                )
                continue
            record_coverage.extend(
                props,
                lambda value: self._normalize_prop(
                    value,
                    detail=detail,
                    fixture=result.fixture,
                    competition=result.competition,
                    query=query,
                ),
                on_success=merge,
            )
            if record_coverage.malformed_count:
                fanout_complete = False

        batch = _NormalizedBatch.from_accumulator(
            record_coverage,
            markets=self._assemble_markets(merged_markets),
            warning_codes=warning_codes,
        )
        if not batch.markets and batch.malformed_count:
            failures.append(
                self._local_failure(
                    _DabbleFailureSource.SNAPSHOT_NORMALIZATION,
                    "Dabble produced no usable markets",
                )
            )
        return _DabbleNormalization(
            batch=batch,
            skipped_count=skipped_count,
            warning_codes=tuple(warning_codes),
            skipped_reasons=tuple(skipped_reasons),
            diagnostic_details=tuple(diagnostic_details),
            fanout_complete=fanout_complete,
            failures=tuple(failures),
        )

    def _assemble_markets(
        self,
        merged_markets: Mapping[tuple[Any, ...], _MarketAccumulator],
    ) -> tuple[PlayerProjectionMarket, ...]:
        """Finalize merged selections only after normalization is committed."""

        market_values: list[PlayerProjectionMarket] = []
        for accumulator in merged_markets.values():
            selections = tuple(
                accumulator.selections[key]
                for key in sorted(accumulator.selections, key=str)
            )
            market_values.append(
                replace(
                    accumulator.market,
                    variant=(
                        None
                        if accumulator.market.variant is MarketVariant.UNKNOWN
                        else accumulator.market.variant
                    ),
                    selections=selections,
                )
            )
        return tuple(sorted(market_values, key=self._market_sort_key))

    def _merge_normalized_prop(
        self,
        normalized: tuple[tuple[Any, ...], PlayerProjectionMarket, Selection],
        warning_codes: list[CoverageCode],
        merged_markets: dict[tuple[Any, ...], _MarketAccumulator],
        conflicted_market_keys: set[tuple[Any, ...]],
    ) -> None:
        market_key, market, selection = normalized
        if market_key in conflicted_market_keys:
            if CoverageCode.CONFLICTING_SOURCE_IDENTITY not in warning_codes:
                warning_codes.append(CoverageCode.CONFLICTING_SOURCE_IDENTITY)
            raise CoverageRecordMalformed(
                "repeated Dabble market identity has conflicting content",
                code=CoverageCode.CONFLICTING_SOURCE_IDENTITY,
            )
        accumulator = merged_markets.get(market_key)
        if accumulator is None:
            merged_markets[market_key] = _MarketAccumulator(
                market=market,
                selections={self._selection_key(selection): selection},
            )
            return

        if self._market_signature(accumulator.market) != self._market_signature(market):
            accumulator.conflict = True
            merged_markets.pop(market_key, None)
            conflicted_market_keys.add(market_key)
            if CoverageCode.CONFLICTING_SOURCE_IDENTITY not in warning_codes:
                warning_codes.append(CoverageCode.CONFLICTING_SOURCE_IDENTITY)
            raise CoverageRecordMalformed(
                "repeated Dabble market identity has conflicting content",
                code=CoverageCode.CONFLICTING_SOURCE_IDENTITY,
            )

        selection_key = self._selection_key(selection)
        previous = accumulator.selections.get(selection_key)
        if previous is None:
            accumulator.selections[selection_key] = selection
            return
        if previous != selection:
            accumulator.conflict = True
            merged_markets.pop(market_key, None)
            conflicted_market_keys.add(market_key)
            if CoverageCode.CONFLICTING_SOURCE_IDENTITY not in warning_codes:
                warning_codes.append(CoverageCode.CONFLICTING_SOURCE_IDENTITY)
            raise CoverageRecordMalformed(
                "repeated Dabble selection identity has conflicting content",
                code=CoverageCode.CONFLICTING_SOURCE_IDENTITY,
            )
        if CoverageCode.DUPLICATE_SOURCE_IDENTITY not in warning_codes:
            warning_codes.append(CoverageCode.DUPLICATE_SOURCE_IDENTITY)

    def _normalization_failure(
        self,
        context: RetrievalContext,
        failure: _DabbleTerminalFailure,
    ) -> NoReturn:
        """Record a local terminal failure exactly once before translating it."""

        try:
            with provider_normalization_call(
                PROVIDER_DABBLE,
                "snapshot_normalization",
                cache_status=CACHE_DISABLED,
                request_id=context.request_id,
            ):
                raise self._failure_response(failure)
        except ProviderResponseError as error:
            raise self._unavailable(error) from error

    @staticmethod
    def _failure_response(failure: _DabbleTerminalFailure) -> ProviderResponseError:
        """Translate a typed local failure without exposing source details."""

        del failure
        return ProviderResponseError("Dabble produced no usable markets")

    @staticmethod
    def _local_failure(
        source: _DabbleFailureSource,
        detail: str,
    ) -> _DabbleTerminalFailure:
        return _DabbleTerminalFailure(
            source=source,
            owner=_DabbleFailureOwner.LOCAL,
            status=_DabbleFailureStatus.MALFORMED,
            error=_DabbleLocalValidationError(detail),
        )

    @staticmethod
    def _failure_from_exception(
        error: Exception,
        *,
        source: _DabbleFailureSource,
        owner: _DabbleFailureOwner,
    ) -> _DabbleTerminalFailure:
        if isinstance(error, _DabbleRequestFailure):
            status = error.status
        elif isinstance(error, ProviderResponseError):
            status = _DabbleFailureStatus.MALFORMED
        elif isinstance(error, DeadlineExceededError):
            status = _DabbleFailureStatus.DEADLINE_EXCEEDED
        elif isinstance(error, requests.exceptions.Timeout):
            status = _DabbleFailureStatus.TIMEOUT
        elif isinstance(error, requests.exceptions.HTTPError):
            status = _DabbleFailureStatus.HTTP_ERROR
        else:
            status = _DabbleFailureStatus.UPSTREAM_ERROR
        return _DabbleTerminalFailure(
            source=source,
            owner=owner,
            status=status,
            error=error,
        )

    @staticmethod
    def _first_failure(
        failures: Sequence[_DabbleTerminalFailure],
        *,
        owner: _DabbleFailureOwner | None = None,
        source: _DabbleFailureSource | None = None,
    ) -> _DabbleTerminalFailure | None:
        candidates = [
            failure
            for failure in failures
            if (owner is None or failure.owner is owner)
            and (source is None or failure.source is source)
        ]
        for failure in candidates:
            if failure.status is _DabbleFailureStatus.DEADLINE_EXCEEDED:
                return failure
        return candidates[0] if candidates else None

    @staticmethod
    def _first_detail_failure(
        detail_results: Sequence[_DetailResult],
    ) -> _DabbleTerminalFailure:
        """Return an upstream detail failure for an all-failed fan-out."""

        failures = [
            result.failure for result in detail_results if result.failure is not None
        ]
        for failure in failures:
            if failure.status is _DabbleFailureStatus.DEADLINE_EXCEEDED:
                return failure
        assert failures
        return failures[0]

    def _fetch_details(
        self,
        fixtures: Sequence[tuple[dict[str, Any], dict[str, Any]]],
        context: RetrievalContext,
    ) -> list[_DetailResult]:
        """Fetch details with bounded workers and deterministic result order."""

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.detail_concurrency, len(fixtures))
        )
        pending: dict[concurrent.futures.Future[Any], tuple[int, dict[str, Any], dict[str, Any]]] = {}
        results: dict[int, _DetailResult] = {}
        iterator: Iterator[tuple[int, dict[str, Any], dict[str, Any]]] = iter(
            (index, fixture, competition)
            for index, (fixture, competition) in enumerate(fixtures)
        )

        def submit_next() -> bool:
            if context.is_expired(now=self._now_utc()):
                return False
            try:
                index, fixture, competition = next(iterator)
            except StopIteration:
                return False
            future = executor.submit(
                self._fetch_detail,
                fixture,
                competition,
                context,
            )
            pending[future] = (index, fixture, competition)
            return True

        def cancel_pending() -> None:
            for future in pending:
                future.cancel()

        try:
            for _ in range(min(self.detail_concurrency, len(fixtures))):
                if not submit_next():
                    break
            while pending:
                remaining = context.remaining_seconds(now=self._now_utc())
                if remaining <= 0:
                    cancel_pending()
                    break
                done, _ = concurrent.futures.wait(
                    pending,
                    timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done or context.is_expired(now=self._now_utc()):
                    cancel_pending()
                    break
                for future in done:
                    if context.is_expired(now=self._now_utc()):
                        cancel_pending()
                        break
                    index, fixture, competition = pending.pop(future)
                    try:
                        detail = future.result()
                        results[index] = _DetailResult(
                            fixture=fixture,
                            competition=competition,
                            detail=detail,
                        )
                    except ProviderResponseError as error:
                        owner = (
                            _DabbleFailureOwner.LOCAL
                            if isinstance(error, _DabbleLocalValidationError)
                            else _DabbleFailureOwner.UPSTREAM
                        )
                        results[index] = _DetailResult(
                            fixture=fixture,
                            competition=competition,
                            failure=self._failure_from_exception(
                                error,
                                source=_DabbleFailureSource.FIXTURE_DETAILS,
                                owner=owner,
                            ),
                        )
                    except _DabbleRequestFailure as error:
                        results[index] = _DetailResult(
                            fixture=fixture,
                            competition=competition,
                            failure=self._failure_from_exception(
                                error,
                                source=_DabbleFailureSource.FIXTURE_DETAILS,
                                owner=_DabbleFailureOwner.UPSTREAM,
                            ),
                        )
                    except Exception:  # implementation defects stay visible
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise
                else:
                    submit_next()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        for index, (fixture, competition) in enumerate(fixtures):
            if index not in results:
                results[index] = _DetailResult(
                    fixture=fixture,
                    competition=competition,
                    failure=self._failure_from_exception(
                        _DabbleRequestFailure(_DabbleFailureStatus.DEADLINE_EXCEEDED),
                        source=_DabbleFailureSource.FIXTURE_DETAILS,
                        owner=_DabbleFailureOwner.UPSTREAM,
                    ),
                )
        return [results[index] for index in range(len(fixtures))]

    def _fetch_detail(
        self,
        fixture: dict[str, Any],
        competition: dict[str, Any],
        context: RetrievalContext,
    ) -> dict[str, Any]:
        fixture_id = str(fixture["id"])
        detail = self._request_json(
            context,
            "fixture_details",
            "/frontend-api/sport-fixtures/details/"
            f"{quote(fixture_id, safe='')}",
            parser=self._parse_detail,
        )
        detail_id = detail.get("id")
        if detail_id is not None and str(detail_id) != fixture_id:
            raise _DabbleLocalValidationError(
                "Dabble fixture detail id conflicts with fixture"
            )
        detail["id"] = fixture_id
        detail.setdefault("competitionId", competition.get("id"))
        detail.setdefault("competitionName", competition.get("name"))
        detail.setdefault("sportId", competition.get("sport_id"))
        detail.setdefault("sportName", competition.get("sport"))
        detail.setdefault("advertisedStart", fixture.get("advertisedStart"))
        detail.setdefault("status", fixture.get("status"))
        return detail

    @staticmethod
    def _parse_competitions(payload: Any) -> _ParsedRows:
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise ProviderResponseError("Dabble competitions payload is malformed")
        normalized: list[dict[str, Any]] = []
        skipped = 0
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("name"):
                skipped += 1
                continue
            normalized.append(
                {
                    "id": str(row["id"]) if row.get("id") else None,
                    "name": str(row["name"]).strip(),
                    "sport_id": str(row["sportId"]) if row.get("sportId") else None,
                    "sport": str(row["sportName"]).strip()
                    if row.get("sportName")
                    else None,
                    "country": str(row["country"]).strip()
                    if row.get("country")
                    else None,
                }
            )
        return _ParsedRows(tuple(normalized), skipped)

    @staticmethod
    def _parse_fixtures(payload: Any) -> _ParsedRows:
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise ProviderResponseError("Dabble fixtures payload is malformed")
        normalized: list[dict[str, Any]] = []
        skipped = 0
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("id"):
                skipped += 1
                continue
            normalized.append(copy.deepcopy(dict(row)))
        return _ParsedRows(tuple(normalized), skipped)

    @staticmethod
    def _parse_detail(payload: Any) -> dict[str, Any]:
        detail = (
            payload.get("sportFixtureDetail")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(detail, Mapping):
            raise ProviderResponseError("Dabble fixture details are malformed")
        props = detail.get("playerProps")
        if not isinstance(props, list):
            raise ProviderResponseError("Dabble playerProps must be a list")
        return copy.deepcopy(dict(detail))

    def _normalize_prop(
        self,
        prop: Any,
        *,
        detail: Mapping[str, Any],
        fixture: Mapping[str, Any],
        competition: Mapping[str, Any],
        query: NBAMarketQuery,
    ) -> tuple[tuple[Any, ...], PlayerProjectionMarket, Selection]:
        facts = self._classify_prop(
            prop,
            detail=detail,
            fixture=fixture,
            competition=competition,
            query=query,
        )
        identity = self._extract_prop_identity(
            prop,
            detail=detail,
            fixture=fixture,
            competition=competition,
        )
        try:
            evidence = self._build_prop_evidence(
                facts=facts,
                identity=identity,
            )
            market = self._build_market(facts=facts, identity=identity, evidence=evidence)
            selection = self._build_selection(prop, facts=facts, identity=identity)
        except CoverageRecordMalformed:
            raise
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error
        return self._market_key(identity, facts, evidence, market), market, selection

    def _classify_prop(
        self,
        prop: Any,
        *,
        detail: Mapping[str, Any],
        fixture: Mapping[str, Any],
        competition: Mapping[str, Any],
        query: NBAMarketQuery,
    ) -> _PropFacts:
        """Apply Dabble's inclusion rules and validate projection facts."""

        if not isinstance(prop, Mapping):
            raise CoverageRecordMalformed("Dabble player prop must be an object")

        kind = prop.get("marketType", prop.get("market_type", prop.get("type")))
        if isinstance(kind, str) and kind.strip().casefold() in _NON_PLAYER_KINDS:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
        player_name = optional_text(prop.get("playerName"))
        if player_name is None:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)

        status, status_label = self._eligible_prop_status(
            prop,
            detail=detail,
            fixture=fixture,
            query=query,
        )
        source_sport = (
            detail.get("sportName")
            or fixture.get("sportName")
            or competition.get("sport")
        )
        if (
            source_sport is not None
            and str(source_sport).strip().casefold() not in _BASKETBALL_LABELS
        ):
            raise CoverageRecordExcluded(CoverageCode.NON_NBA_SPORT)

        raw_stats = self._validated_stats(prop.get("stats"))
        normalized_stats = canonical_stat_components(raw_stats)
        if not normalized_stats:
            raise CoverageRecordMalformed("Dabble player prop stats are empty")

        value, source_value = self._decimal_source(prop.get("value"), "value")
        line_type = prop.get("lineType")
        if not isinstance(line_type, str) or not line_type.strip():
            raise CoverageRecordMalformed("Dabble lineType is malformed")
        components, scoring_period, period_label = self._statistic_period(
            normalized_stats
        )
        return _PropFacts(
            player_name=player_name,
            status=status,
            status_label=status_label,
            raw_stats=tuple(raw_stats),
            components=tuple(components),
            scoring_period=scoring_period,
            period_label=period_label,
            value=value,
            source_value=source_value,
            line_type=line_type.strip(),
            provider_athlete_id=self._optional_id(prop.get("playerId")),
            variant_label=optional_text(
                prop.get("variant") or prop.get("marketVariant")
            ),
        )

    @staticmethod
    def _eligible_prop_status(
        prop: Mapping[str, Any],
        *,
        detail: Mapping[str, Any],
        fixture: Mapping[str, Any],
        query: NBAMarketQuery,
    ) -> tuple[MarketStatus, Any | None]:
        """Require every available status source to agree with the query."""

        status_labels = tuple(
            label
            for label in (
                prop.get("status"),
                detail.get("status"),
                fixture.get("status"),
            )
            if label is not None
        )
        status_label = next(iter(status_labels), None)
        try:
            normalized_statuses = tuple(
                normalize_market_status(label) for label in status_labels
            )
        except ValueError as error:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS) from error
        if not normalized_statuses or any(
            status.value.value not in query.market_statuses
            for status in normalized_statuses
        ):
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS)
        return normalized_statuses[0].value, status_label

    @staticmethod
    def _validated_stats(value: Any) -> list[str]:
        """Validate and retain each source statistic label before sorting."""

        if not isinstance(value, list) or not value:
            raise CoverageRecordMalformed("Dabble player prop stats are malformed")
        raw_stats: list[str] = []
        for stat in value:
            if not isinstance(stat, str) or not stat.strip():
                raise CoverageRecordMalformed("Dabble player prop stat is malformed")
            raw_stats.append(stat.strip())
        return raw_stats

    def _extract_prop_identity(
        self,
        prop: Mapping[str, Any],
        *,
        detail: Mapping[str, Any],
        fixture: Mapping[str, Any],
        competition: Mapping[str, Any],
    ) -> _PropIdentity:
        """Select IDs and labels using Dabble's fallback relationships."""

        return _PropIdentity(
            fixture_id=str(detail.get("id") or fixture.get("id")),
            market_id=self._optional_id(prop.get("marketId")),
            selection_id=self._optional_id(prop.get("selectionId")),
            competition_id=self._optional_id(
                detail.get("competitionId")
                or fixture.get("competitionId")
                or competition.get("id")
            ),
            sport_id=self._optional_id(
                detail.get("sportId")
                or fixture.get("sportId")
                or competition.get("sport_id")
            ),
            sport_label=optional_text(
                detail.get("sportName")
                or fixture.get("sportName")
                or competition.get("sport")
            ),
            competition_label=optional_text(
                detail.get("competitionName")
                or fixture.get("competitionName")
                or competition.get("name")
            ),
            event_label=optional_text(detail.get("name") or fixture.get("name")),
            starts_at=detail.get("advertisedStart") or fixture.get("advertisedStart"),
            updated_at=detail.get("updatedAt") or detail.get("updated_at"),
            team_provider_id=self._optional_id(prop.get("teamId")),
            team_name=optional_text(prop.get("teamName")),
            team_abbreviation=optional_text(prop.get("teamAbbreviation")),
        )

    @staticmethod
    def _build_prop_evidence(
        facts: _PropFacts,
        identity: _PropIdentity,
    ) -> _PropEvidence:
        """Construct the shared evidence graph once for market consumers."""

        team = TeamEvidence(
            provider_id=identity.team_provider_id,
            name=identity.team_name,
            abbreviation=identity.team_abbreviation,
        )
        athlete = AthleteEvidence(
            provider_id=facts.provider_athlete_id,
            name=facts.player_name,
            team=team,
        )
        sport = SportEvidence(provider_id=identity.sport_id, label=identity.sport_label)
        competition = CompetitionEvidence(
            provider_id=identity.competition_id,
            label=identity.competition_label,
            sport=sport,
        )
        return _PropEvidence(
            team=team,
            athlete=athlete,
            sport=sport,
            competition=competition,
            league=LeagueEvidence(
                provider_id=identity.competition_id,
                label=identity.competition_label,
            ),
        )

    def _build_market(
        self,
        *,
        facts: _PropFacts,
        identity: _PropIdentity,
        evidence: _PropEvidence,
    ) -> PlayerProjectionMarket:
        """Build one immutable market from validated facts and evidence."""

        return PlayerProjectionMarket(
            provider=self.PROVIDER_ID,
            market_id=identity.market_id,
            athlete=evidence.athlete,
            event=EventEvidence(
                provider_id=identity.fixture_id,
                label=identity.event_label,
                starts_at=identity.starts_at,
                updated_at=identity.updated_at,
            ),
            team=evidence.team,
            league=evidence.league,
            competition=evidence.competition,
            sport=evidence.sport,
            statistic=StatisticEvidence(
                label="+".join(facts.raw_stats),
                components=facts.components,
            ),
            threshold=MarketThreshold(
                value=facts.value,
                unit="count",
                original_value=facts.source_value,
            ),
            status=facts.status,
            status_label=(
                str(facts.status_label).strip()
                if facts.status_label is not None
                else None
            ),
            variant=facts.variant_label,
            variant_label=facts.variant_label,
            scoring_period=facts.scoring_period,
            scoring_period_label=facts.period_label,
            starts_at=identity.starts_at,
            updated_at=identity.updated_at,
        )

    def _build_selection(
        self,
        prop: Mapping[str, Any],
        *,
        facts: _PropFacts,
        identity: _PropIdentity,
    ) -> Selection:
        """Normalize an optional multiplier and construct the selectable side."""

        modifiers: tuple[SelectionModifier, ...] = ()
        if "multiplier" in prop and prop.get("multiplier") is not None:
            multiplier, _ = self._decimal_source(prop.get("multiplier"), "multiplier")
            if multiplier <= 0:
                raise CoverageRecordMalformed("Dabble multiplier must be positive")
            multiplier_label = optional_text(
                prop.get("multiplierLabel")
                or prop.get("payoutMultiplierLabel")
                or prop.get("modifierLabel")
            )
            modifiers = (
                SelectionModifier(
                    value=multiplier,
                    kind="multiplier",
                    scope="selection",
                    label=multiplier_label,
                ),
            )
        status = (
            str(facts.status_label).strip()
            if facts.status_label is not None
            else None
        )
        return Selection(
            selection_id=identity.selection_id,
            label=facts.line_type,
            direction=facts.line_type,
            direction_label=facts.line_type,
            status=status,
            modifiers=modifiers,
        )

    @staticmethod
    def _market_key(
        identity: _PropIdentity,
        facts: _PropFacts,
        evidence: _PropEvidence,
        market: PlayerProjectionMarket,
    ) -> tuple[Any, ...]:
        """Choose explicit market identity, or a complete evidence identity."""

        if identity.market_id is not None:
            return ("market", identity.market_id)
        return (
            "evidence",
            identity.fixture_id,
            evidence.athlete.provider_id,
            evidence.athlete.name,
            evidence.team.provider_id,
            evidence.team.name,
            facts.components,
            facts.value,
            facts.status.value,
            market.scoring_period,
            market.scoring_period_label,
            market.variant,
            market.variant_label,
        )

    @staticmethod
    def _statistic_period(
        stats: Sequence[str],
    ) -> tuple[tuple[str, ...], ScoringPeriod | str, str | None]:
        prefixes = (
            ("first-half-", "first half", ScoringPeriod.FIRST_HALF),
            ("second-half-", "second half", ScoringPeriod.SECOND_HALF),
            ("first-quarter-", "first quarter", ScoringPeriod.FIRST_QUARTER),
            ("second-quarter-", "second quarter", ScoringPeriod.SECOND_QUARTER),
        )
        for prefix, label, period in prefixes:
            if all(stat.startswith(prefix) for stat in stats):
                return tuple(stat.removeprefix(prefix) for stat in stats), period, label
        if all(stat in _STAT_ORDER for stat in stats):
            return tuple(stats), ScoringPeriod.FULL_GAME, None
        return tuple(stats), ScoringPeriod.UNKNOWN, None

    @staticmethod
    def _selection_key(selection: Selection) -> tuple[Any, ...]:
        if selection.selection_id is not None:
            return ("id", selection.selection_id)
        return (
            "evidence",
            selection.direction,
            selection.direction_label,
            selection.label,
            selection.status,
            selection.modifiers,
        )

    @staticmethod
    def _market_signature(market: PlayerProjectionMarket) -> tuple[Any, ...]:
        """Compare normalized facts while ignoring source label ordering."""

        statistic = market.statistic
        return (
            market.provider,
            market.market_id,
            market.athlete,
            market.event,
            market.team,
            market.opponent,
            market.league,
            market.competition,
            market.sport,
            statistic.provider_id if statistic else None,
            statistic.canonical_id if statistic else None,
            statistic.components if statistic else (),
            market.threshold,
            market.status,
            (market.status_label or "").casefold(),
            market.variant,
            market.variant_label,
            market.scoring_period,
            market.starts_at,
            market.updated_at,
        )

    @staticmethod
    def _market_sort_key(market: PlayerProjectionMarket) -> tuple[str, str, str]:
        return (
            market.event.provider_id or "",
            market.athlete.provider_id
            if market.athlete and market.athlete.provider_id
            else "",
            market.market_id or "",
        )

    @staticmethod
    def _is_nba_competition(
        competition: Mapping[str, Any],
        query: NBAMarketQuery,
    ) -> bool:
        if str(competition.get("name", "")).strip().casefold() != query.league.casefold():
            return False
        sport = competition.get("sport")
        return sport is None or str(sport).strip().casefold() in _BASKETBALL_LABELS

    @staticmethod
    def _fixture_matches_query(
        fixture: Mapping[str, Any],
    ) -> bool:
        sport = fixture.get("sportName")
        return sport is None or str(sport).strip().casefold() in _BASKETBALL_LABELS

    def _request_json(
        self,
        context: RetrievalContext,
        operation: str,
        path: str,
        *,
        parser: Callable[[Any], Any],
        params: dict[str, Any] | None = None,
    ) -> Any:
        return request_json(
            context=context,
            session=self._request_session,
            url=f"{self.BASE_URL}{path}",
            params=params,
            timeout=(self.connect_timeout_seconds, self.read_timeout_seconds),
            now=self._now_utc,
            provider=PROVIDER_DABBLE,
            operation=operation,
            parse=parser,
            error_policy=self._transport_error_policy,
        )

    @staticmethod
    def _request_failure(reason: str, error: Exception) -> Exception:
        if reason == "http_error":
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            status = (
                _DabbleFailureStatus.RATE_LIMITED
                if status_code == 429
                else _DabbleFailureStatus.ACCESS_DENIED
                if status_code in {401, 403}
                else _DabbleFailureStatus.UPSTREAM_ERROR
            )
            return _DabbleRequestFailure(status, error)
        status_by_reason = {
            "deadline_exceeded": _DabbleFailureStatus.DEADLINE_EXCEEDED,
            "timeout": _DabbleFailureStatus.TIMEOUT,
            "request_error": _DabbleFailureStatus.HTTP_ERROR,
        }
        return _DabbleRequestFailure(
            status_by_reason.get(reason, _DabbleFailureStatus.UPSTREAM_ERROR),
            error,
        )

    def _unavailable(
        self,
        error: Exception | _DabbleTerminalFailure,
    ) -> ProviderUnavailableError:
        if isinstance(error, _DabbleTerminalFailure):
            status = error.status
            diagnostic_error = error.error or error
        elif isinstance(error, _DabbleRequestFailure):
            status = error.status
            diagnostic_error = error
        elif isinstance(error, ProviderResponseError):
            status = _DabbleFailureStatus.MALFORMED
            diagnostic_error = error
        elif isinstance(error, DeadlineExceededError):
            status = _DabbleFailureStatus.DEADLINE_EXCEEDED
            diagnostic_error = error
        elif isinstance(error, requests.exceptions.Timeout):
            status = _DabbleFailureStatus.TIMEOUT
            diagnostic_error = error
        else:
            status = _DabbleFailureStatus.UPSTREAM_ERROR
            diagnostic_error = error
        return ProviderUnavailableError(
            "Dabble snapshot is currently unavailable.",
            detail=f"{status.value}: {type(diagnostic_error).__name__}",
        )

    def _now_utc(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Dabble clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _optional_id(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text

    @staticmethod
    def _decimal_source(value: Any, field: str) -> tuple[Decimal, str]:
        if isinstance(value, bool) or value is None:
            raise CoverageRecordMalformed(f"Dabble {field} must be a finite decimal")
        if isinstance(value, float) and not math.isfinite(value):
            raise CoverageRecordMalformed(f"Dabble {field} must be a finite decimal")
        displayed = str(value).strip()
        try:
            decimal = Decimal(displayed)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise CoverageRecordMalformed(
                f"Dabble {field} must be a finite decimal"
            ) from error
        if not decimal.is_finite():
            raise CoverageRecordMalformed(f"Dabble {field} must be a finite decimal")
        return decimal, displayed


__all__ = [
    "DabbleAdapter",
    "canonical_stat_components",
]
