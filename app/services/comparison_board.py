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

Readability comes first.  A read no provider could be read from is an outage
rather than an over-large board, however many markets it observed, so the
ceiling is applied only to a read something on it could still be published
from.  The unreadable one builds no board at all: it raises
:class:`UnreadableComparisonBoardError`, carrying bounded evidence for the
response seam to report, and both seams judge readability through the one
domain authority in :func:`app.domain.comparisons.has_readable_provider`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from app.domain.freshness import (
    time_window_seconds,
    within_fresh_window,
    within_max_age,
)
from app.domain.comparisons import (
    SUPPORTED_NARROWING_FILTERS,
    BoardAppearance,
    BoardAthlete,
    BoardCacheState,
    BoardCompetition,
    BoardCoverage,
    BoardEvent,
    BoardMarket,
    BoardNamedEvidence,
    BoardObservation,
    BoardReadEvidence,
    BoardSelection,
    BoardStatistic,
    BoardStatisticResolution,
    BoardTeam,
    BoardThreshold,
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
    canonical_selections,
    exact_scaled_seconds,
    exact_seconds,
    market_content_key,
    market_evidence_key,
    has_readable_provider,
    market_reference,
    observation_evidence_key,
    selection_reference,
)
from app.domain.statistics import MatchState, ScoringPeriod
from app.errors import AppError, ProviderUnavailableError
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
DEFAULT_FRESH_SECONDS = Decimal(300)
DEFAULT_STALE_SECONDS = Decimal(1800)

ATHLETE_CATALOG = "athlete_catalog"
EVENT_CATALOG = "event_catalog"


class ComparisonBoardTooLargeError(AppError):
    """The post-filter board exceeds the configured market ceiling.

    The refusal carries two separate things.  ``public_details`` is the bounded
    contract a caller acts on -- what the read observed and what would narrow
    it.  ``board_evidence`` is the completed retrieval and classification the
    read had already finished when the ceiling stopped it, kept so an operator
    observing the refusal sees the same providers, freshness, cache states, and
    availability a published board would have shown, rather than a board that
    looks like it never happened.  It is not published to a caller.
    """

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
        board_evidence: BoardReadEvidence | None = None,
        message: str | None = None,
    ) -> None:
        self.observed_market_count = observed_market_count
        self.market_limit = market_limit
        self.supported_filters = tuple(supported_filters)
        self.board_evidence = board_evidence
        super().__init__(message)

    @property
    def public_details(self) -> dict[str, Any]:
        """What the read observed and what would make it smaller.

        A refusal is only actionable if the caller learns both, so the count is
        the whole post-filter board rather than the point a truncating reader
        would have stopped at.
        """

        return {
            "observed_market_count": self.observed_market_count,
            "market_limit": self.market_limit,
            "supported_filters": list(self.supported_filters),
        }


class UnreadableComparisonBoardError(ProviderUnavailableError):
    """No provider on this read could be read from, so no board was built.

    This is the internal result of a read that finished retrieval and found
    nothing publishable in it.  It carries the completed read's
    :class:`BoardReadEvidence` and deliberately no board: there is no board to
    carry, and a caller learns about the outage from the response seam, which
    catches this and states the bounded Provider Outcome vocabulary the public
    503 contract documents.

    It is a :class:`~app.errors.ProviderUnavailableError` rather than a sibling
    of :class:`ComparisonBoardTooLargeError` so that the central error handler
    already answers it as the same safe 503 an outage is, without publishing
    any evidence, should it ever escape the response seam.  Nothing about it is
    a too-large refusal: an outage cannot be narrowed by a filter.
    """

    def __init__(
        self,
        *,
        board_evidence: BoardReadEvidence,
        message: str | None = None,
    ) -> None:
        if not isinstance(board_evidence, BoardReadEvidence):
            raise TypeError("an unreadable board read states typed board evidence")
        self.board_evidence = board_evidence
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
    freshness = catalog.get_freshness(season, now=now)
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
    age = max(exact_seconds(now - last_success), Decimal(0))
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
    """The configured maximum age one catalog reports, in exact seconds.

    A canonical catalog states the exact duration it gated on, counted from
    that duration's own whole microseconds, and that is read first: a TTL
    rewritten into floating-point hours and multiplied back is not the number
    the catalog compared an age against.  The unit spellings remain readable
    for a freshness document that states no exact seconds of its own.
    """

    seconds = freshness.get("max_age_seconds")
    if seconds is not None:
        return exact_scaled_seconds(
            seconds, unit_seconds=1, field="catalog max_age_seconds"
        )
    hours = freshness.get("max_age_hours")
    if hours is not None:
        return exact_scaled_seconds(
            hours, unit_seconds=3600, field="catalog max_age_hours"
        )
    days = freshness.get("freshness_days")
    if days is not None:
        return exact_scaled_seconds(
            days, unit_seconds=86400, field="catalog freshness_days"
        )
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

        # One timezone-aware observation, taken after the collector returned.
        # Every age, freshness window, and catalog TTL on this board is measured
        # against exactly this instant, so a slow collection cannot report a
        # market as fresher than the board that states it.
        observed_at = self._observed_at()
        availability = self._availability(query.season, observed_at)
        athletes = _athlete_identities(board)
        events_by_identity, events_by_evidence = _event_identities(board)

        retained = self._retained_markets(board, filters, observed_at)

        members: dict[ComparisonKey, list[ComparisonMember]] = {}
        unresolved: list[UnresolvedMarket] = []
        markets: list[BoardMarket] = []
        observed = 0
        for reference, observations in retained.items():
            # Several disagreeing observations of one reference are all kept,
            # in their own content-derived order, so nothing is lost and the
            # board does not depend on which of them arrived first.
            conflict_count = len(observations) if len(observations) > 1 else None
            for ordinal, (market, observation) in enumerate(observations):
                if conflict_count is not None:
                    key: ComparisonKey | None = None
                    exclusion: ComparisonExclusion | None = (
                        ComparisonExclusion.CONFLICTING_MARKET_IDENTITY
                    )
                    detail: str | None = "conflicting_normalized_content"
                elif observation.is_future:
                    key, exclusion, detail = (
                        None,
                        ComparisonExclusion.FUTURE_SNAPSHOT,
                        None,
                    )
                else:
                    key, exclusion, detail = self._classify(
                        market,
                        availability=availability,
                        freshness=observation.freshness,
                        athletes=athletes,
                        events_by_identity=events_by_identity,
                        events_by_evidence=events_by_evidence,
                    )
                if not self._passes_canonical_filters(key, filters):
                    continue
                observed += 1
                markets.append(
                    self._board_market(
                        market,
                        reference,
                        observation,
                        key,
                        exclusion,
                        detail,
                        conflict_ordinal=None if conflict_count is None else ordinal,
                        conflict_count=conflict_count,
                    )
                )
                if key is None:
                    # One reference is unresolved once, however many
                    # observations contested it; each of them stays readable in
                    # ``markets``.
                    if conflict_count is None or ordinal == 0:
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
                    self._member(market, reference, observation)
                )

        # The provider reports are derived before the ceiling is applied
        # because they describe the retrieval, which is complete either way: a
        # refused read is still a read whose providers answered.
        reports = self._provider_reports(board, observed_at, filters)

        # The whole read is classified before the ceiling is applied, so the
        # count reported back is what the caller's filters actually observed
        # rather than the point a truncating reader would have stopped at.
        #
        # Readability outranks size.  A read no provider could be read from
        # states nothing at any ceiling, so there is no board for a ceiling to
        # be exceeded by, and refusing it as too large would tell a caller to
        # narrow filters that cannot make an outage readable.  Such a read
        # fails with its own type instead, carrying only the bounded evidence
        # the response seam needs to report the outage: every observation on it
        # is beyond the permitted maximum age or ahead of this board's clock,
        # so it holds no group and nothing publishable was dropped.  No board
        # is built for it, because a board that retained nothing it counted
        # would contradict itself and could still reach the serializer.
        if observed > self.max_markets:
            evidence = BoardReadEvidence(
                availability=availability,
                provider_reports=reports,
                disabled_providers=board.disabled_providers,
                group_count=len(members),
                market_count=observed,
                unresolved_count=len(unresolved),
            )
            if has_readable_provider(reports):
                raise ComparisonBoardTooLargeError(
                    observed_market_count=observed,
                    market_limit=self.max_markets,
                    board_evidence=evidence,
                )
            raise UnreadableComparisonBoardError(board_evidence=evidence)

        groups = tuple(
            _group(key, members[key])
            for key in sorted(members, key=lambda value: value.order)
        )
        return ComparisonBoard(
            season=query.season,
            generated_at=observed_at,
            availability=availability,
            groups=groups,
            unresolved=tuple(sorted(unresolved, key=lambda entry: entry.order)),
            markets=tuple(sorted(markets, key=lambda entry: entry.order)),
            provider_reports=reports,
            disabled_providers=board.disabled_providers,
            filters=filters,
            market_count=observed,
        )

    # -- observation -------------------------------------------------------

    def _observed_at(self) -> datetime:
        """The single instant this board is measured against."""

        observed_at = self.clock()
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise ValueError("the comparison board clock must return an aware datetime")
        return observed_at.astimezone(timezone.utc)

    def _retained_markets(
        self, board: Any, filters: ComparisonFilters, observed_at: datetime
    ) -> dict[str, list[tuple[PlayerProjectionMarket, BoardObservation]]]:
        """Every normalized observation this read keeps, by stable reference.

        A repeat collapses when it says the same thing -- in the same canonical
        semantics the reference itself is derived in, so a market relisted with
        its selections the other way round, or its threshold rewritten at
        another scale, is one offering rather than a market contradicting
        itself.  Where two such repeats differ only in a retained audit field,
        the one kept is chosen by content too, never by arrival order.

        A repeat that says something else is malformed rather than a second
        offering, but it is still evidence: every distinct contradicting
        observation is kept, ordered by its own complete retained content so the
        result cannot depend on the order the providers were read in, and none
        of them enters a group.
        """

        repeats: dict[
            str, dict[tuple[bytes, bytes], tuple[PlayerProjectionMarket, BoardObservation]]
        ] = {}
        for snapshot in board.snapshots:
            if filters.providers and snapshot.provider not in filters.providers:
                continue
            observation = self._observation(snapshot, observed_at)
            for market in snapshot.markets:
                if filters.market_statuses and (
                    MarketStatus(market.status) not in filters.market_statuses
                ):
                    continue
                entry = (market, observation)
                semantic = (
                    market_content_key(market),
                    observation_evidence_key(observation),
                )
                stated = repeats.setdefault(market_reference(market), {})
                previous = stated.get(semantic)
                if previous is None or _evidence_order(entry) < _evidence_order(previous):
                    stated[semantic] = entry
        return {
            reference: sorted(stated.values(), key=_evidence_order)
            for reference, stated in repeats.items()
        }

    def _observation(
        self, snapshot: ProviderSnapshot, observed_at: datetime
    ) -> BoardObservation:
        """One snapshot's age and freshness against the board's observation.

        A snapshot the provider timestamped after the board observed it cannot
        be aged, so it fails closed: it reports no negative age, carries no
        freshness, and its markets enter no comparison.
        """

        elapsed = observed_at - snapshot.retrieved_at
        is_future = elapsed.total_seconds() < 0
        age = Decimal(0) if is_future else exact_seconds(elapsed)
        return BoardObservation(
            provider=snapshot.provider,
            snapshot_status=getattr(snapshot.status, "value", str(snapshot.status)),
            retrieved_at=snapshot.retrieved_at,
            observed_at=observed_at,
            age_seconds=age,
            freshness=None if is_future else self._freshness(age, snapshot.provider),
            is_future=is_future,
        )

    # -- availability ------------------------------------------------------

    def _availability(
        self, season: str | None, observed_at: datetime
    ) -> ComparisonAvailability:
        """Whether both canonical catalogs can support comparison identity.

        Both catalogs are asked about the same instant the rest of the board is
        measured against, so a reported age and the availability derived from it
        can never disagree at a TTL boundary.
        """

        catalogs = (
            _catalog_availability(
                self.athlete_catalog,
                name=ATHLETE_CATALOG,
                season=season,
                now=observed_at,
                fresh_key="is_fresh",
            ),
            _catalog_availability(
                self.event_catalog,
                name=EVENT_CATALOG,
                season=season,
                now=observed_at,
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
        observation: BoardObservation,
    ) -> ComparisonMember:
        return ComparisonMember(
            market_reference=reference,
            provider=market.provider,
            threshold=market.threshold.value,
            threshold_unit=market.threshold.unit,
            variant=MarketVariant(market.variant),
            status=MarketStatus(market.status),
            retrieved_at=observation.retrieved_at,
            freshness=observation.freshness,
            selection_references=tuple(
                selection_reference(reference, selection)
                for selection in canonical_selections(market.selections)
            ),
        )

    @staticmethod
    def _board_market(
        market: PlayerProjectionMarket,
        reference: str,
        observation: BoardObservation,
        key: ComparisonKey | None,
        exclusion: ComparisonExclusion | None,
        detail: str | None,
        *,
        conflict_ordinal: int | None = None,
        conflict_count: int | None = None,
    ) -> BoardMarket:
        """Retain one normalized market whole, linked to where it went."""

        return BoardMarket(
            market_reference=reference,
            provider=market.provider,
            observation=observation,
            market_id=market.market_id,
            athlete=BoardAthlete.of(market.athlete),
            event=BoardEvent.of(market.event),
            team=BoardTeam.of(market.team),
            opponent=BoardTeam.of(market.opponent),
            league=BoardNamedEvidence.of(market.league),
            competition=BoardCompetition.of(market.competition),
            sport=BoardNamedEvidence.of(market.sport),
            statistic=BoardStatistic.of(market.statistic),
            statistic_resolution=BoardStatisticResolution.of(market.statistic_match),
            threshold=BoardThreshold.of(market.threshold),
            status=MarketStatus(market.status),
            status_label=market.status_label,
            variant=MarketVariant(market.variant),
            variant_label=market.variant_label,
            scoring_period=ScoringPeriod(market.scoring_period),
            scoring_period_label=market.scoring_period_label,
            starts_at=market.starts_at,
            updated_at=market.updated_at,
            appearance=BoardAppearance.of(market.appearance),
            selections=tuple(
                BoardSelection.of(selection_reference(reference, selection), selection)
                for selection in canonical_selections(market.selections)
            ),
            comparison_reference=None if key is None else key.reference,
            exclusion=exclusion,
            exclusion_detail=detail,
            conflict_ordinal=conflict_ordinal,
            conflict_count=conflict_count,
        )

    # -- freshness and reporting -------------------------------------------

    def _freshness(self, age: Decimal, provider: str) -> MarketFreshness | None:
        """Whether an observation of this age may enter comparisons.

        A snapshot inside its provider's fresh window is contemporaneous.  One
        past it may still be compared while it is inside the permitted stale
        window, and says so explicitly.  Beyond that window it stays visible on
        the board but enters no group.

        The boundary itself is the cache's, read through the one shared
        predicate in :mod:`app.domain.freshness`: an observation exactly one
        fresh window old is served as a miss rather than a hit, so it can never
        be a fresh member of a comparison either.
        """

        if within_fresh_window(
            age,
            self._window("dfs_cache_fresh_seconds_for", provider, DEFAULT_FRESH_SECONDS),
        ):
            return MarketFreshness.FRESH
        if within_max_age(
            age,
            self._window(
                "dfs_cache_stale_if_error_seconds_for", provider, DEFAULT_STALE_SECONDS
            ),
        ):
            return MarketFreshness.STALE
        return None

    def _window(self, accessor: str, provider: str, default: Decimal) -> Decimal:
        """One configured window as an exact decimal number of seconds."""

        providers = getattr(self.settings, "providers", None)
        reader = getattr(providers, accessor, None)
        configured = default if not callable(reader) else reader(provider)
        return time_window_seconds(configured, field=accessor)

    def _provider_reports(
        self, board: Any, observed_at: datetime, filters: ComparisonFilters
    ) -> tuple[ProviderReport, ...]:
        reports: list[ProviderReport] = []
        for outcome in board.provider_outcomes:
            if filters.providers and outcome.provider not in filters.providers:
                continue
            snapshot = outcome.snapshot
            coverage = outcome.coverage
            observation = (
                None if snapshot is None else self._observation(snapshot, observed_at)
            )
            reports.append(
                ProviderReport(
                    provider=outcome.provider,
                    status=outcome.status.value,
                    reason=None if outcome.reason is None else outcome.reason.value,
                    retrieved_at=None if snapshot is None else snapshot.retrieved_at,
                    age_seconds=None if observation is None else observation.age_seconds,
                    freshness=None if observation is None else observation.freshness,
                    snapshot_status=(
                        None if observation is None else observation.snapshot_status
                    ),
                    future_observation=(
                        False if observation is None else observation.is_future
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
                    coverage=BoardCoverage.of(coverage),
                    cache=BoardCacheState.of(outcome),
                )
            )
        return tuple(sorted(reports, key=lambda report: report.provider))

def _evidence_order(
    entry: tuple[PlayerProjectionMarket, BoardObservation],
) -> tuple[bytes, bytes]:
    """A total order over one retained observation's own normalized content."""

    market, observation = entry
    return market_evidence_key(market), observation_evidence_key(observation)


def _group(key: ComparisonKey, members: Iterable[ComparisonMember]) -> ComparisonGroup:
    ordered = _ordered_members(members)
    return ComparisonGroup(key=key, members=ordered, summary=ComparisonSummary.of(ordered))


def _ordered_members(members: Iterable[ComparisonMember]) -> tuple[ComparisonMember, ...]:
    """Deterministically ordered members.

    Nothing is collapsed here: identity is decided once, over whole normalized
    markets, before a member is ever reduced, so legitimate multiple
    thresholds, variants, statuses, prices, and same-provider markets all stay
    distinct members of one group and two markets can never be merged because
    the facts a member happens to state agree.
    """

    return tuple(sorted(members, key=lambda member: member.order))


__all__ = [
    "DEFAULT_MAX_COMPARISON_MARKETS",
    "ComparisonBoardService",
    "ComparisonBoardTooLargeError",
    "UnreadableComparisonBoardError",
]
