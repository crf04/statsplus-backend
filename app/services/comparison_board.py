"""Build factual Comparison Groups from resolved Provider Snapshots.

The collector in :mod:`app.services.dfs_board` owns retrieval, statistic
resolution, and governed athlete/event mapping.  This service is the seam above
it: it turns one board read into deterministic Comparison Groups, explicit
Comparison Availability, and visible unresolved evidence.

What it states is exactly what the providers and the catalogs already say.  A
group exists only when its members share one Canonical Event, one Canonical
Athlete, one Canonical Statistic, and one scoring period; its summary is exact
decimal arithmetic over published thresholds.  No probability, expected value,
recommendation, average, preferred market, entry payout, or cross-provider
fantasy assumption is derived here, and nothing is ever truncated: a read that
would exceed the configured ceiling is refused with the count it observed and
the filters that would narrow it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from app.domain.comparisons import (
    SUPPORTED_NARROWING_FILTERS,
    CatalogAvailability,
    CatalogAvailabilityReason,
    ComparisonAvailability,
    ComparisonBoard,
    ComparisonExclusion,
    ComparisonFilters,
    ComparisonGroup,
    ComparisonKey,
    ComparisonMember,
    ComparisonSummary,
    MarketFreshness,
    ProviderReport,
    UnresolvedMarket,
    exact_seconds,
    market_reference,
    selection_reference,
)
from app.domain.statistics import MatchState, ScoringPeriod
from app.errors import AppError
from app.providers.dfs import (
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
)

#: Post-filter market ceiling.  A board larger than this is refused rather than
#: truncated, because a silently shortened board reads as a complete one.
DEFAULT_MAX_COMPARISON_MARKETS = 10000

#: Windows used when no runtime settings are injected.  They mirror the
#: reviewed provider cache defaults, so a market is fresh for five minutes and
#: may still enter a comparison as explicitly stale for thirty.
DEFAULT_FRESH_SECONDS = 300.0
DEFAULT_STALE_SECONDS = 1800.0

ATHLETE_CATALOG = "athlete_catalog"
EVENT_CATALOG = "event_catalog"


class ComparisonBoardTooLargeError(AppError):
    """The post-filter board exceeds the configured market ceiling."""

    status_code = 400
    code = "board_too_large"
    default_message = (
        "The requested board is too large. Narrow it with the supported filters."
    )

    def __init__(
        self,
        *,
        observed_market_count: int,
        market_limit: int,
        supported_filters: tuple[str, ...] = SUPPORTED_NARROWING_FILTERS,
        message: str | None = None,
    ) -> None:
        self.observed_market_count = observed_market_count
        self.market_limit = market_limit
        self.supported_filters = tuple(supported_filters)
        super().__init__(message)


def _catalog_availability(
    catalog: Any,
    *,
    name: str,
    season: str | None,
    now: datetime,
    fresh_key: str,
) -> CatalogAvailability:
    """Read one canonical catalog's identity, age, and usability.

    Missing data, an over-age refresh, and an unconfigured catalog are all
    unusable for comparison identity, and none of them is a provider defect:
    the normalized markets stay visible while comparisons wait for a refresh.
    """

    if season is None:
        return CatalogAvailability(
            catalog=name,
            season=None,
            available=False,
            reason=CatalogAvailabilityReason.NOT_CONFIGURED,
        )
    if catalog is None or not callable(getattr(catalog, "get_freshness", None)):
        return CatalogAvailability(
            catalog=name,
            season=season,
            available=False,
            reason=CatalogAvailabilityReason.NOT_CONFIGURED,
        )
    freshness = catalog.get_freshness(season)
    if not isinstance(freshness, Mapping):
        return CatalogAvailability(
            catalog=name,
            season=season,
            available=False,
            reason=CatalogAvailabilityReason.MISSING,
        )
    last_success = _timestamp(freshness.get("last_success_at"))
    max_age = _catalog_max_age_seconds(freshness)
    if last_success is None:
        return CatalogAvailability(
            catalog=name,
            season=season,
            available=False,
            reason=CatalogAvailabilityReason.MISSING,
            max_age_seconds=max_age,
        )
    age = exact_seconds(now - last_success)
    if not bool(freshness.get(fresh_key)):
        return CatalogAvailability(
            catalog=name,
            season=season,
            available=False,
            reason=CatalogAvailabilityReason.STALE,
            last_success_at=last_success,
            age_seconds=age,
            max_age_seconds=max_age,
        )
    return CatalogAvailability(
        catalog=name,
        season=season,
        available=True,
        last_success_at=last_success,
        age_seconds=age,
        max_age_seconds=max_age,
    )


def _catalog_max_age_seconds(freshness: Mapping[str, Any]) -> Decimal | None:
    """The configured maximum age one catalog reports, in exact seconds."""

    hours = freshness.get("max_age_hours")
    if hours is not None:
        return Decimal(str(hours)) * Decimal(3600)
    days = freshness.get("freshness_days")
    if days is not None:
        return Decimal(str(days)) * Decimal(86400)
    return None


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _athlete_identities(board: Any) -> dict[tuple[str, str], tuple[int | None, str]]:
    """Each provider athlete identity's governed canonical athlete and state."""

    identities: dict[tuple[str, str], tuple[int | None, str]] = {}
    for outcome in getattr(board, "mapping_outcomes", ()):
        resolution = outcome.resolution
        provider_athlete_id = resolution.provider_athlete_id
        if not provider_athlete_id:
            continue
        identities[(resolution.provider, provider_athlete_id)] = (
            outcome.canonical_player_id,
            outcome.state.value,
        )
    return identities


def _event_identities(
    board: Any,
) -> tuple[dict[tuple[str, str], tuple[str | None, str]], dict[Any, tuple[str | None, str]]]:
    """Governed canonical games, keyed by identity and by reported evidence.

    A provider that publishes no stable event ID still resolves a game for the
    current board, and that observation is keyed by nothing durable.  Its own
    typed evidence is what names it, so it is indexed by exactly the evidence
    the market carried.
    """

    by_identity: dict[tuple[str, str], tuple[str | None, str]] = {}
    by_evidence: dict[Any, tuple[str | None, str]] = {}
    for outcome in getattr(board, "event_mapping_outcomes", ()):
        resolution = outcome.resolution
        governed = (outcome.canonical_event_id, outcome.state.value)
        provider_event_id = resolution.provider_event_id
        if provider_event_id:
            by_identity[(resolution.provider, provider_event_id)] = governed
        else:
            by_evidence[(resolution.provider, resolution.provider_evidence)] = governed
    return by_identity, by_evidence


class ComparisonBoardService:
    """Turn one collected board into deterministic, factual comparisons."""

    def __init__(
        self,
        board_service: Any,
        *,
        athlete_catalog: Any | None = None,
        event_catalog: Any | None = None,
        settings: Any | None = None,
        max_markets: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(board_service, "get_board", None)):
            raise TypeError("comparison board service requires a board collector")
        self.board_service = board_service
        self.athlete_catalog = athlete_catalog
        self.event_catalog = event_catalog
        self.settings = settings
        limit = max_markets
        if limit is None:
            limit = getattr(
                getattr(settings, "providers", None),
                "dfs_comparison_max_markets",
                DEFAULT_MAX_COMPARISON_MARKETS,
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("comparison market ceiling must be a positive integer")
        self.max_markets = limit
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- public seam -------------------------------------------------------

    def get_comparisons(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext | None = None,
        *,
        filters: ComparisonFilters | None = None,
    ) -> ComparisonBoard:
        """Read one comparison board, narrowed centrally by ``filters``."""

        if not isinstance(query, NBAMarketQuery):
            raise TypeError("query must be NBAMarketQuery")
        if filters is None:
            filters = ComparisonFilters()
        elif not isinstance(filters, ComparisonFilters):
            raise TypeError("filters must be ComparisonFilters")

        # A provider filter is answered before any retrieval, so an excluded
        # provider is never called at all.
        board = self.board_service.get_board(
            query, context, providers=(filters.providers or None)
        )
        generated_at = board.generated_at
        availability = self._availability(query.season, generated_at)
        athletes = _athlete_identities(board)
        events_by_identity, events_by_evidence = _event_identities(board)

        members: dict[ComparisonKey, list[ComparisonMember]] = {}
        unresolved: list[UnresolvedMarket] = []
        observed = 0
        for snapshot in board.snapshots:
            if filters.providers and snapshot.provider not in filters.providers:
                continue
            freshness = self._snapshot_freshness(snapshot, generated_at)
            for market in snapshot.markets:
                if filters.market_statuses and (
                    MarketStatus(market.status) not in filters.market_statuses
                ):
                    continue
                key, exclusion, detail = self._classify(
                    market,
                    availability=availability,
                    freshness=freshness,
                    athletes=athletes,
                    events_by_identity=events_by_identity,
                    events_by_evidence=events_by_evidence,
                )
                if not self._passes_canonical_filters(key, filters):
                    continue
                observed += 1
                reference = market_reference(market)
                if key is None:
                    unresolved.append(
                        UnresolvedMarket(
                            market_reference=reference,
                            provider=market.provider,
                            reason=exclusion,
                            detail=detail,
                        )
                    )
                    continue
                members.setdefault(key, []).append(
                    self._member(market, reference, freshness, snapshot.retrieved_at)
                )

        # The whole read is classified before the ceiling is applied, so the
        # count reported back is what the caller's filters actually observed
        # rather than the point a truncating reader would have stopped at.
        if observed > self.max_markets:
            raise ComparisonBoardTooLargeError(
                observed_market_count=observed,
                market_limit=self.max_markets,
            )

        groups = tuple(
            _group(key, members[key])
            for key in sorted(members, key=lambda value: value.order)
        )
        return ComparisonBoard(
            season=query.season,
            generated_at=generated_at,
            availability=availability,
            groups=groups,
            unresolved=tuple(sorted(unresolved, key=lambda entry: entry.order)),
            provider_reports=self._provider_reports(board, generated_at, filters),
            disabled_providers=board.disabled_providers,
            filters=filters,
            market_count=observed,
        )

    # -- availability ------------------------------------------------------

    def _availability(
        self, season: str | None, generated_at: datetime
    ) -> ComparisonAvailability:
        """Whether both canonical catalogs can support comparison identity."""

        catalogs = (
            _catalog_availability(
                self.athlete_catalog,
                name=ATHLETE_CATALOG,
                season=season,
                now=generated_at,
                fresh_key="is_fresh",
            ),
            _catalog_availability(
                self.event_catalog,
                name=EVENT_CATALOG,
                season=season,
                now=generated_at,
                fresh_key="fresh",
            ),
        )
        return ComparisonAvailability(
            available=all(entry.available for entry in catalogs),
            catalogs=catalogs,
        )

    # -- classification ----------------------------------------------------

    def _classify(
        self,
        market: PlayerProjectionMarket,
        *,
        availability: ComparisonAvailability,
        freshness: MarketFreshness | None,
        athletes: Mapping[tuple[str, str], tuple[int | None, str]],
        events_by_identity: Mapping[tuple[str, str], tuple[str | None, str]],
        events_by_evidence: Mapping[Any, tuple[str | None, str]],
    ) -> tuple[ComparisonKey | None, ComparisonExclusion | None, str | None]:
        """The canonical identity one market may be compared under, or why not.

        The checks run in a fixed order -- availability, freshness, statistic,
        athlete, event, threshold -- so a market that fails several of them
        always reports the same reason.
        """

        if not availability.available:
            unavailable = availability.unavailable_catalogs
            return (
                None,
                ComparisonExclusion.COMPARISON_UNAVAILABLE,
                unavailable[0].reason.value if unavailable else None,
            )
        if freshness is None:
            return None, ComparisonExclusion.STALE_SNAPSHOT, None

        match = market.statistic_match
        if match is None or match.state is MatchState.UNMAPPED:
            return (
                None,
                ComparisonExclusion.UNMAPPED_STATISTIC,
                None if match is None or match.reason is None else match.reason.value,
            )
        if not match.is_comparable:
            return None, ComparisonExclusion.NON_COMPARABLE_STATISTIC, match.canonical_id

        if market.athlete is None or not market.athlete.provider_id:
            return None, ComparisonExclusion.MISSING_ATHLETE_EVIDENCE, None
        athlete_id, athlete_state = athletes.get(
            (market.provider, market.athlete.provider_id), (None, "unmapped")
        )
        if athlete_id is None:
            return None, ComparisonExclusion.UNRESOLVED_ATHLETE, athlete_state

        if market.event is None:
            return None, ComparisonExclusion.MISSING_EVENT_EVIDENCE, None
        if market.event.provider_id:
            event_id, event_state = events_by_identity.get(
                (market.provider, market.event.provider_id), (None, "unmapped")
            )
        else:
            event_id, event_state = events_by_evidence.get(
                (market.provider, market.event), (None, "unmapped")
            )
        if event_id is None:
            return None, ComparisonExclusion.UNRESOLVED_EVENT, event_state

        if market.threshold is None:
            return None, ComparisonExclusion.MISSING_THRESHOLD, None

        return (
            ComparisonKey(
                canonical_event_id=event_id,
                canonical_athlete_id=athlete_id,
                canonical_statistic_id=match.canonical_id,
                scoring_period=ScoringPeriod(match.scoring_period),
            ),
            None,
            None,
        )

    @staticmethod
    def _passes_canonical_filters(
        key: ComparisonKey | None, filters: ComparisonFilters
    ) -> bool:
        """Whether a canonical filter admits this market.

        A canonical filter names an identity the board itself established, so a
        market with no such identity cannot satisfy one and leaves the board
        rather than appearing as evidence for an identity it never had.
        """

        if key is None:
            return not (
                filters.canonical_athlete_ids
                or filters.canonical_event_ids
                or filters.canonical_statistic_ids
            )
        if (
            filters.canonical_athlete_ids
            and key.canonical_athlete_id not in filters.canonical_athlete_ids
        ):
            return False
        if (
            filters.canonical_event_ids
            and key.canonical_event_id not in filters.canonical_event_ids
        ):
            return False
        return not (
            filters.canonical_statistic_ids
            and key.canonical_statistic_id not in filters.canonical_statistic_ids
        )

    @staticmethod
    def _member(
        market: PlayerProjectionMarket,
        reference: str,
        freshness: MarketFreshness,
        retrieved_at: datetime,
    ) -> ComparisonMember:
        return ComparisonMember(
            market_reference=reference,
            provider=market.provider,
            threshold=market.threshold.value,
            threshold_unit=market.threshold.unit,
            variant=MarketVariant(market.variant),
            status=MarketStatus(market.status),
            retrieved_at=retrieved_at,
            freshness=freshness,
            selection_references=tuple(
                selection_reference(reference, selection)
                for selection in market.selections
            ),
        )

    # -- freshness and reporting -------------------------------------------

    def _snapshot_freshness(
        self, snapshot: ProviderSnapshot, generated_at: datetime
    ) -> MarketFreshness | None:
        """Whether this observation may enter comparisons, and how fresh it is.

        A snapshot inside its provider's fresh window is contemporaneous.  One
        past it may still be compared while it is inside the permitted stale
        window, and says so explicitly.  Beyond that window it stays visible on
        the board but enters no group.
        """

        age = (generated_at - snapshot.retrieved_at).total_seconds()
        if age <= self._window("dfs_cache_fresh_seconds_for", snapshot.provider, DEFAULT_FRESH_SECONDS):
            return MarketFreshness.FRESH
        if age <= self._window(
            "dfs_cache_stale_if_error_seconds_for", snapshot.provider, DEFAULT_STALE_SECONDS
        ):
            return MarketFreshness.STALE
        return None

    def _window(self, accessor: str, provider: str, default: float) -> float:
        providers = getattr(self.settings, "providers", None)
        reader = getattr(providers, accessor, None)
        if not callable(reader):
            return default
        return float(reader(provider))

    def _provider_reports(
        self, board: Any, generated_at: datetime, filters: ComparisonFilters
    ) -> tuple[ProviderReport, ...]:
        reports: list[ProviderReport] = []
        for outcome in board.provider_outcomes:
            if filters.providers and outcome.provider not in filters.providers:
                continue
            snapshot = outcome.snapshot
            coverage = outcome.coverage
            retrieved_at = None if snapshot is None else snapshot.retrieved_at
            reports.append(
                ProviderReport(
                    provider=outcome.provider,
                    status=outcome.status.value,
                    reason=None if outcome.reason is None else outcome.reason.value,
                    retrieved_at=retrieved_at,
                    age_seconds=(
                        None
                        if retrieved_at is None
                        else exact_seconds(generated_at - retrieved_at)
                    ),
                    freshness=(
                        None
                        if snapshot is None
                        else self._snapshot_freshness(snapshot, generated_at)
                    ),
                    market_count=0 if snapshot is None else len(snapshot.markets),
                    warning_codes=(
                        ()
                        if coverage is None
                        else tuple(
                            code.value
                            for code in (
                                *coverage.warning_codes,
                                *coverage.skipped_reasons,
                            )
                        )
                    ),
                )
            )
        return tuple(sorted(reports, key=lambda report: report.provider))

def _group(key: ComparisonKey, members: Iterable[ComparisonMember]) -> ComparisonGroup:
    ordered = _ordered_members(members)
    return ComparisonGroup(key=key, members=ordered, summary=ComparisonSummary.of(ordered))


def _ordered_members(members: Iterable[ComparisonMember]) -> tuple[ComparisonMember, ...]:
    """Deterministically ordered members with exact repeats collapsed.

    Two members are the same offering only when every fact about them agrees,
    so legitimate multiple thresholds, variants, statuses, and same-provider
    markets all stay distinct members of one group.
    """

    unique = tuple(dict.fromkeys(members))
    return tuple(sorted(unique, key=lambda member: member.order))


__all__ = [
    "DEFAULT_MAX_COMPARISON_MARKETS",
    "ComparisonBoardService",
    "ComparisonBoardTooLargeError",
]
