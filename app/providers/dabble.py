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
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
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
from app.providers.dfs_transport import request_json, run_bounded
from app.utils.telemetry import (
    PROVIDER_DABBLE,
    ProviderResponseError,
    increment_retry_count,
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


@dataclass(frozen=True, slots=True)
class _ParsedRows:
    rows: tuple[dict[str, Any], ...]
    skipped_count: int = 0


@dataclass(frozen=True, slots=True)
class _DetailResult:
    fixture: dict[str, Any]
    competition: dict[str, Any]
    detail: dict[str, Any] | None = None
    error: Exception | None = None
    malformed: bool = False


@dataclass(frozen=True, slots=True)
class _DabbleDiscovery:
    """Immutable discovery facts handed to the bounded detail pipeline."""

    fixtures: tuple[tuple[dict[str, Any], dict[str, Any]], ...]
    skipped_count: int = 0
    warning_codes: tuple[CoverageCode, ...] = ()
    skipped_reasons: tuple[CoverageCode, ...] = ()
    diagnostic_details: tuple[str, ...] = ()
    fanout_complete: bool = True
    malformed_seen: bool = False
    fixture_fetch_failed: bool = False


@dataclass(frozen=True, slots=True)
class _DabbleNormalization:
    """One isolated, fully normalized fixture-detail observation."""

    batch: _NormalizedBatch
    skipped_count: int = 0
    warning_codes: tuple[CoverageCode, ...] = ()
    skipped_reasons: tuple[CoverageCode, ...] = ()
    diagnostic_details: tuple[str, ...] = ()
    fanout_complete: bool = True
    malformed_seen: bool = False
    deadline_error: bool = False


class _DabbleRequestFailure(Exception):
    """Expected failure while fetching one Dabble resource."""

    def __init__(self, reason: str, detail: Any = None) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


@dataclass
class _MarketAccumulator:
    market: PlayerProjectionMarket
    selections: dict[tuple[Any, ...], Selection]
    conflict: bool = False


class DabbleAdapter:
    """Retrieve Dabble's eligible NBA player projection markets."""

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
        detail_concurrency: int = DETAIL_CONCURRENCY,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = READ_TIMEOUT_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if detail_concurrency < 1:
            raise ValueError("detail_concurrency must be at least 1")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("provider timeouts must be positive")
        self.session = session or _build_session()
        self.detail_concurrency = detail_concurrency
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.read_timeout_seconds = float(read_timeout_seconds)
        self.now = now or (lambda: datetime.now(timezone.utc))

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
            if discovery.fixture_fetch_failed:
                raise self._unavailable(
                    _DabbleRequestFailure("upstream_error", "fixture list")
                )
            if discovery.malformed_seen:
                raise self._unavailable(
                    ProviderResponseError("Dabble returned no usable fixture data")
                )
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
        if context.is_expired(now=self._now_utc()):
            raise self._unavailable(_DabbleRequestFailure("deadline_exceeded"))
        try:
            normalization = run_bounded(
                context=context,
                now=self._now_utc,
                call=lambda: self._normalize_detail_results(detail_results, query),
            )
        except DeadlineExceededError as error:
            raise self._unavailable(
                _DabbleRequestFailure("deadline_exceeded", error)
            ) from error
        if normalization.deadline_error or context.is_expired(now=self._now_utc()):
            raise self._unavailable(_DabbleRequestFailure("deadline_exceeded"))

        batch = normalization.batch
        warnings = (*discovery.warning_codes, *normalization.warning_codes, *batch.warning_codes)
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
        malformed_seen = (
            discovery.malformed_seen
            or normalization.malformed_seen
            or batch.malformed_count > 0
        )
        fanout_complete = (
            discovery.fanout_complete
            and normalization.fanout_complete
            and not malformed_seen
        )

        if not batch.markets:
            if malformed_seen or not normalization.fanout_complete:
                raise self._unavailable(
                    ProviderResponseError("Dabble produced no usable markets")
                )
            return _build_snapshot(
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

        return _build_snapshot(
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

    def _discover_fixtures(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> _DabbleDiscovery:
        """Discover eligible fixtures and own discovery coverage decisions."""

        warning_codes: list[CoverageCode] = []
        skipped_reasons: list[CoverageCode] = []
        skipped_count = 0
        malformed_seen = False
        fanout_complete = True
        fixture_fetch_failed = False

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
            malformed_seen = True
        if not competitions and competitions_result.skipped_count:
            raise self._unavailable(
                ProviderResponseError("Dabble competition records were malformed")
            )

        nba_competitions: list[dict[str, Any]] = []
        for competition in competitions:
            if not self._is_nba_competition(competition, query):
                skip(CoverageCode.NON_NBA_COMPETITION)
                continue
            if not competition.get("id"):
                skip(CoverageCode.MISSING_COMPETITION_ID, warning=True)
                malformed_seen = True
                continue
            nba_competitions.append(competition)

        fixtures: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for competition in nba_competitions:
            if context.is_expired(now=self._now_utc()):
                raise self._unavailable(_DabbleRequestFailure("deadline_exceeded"))
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
                if getattr(error, "reason", None) == "deadline_exceeded":
                    raise self._unavailable(error) from error
                fixture_fetch_failed = True
                fanout_complete = False
                warn(CoverageCode.FIXTURE_LIST_FAILED)
                skip(CoverageCode.FIXTURE_LIST_FAILED)
                logger.warning("Dabble fixture list failed: %s", type(error).__name__)
                continue
            if fixtures_result.skipped_count:
                warn(CoverageCode.MALFORMED_RECORD)
                for _ in range(fixtures_result.skipped_count):
                    skip(CoverageCode.MALFORMED_RECORD)
                malformed_seen = True
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
            raise self._unavailable(_DabbleRequestFailure("deadline_exceeded"))
        return _DabbleDiscovery(
            fixtures=tuple(fixtures),
            skipped_count=skipped_count,
            warning_codes=tuple(warning_codes),
            skipped_reasons=tuple(skipped_reasons),
            fanout_complete=fanout_complete,
            malformed_seen=malformed_seen,
            fixture_fetch_failed=fixture_fetch_failed,
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
        malformed_seen = False
        deadline_error = False

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
            if result.error is not None:
                if getattr(result.error, "reason", None) == "deadline_exceeded":
                    deadline_error = True
                fanout_complete = False
                if result.malformed:
                    malformed_seen = True
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
                malformed_seen = True
                warning_codes.append(CoverageCode.FIXTURE_MALFORMED)
                skipped_reasons.append(CoverageCode.FIXTURE_MALFORMED)
                skipped_count += 1
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
                malformed_seen = True
                fanout_complete = False

        batch = _NormalizedBatch.from_accumulator(
            record_coverage,
            markets=self._assemble_markets(merged_markets),
            warning_codes=warning_codes,
        )
        return _DabbleNormalization(
            batch=batch,
            skipped_count=skipped_count,
            warning_codes=tuple(warning_codes),
            skipped_reasons=tuple(skipped_reasons),
            diagnostic_details=tuple(diagnostic_details),
            fanout_complete=fanout_complete,
            malformed_seen=malformed_seen,
            deadline_error=deadline_error,
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
                        results[index] = _DetailResult(
                            fixture=fixture,
                            competition=competition,
                            error=error,
                            malformed=True,
                        )
                    except _DabbleRequestFailure as error:
                        results[index] = _DetailResult(
                            fixture=fixture,
                            competition=competition,
                            error=error,
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
                    error=_DabbleRequestFailure("deadline_exceeded"),
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
            raise ProviderResponseError("Dabble fixture detail id conflicts with fixture")
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
        if not isinstance(prop, Mapping):
            raise CoverageRecordMalformed("Dabble player prop must be an object")

        kind = prop.get("marketType", prop.get("market_type", prop.get("type")))
        if isinstance(kind, str) and kind.strip().casefold() in _NON_PLAYER_KINDS:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
        player_name = optional_text(prop.get("playerName"))
        if player_name is None:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)

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
        status = normalized_statuses[0]

        source_sport = (
            detail.get("sportName")
            or fixture.get("sportName")
            or competition.get("sport")
        )
        if source_sport is not None and str(source_sport).strip().casefold() not in _BASKETBALL_LABELS:
            raise CoverageRecordExcluded(CoverageCode.NON_NBA_SPORT)

        stats = prop.get("stats")
        if not isinstance(stats, list) or not stats:
            raise CoverageRecordMalformed("Dabble player prop stats are malformed")
        raw_stats = []
        for stat in stats:
            if not isinstance(stat, str) or not stat.strip():
                raise CoverageRecordMalformed("Dabble player prop stat is malformed")
            raw_stats.append(stat.strip())
        normalized_stats = canonical_stat_components(raw_stats)
        if not normalized_stats:
            raise CoverageRecordMalformed("Dabble player prop stats are empty")

        value, source_value = self._decimal_source(prop.get("value"), "value")
        line_type = prop.get("lineType")
        if not isinstance(line_type, str) or not line_type.strip():
            raise CoverageRecordMalformed("Dabble lineType is malformed")
        line_type = line_type.strip()

        provider_athlete_id = self._optional_id(prop.get("playerId"))

        fixture_id = str(detail.get("id") or fixture.get("id"))
        market_id = self._optional_id(prop.get("marketId"))
        selection_id = self._optional_id(prop.get("selectionId"))
        competition_id = self._optional_id(
            detail.get("competitionId")
            or fixture.get("competitionId")
            or competition.get("id")
        )
        sport_id = self._optional_id(
            detail.get("sportId")
            or fixture.get("sportId")
            or competition.get("sport_id")
        )
        sport_label = optional_text(
            detail.get("sportName")
            or fixture.get("sportName")
            or competition.get("sport")
        )
        competition_label = optional_text(
            detail.get("competitionName")
            or fixture.get("competitionName")
            or competition.get("name")
        )
        event_label = optional_text(
            detail.get("name") or fixture.get("name")
        )
        starts_at = detail.get("advertisedStart") or fixture.get("advertisedStart")
        updated_at = detail.get("updatedAt") or detail.get("updated_at")
        statistic_label = "+".join(raw_stats)
        components, scoring_period, period_label = self._statistic_period(normalized_stats)
        variant_label = optional_text(
            prop.get("variant") or prop.get("marketVariant")
        )
        try:
            team = TeamEvidence(
                provider_id=self._optional_id(prop.get("teamId")),
                name=optional_text(prop.get("teamName")),
                abbreviation=optional_text(prop.get("teamAbbreviation")),
            )
            athlete = AthleteEvidence(
                provider_id=provider_athlete_id,
                name=player_name,
                team=team,
            )
            sport = SportEvidence(provider_id=sport_id, label=sport_label)
            competition_evidence = CompetitionEvidence(
                provider_id=competition_id,
                label=competition_label,
                sport=sport,
            )
            market = PlayerProjectionMarket(
                provider=self.PROVIDER_ID,
                market_id=market_id,
                athlete=athlete,
                event=EventEvidence(
                    provider_id=fixture_id,
                    label=event_label,
                    starts_at=starts_at,
                    updated_at=updated_at,
                ),
                team=team,
                league=LeagueEvidence(
                    provider_id=competition_id,
                    label=competition_label,
                ),
                competition=competition_evidence,
                sport=sport,
                statistic=StatisticEvidence(
                    label=statistic_label,
                    components=tuple(components),
                ),
                threshold=MarketThreshold(
                    value=value,
                    unit="count",
                    original_value=source_value,
                ),
                status=status.value,
                status_label=(
                    str(status_label).strip() if status_label is not None else None
                ),
                variant=variant_label,
                variant_label=variant_label,
                scoring_period=scoring_period,
                scoring_period_label=period_label,
                starts_at=starts_at,
                updated_at=updated_at,
            )

            modifiers: tuple[SelectionModifier, ...] = ()
            if "multiplier" in prop and prop.get("multiplier") is not None:
                multiplier, _ = self._decimal_source(
                    prop.get("multiplier"), "multiplier"
                )
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
            selection = Selection(
                selection_id=selection_id,
                label=line_type,
                direction=line_type,
                direction_label=line_type,
                status=(
                    str(status_label).strip() if status_label is not None else None
                ),
                modifiers=modifiers,
            )
        except CoverageRecordMalformed:
            raise
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error
        if market_id is not None:
            market_key: tuple[Any, ...] = ("market", market_id)
        else:
            market_key = (
                "evidence",
                fixture_id,
                athlete.provider_id,
                athlete.name,
                team.provider_id,
                team.name,
                tuple(components),
                value,
                status.value,
                market.scoring_period,
                market.scoring_period_label,
                market.variant,
                market.variant_label,
            )
        return market_key, market, selection

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
            session=self.session,
            url=f"{self.BASE_URL}{path}",
            params=params,
            timeout=(self.connect_timeout_seconds, self.read_timeout_seconds),
            now=self._now_utc,
            provider=PROVIDER_DABBLE,
            operation=operation,
            parse=parser,
            deadline_message="Dabble retrieval deadline exceeded.",
            timeout_message="Dabble timed out while fetching lines.",
            unavailable_message="Dabble could not be reached.",
            invalid_json_message="Dabble returned invalid JSON",
            failure_factory=self._request_failure,
        )

    @staticmethod
    def _request_failure(reason: str, error: Exception) -> Exception:
        if reason == "deadline_exceeded":
            return _DabbleRequestFailure("deadline_exceeded", error)
        if reason == "timeout":
            return _DabbleRequestFailure("timeout", error)
        if reason == "http_error":
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            dabble_reason = (
                "rate_limited"
                if status_code == 429
                else "access_denied"
                if status_code in {401, 403}
                else "upstream_error"
            )
            return _DabbleRequestFailure(dabble_reason, error)
        return _DabbleRequestFailure("upstream_error", error)

    def _unavailable(self, error: Exception) -> ProviderUnavailableError:
        reason = getattr(error, "reason", "malformed_response")
        return ProviderUnavailableError(
            "Dabble snapshot is currently unavailable.",
            detail=f"{reason}: {type(error).__name__}",
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
