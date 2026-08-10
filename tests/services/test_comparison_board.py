"""Offline, high-level tests for the factual Comparison Group board."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_CEILING,
    Clamped,
    Decimal,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)

import pytest
import requests

from app.domain import comparisons
from app.domain.comparisons import (
    MAX_EXACT_DIFFERENCE_SPAN,
    NORMALIZED_DECIMAL_PLACE_LIMIT,
    REFERENCE_VERSION,
    SUPPORTED_NARROWING_FILTERS,
    NumericDomainError,
    BoardCacheState,
    BoardReadEvidence,
    ComparisonBoard,
    ComparisonGroup,
    ComparisonMember,
    CatalogAvailabilityReason,
    ComparisonExclusion,
    ComparisonFilters,
    ComparisonFreshness,
    ComparisonSummary,
    MarketFreshness,
    canonical_decimal,
    canonical_selections,
    market_evidence_key,
    market_reference,
    selection_reference,
)
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.providers.dfs import (
    AppearanceEvidence,
    AthleteEvidence,
    CompetitionEvidence,
    CoverageEvidence,
    EventEvidence,
    LeagueEvidence,
    MalformedProviderResponseError,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    Selection,
    SelectionModifier,
    SnapshotStatus,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.athlete_mapping_repository import (
    BoardMappingOutcome,
    ProviderAthleteMappingRecord,
)
from app.services.athlete_resolver import (
    AthleteResolution,
    CanonicalAthlete,
    MappingResolutionState,
)
from app.services import comparison_board
from app.services.comparison_board import (
    ComparisonBoardService,
    ComparisonBoardTooLargeError,
    UnreadableComparisonBoardError,
)
from app.services.dfs_board import (
    DFSBoard,
    DFSBoardService,
    ProviderFailureReason,
    ProviderOutcome,
    ProviderOutcomeStatus,
)
from app.services.event_mapping_repository import (
    BoardEventMappingOutcome,
    ProviderEventMappingRecord,
)
from app.services.event_resolver import (
    CanonicalEvent,
    EventResolution,
    EventResolutionState,
)
from app.services.statistic_catalog import StatisticCatalog

SEASON = "2025-26"
PLACE_LIMIT = NORMALIZED_DECIMAL_PLACE_LIMIT
RETRIEVED_AT = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
GENERATED_AT = RETRIEVED_AT + timedelta(seconds=30)


# -- fakes -----------------------------------------------------------------


def _record(record_type, **values):
    """Build one persistence record with only the facts a test cares about."""

    empty = {field.name: None for field in fields(record_type)}
    return record_type(**{**empty, **values})


class FakeAthleteResolver:
    """Resolve provider athlete IDs from an explicit reviewed mapping."""

    def __init__(self, canonical: dict[str, int], states: dict[str, str] | None = None):
        self.canonical = canonical
        self.states = states or {}

    def resolve(self, provider, evidence, season, *, observed_at=None):
        provider_id = evidence.provider_id or ""
        player_id = self.canonical.get(provider_id)
        state = MappingResolutionState(self.states.get(provider_id, "auto"))
        if player_id is None or state is not MappingResolutionState.AUTO:
            return AthleteResolution(
                provider=provider,
                provider_evidence=evidence,
                season=season,
                state=(
                    state
                    if state is not MappingResolutionState.AUTO
                    else MappingResolutionState.UNMATCHED
                ),
                reason="test",
                observed_at=observed_at,
            )
        return AthleteResolution(
            provider=provider,
            provider_evidence=evidence,
            season=season,
            state=MappingResolutionState.AUTO,
            canonical_athlete=CanonicalAthlete(
                season=season,
                player_id=player_id,
                display_name=evidence.name or "",
                roster_status="Active",
                is_active=True,
                is_active_for_season=True,
            ),
            observed_at=observed_at,
        )

    def resolve_market(self, market, season, *, observed_at=None):
        return self.resolve(
            market.provider, market.athlete, season, observed_at=observed_at
        )


class FakeAthleteRepository:
    def record_resolution(self, resolution):
        return _FakeAthletePersistence(resolution)


class _FakeAthletePersistence:
    def __init__(self, resolution):
        self.resolution = resolution

    def board_outcome(self, resolution):
        claiming = resolution.state is MappingResolutionState.AUTO
        mapping = None
        if claiming:
            mapping = _record(
                ProviderAthleteMappingRecord,
                provider=resolution.provider,
                provider_athlete_id=resolution.provider_athlete_id,
                mapping_state=resolution.state.value,
                is_active=True,
                season=resolution.season,
                canonical_player_id=resolution.canonical_player_id,
                first_seen_at="2026-08-09T20:00:00+00:00",
                last_seen_at="2026-08-09T20:00:00+00:00",
            )
        return BoardMappingOutcome(
            resolution=resolution,
            state=resolution.state,
            persisted=True,
            mapping=mapping,
        )


class FakeEventResolver:
    """Resolve provider event IDs from an explicit reviewed mapping."""

    def __init__(self, canonical: dict[str, str], states: dict[str, str] | None = None):
        self.canonical = canonical
        self.states = states or {}

    def resolve(self, provider, evidence, season, *, observed_at=None):
        provider_id = evidence.provider_id or ""
        game_id = self.canonical.get(provider_id)
        state = EventResolutionState(self.states.get(provider_id, "auto"))
        if game_id is None or state is not EventResolutionState.AUTO:
            return EventResolution(
                provider=provider,
                provider_evidence=evidence,
                season=season,
                state=(
                    state
                    if state is not EventResolutionState.AUTO
                    else EventResolutionState.UNMATCHED
                ),
                reason="test",
                observed_at=observed_at,
            )
        return EventResolution(
            provider=provider,
            provider_evidence=evidence,
            season=season,
            state=EventResolutionState.AUTO,
            canonical_event=CanonicalEvent(
                nba_game_id=game_id, season=season, scheduled_at=None
            ),
            observed_at=observed_at,
        )

    def resolve_market(self, market, season, *, observed_at=None):
        return self.resolve(
            market.provider, market.event, season, observed_at=observed_at
        )


class FakeEventRepository:
    def record_resolution(self, resolution):
        return _FakeEventPersistence(resolution)


class _FakeEventPersistence:
    def __init__(self, resolution):
        self.resolution = resolution

    def board_outcome(self, resolution):
        claiming = resolution.state is EventResolutionState.AUTO
        mapping = None
        if claiming and resolution.is_durable:
            mapping = _record(
                ProviderEventMappingRecord,
                provider=resolution.provider,
                provider_event_id=resolution.provider_event_id,
                mapping_state=resolution.state.value,
                is_active=True,
                season=resolution.season,
                canonical_event_id=resolution.canonical_event_id,
                first_seen_at="2026-08-09T20:00:00+00:00",
                last_seen_at="2026-08-09T20:00:00+00:00",
            )
        return BoardEventMappingOutcome(
            resolution=resolution,
            state=resolution.state,
            persisted=True,
            mapping=mapping,
        )


class FakeCatalog:
    """A canonical catalog reporting one reviewed freshness document.

    It records the observation instant it was asked about, because a board must
    age every catalog against exactly the instant it states.
    """

    def __init__(self, document, *, ttl_seconds=None, fresh_key="is_fresh"):
        self.document = document
        self.ttl_seconds = ttl_seconds
        self.fresh_key = fresh_key
        self.seasons: list[str] = []
        self.observed: list[datetime] = []

    def get_freshness(self, season, *, now=None):
        self.seasons.append(season)
        self.observed.append(now)
        if self.ttl_seconds is None:
            return self.document
        last_success = datetime.fromisoformat(self.document["last_success_at"])
        return {
            **self.document,
            self.fresh_key: now <= last_success + timedelta(seconds=self.ttl_seconds),
        }


def _athlete_catalog(*, fresh=True, last_success_at="2026-08-09T12:00:00+00:00"):
    return FakeCatalog(
        {
            "season": SEASON,
            "is_fresh": fresh,
            "freshness_days": 7,
            "last_success_at": last_success_at,
        }
    )


def _event_catalog(*, fresh=True, last_success_at="2026-08-09T12:00:00+00:00"):
    return FakeCatalog(
        {
            "season": SEASON,
            "fresh": fresh,
            "max_age_hours": 72.0,
            "last_success_at": last_success_at,
        }
    )


class FakeProvider:
    def __init__(self, snapshot, *, delay_seconds: float = 0.0):
        self.snapshot = snapshot
        self.delay_seconds = delay_seconds
        self.failure: Exception | None = None
        self.calls = 0

    def get_snapshot(self, query, context):
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.failure is not None:
            raise self.failure
        return self.snapshot


# -- builders --------------------------------------------------------------


def _market(
    provider="dabble",
    *,
    market_id="m-1",
    athlete_id="a-1",
    athlete_name="Nikola Jokic",
    event_id="e-1",
    statistic="points",
    threshold="25.5",
    unit="count",
    variant=MarketVariant.STANDARD,
    status=MarketStatus.AVAILABLE,
    scoring_period=ScoringPeriod.FULL_GAME,
    selections=(),
):
    return PlayerProjectionMarket(
        provider=provider,
        market_id=market_id,
        athlete=(
            None
            if athlete_id is None and athlete_name is None
            else AthleteEvidence(provider_id=athlete_id, name=athlete_name)
        ),
        event=(
            None
            if event_id is None
            else EventEvidence(
                provider_id=event_id,
                label="DEN @ LAL",
                home_team=TeamEvidence(canonical_id=1610612747),
                away_team=TeamEvidence(canonical_id=1610612743),
            )
        ),
        statistic=(None if statistic is None else StatisticEvidence(label=statistic)),
        threshold=(None if threshold is None else MarketThreshold(threshold, unit)),
        status=status,
        variant=variant,
        scoring_period=scoring_period,
        selections=selections,
    )


def _snapshot(provider, markets, *, retrieved_at=RETRIEVED_AT, coverage=None):
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=tuple(markets),
        coverage=coverage
        or CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=retrieved_at,
    )


def _service(
    snapshots,
    *,
    athletes=None,
    events=None,
    athlete_states=None,
    event_states=None,
    athlete_catalog=None,
    event_catalog=None,
    max_markets=None,
    generated_at=GENERATED_AT,
    observed_at=None,
    statistic_catalog=None,
    delays=None,
):
    providers = {
        snapshot.provider: FakeProvider(
            snapshot, delay_seconds=(delays or {}).get(snapshot.provider, 0.0)
        )
        for snapshot in snapshots
    }
    board_service = DFSBoardService(
        provider_registry=providers,
        clock=lambda: generated_at,
        statistic_catalog=statistic_catalog or StatisticCatalog.load_default(),
        athlete_resolver=FakeAthleteResolver(
            athletes if athletes is not None else {"a-1": 203999, "a-2": 2544},
            athlete_states,
        ),
        athlete_mapping_repository=FakeAthleteRepository(),
        event_resolver=FakeEventResolver(
            events if events is not None else {"e-1": "0022500001", "e-2": "0022500002"},
            event_states,
        ),
        event_mapping_repository=FakeEventRepository(),
    )
    observation = observed_at or generated_at
    service = ComparisonBoardService(
        board_service,
        athlete_catalog=athlete_catalog or _athlete_catalog(),
        event_catalog=event_catalog or _event_catalog(),
        max_markets=max_markets,
        clock=lambda: observation,
    )
    return service, providers


def _query():
    return NBAMarketQuery(season=SEASON)


def _context(seconds=5.0):
    return RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        request_id="comparison-test",
    )


def _read(service):
    return service.get_comparisons(_query(), _context())


# -- identity keys ---------------------------------------------------------


def test_a_group_requires_the_same_event_athlete_statistic_and_period():
    markets = (
        _market(market_id="m-1"),
        _market(market_id="m-2", event_id="e-2"),
        _market(market_id="m-3", athlete_id="a-2", athlete_name="LeBron James"),
        _market(market_id="m-4", statistic="rebounds"),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert board.availability.available
    assert len(board.groups) == 4
    assert all(group.summary.market_count == 1 for group in board.groups)
    assert [group.key.order for group in board.groups] == sorted(
        group.key.order for group in board.groups
    )


def test_markets_sharing_one_canonical_identity_form_one_group():
    service, _ = _service(
        [
            _snapshot("dabble", (_market("dabble", market_id="d-1"),)),
            _snapshot("prizepicks", (_market("prizepicks", market_id="p-1", statistic="Points"),)),
        ]
    )

    board = _read(service)

    assert len(board.groups) == 1
    group = board.groups[0]
    assert group.key.canonical_event_id == "0022500001"
    assert group.key.canonical_athlete_id == 203999
    assert group.key.canonical_statistic_id == "points"
    assert group.key.scoring_period is ScoringPeriod.FULL_GAME
    assert group.summary.provider_count == 2
    assert [member.provider for member in group.members] == ["dabble", "prizepicks"]


def test_a_period_specific_market_never_joins_a_full_game_group():
    service, _ = _service(
        [
            _snapshot(
                "dabble",
                (
                    _market(market_id="m-1"),
                    _market(market_id="m-2", scoring_period=ScoringPeriod.FIRST_HALF),
                ),
            )
        ]
    )

    board = _read(service)

    assert len(board.groups) == 1
    assert board.groups[0].key.scoring_period is ScoringPeriod.FULL_GAME
    assert [entry.reason for entry in board.unresolved] == [
        ComparisonExclusion.UNMAPPED_STATISTIC
    ]


# -- multiple offerings ----------------------------------------------------


def test_thresholds_variants_statuses_and_same_provider_markets_stay_distinct():
    markets = (
        _market(market_id="m-1", threshold="25.5"),
        _market(market_id="m-2", threshold="27.5"),
        _market(market_id="m-3", threshold="25.5", variant=MarketVariant.ALTERNATE),
        _market(market_id="m-4", threshold="25.5", status=MarketStatus.SUSPENDED),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert len(board.groups) == 1
    group = board.groups[0]
    assert group.summary.market_count == 4
    assert group.summary.provider_count == 1
    assert len({member.market_reference for member in group.members}) == 4


def test_exact_repeated_source_identities_deduplicate_only_when_content_agrees():
    repeated = _market(market_id="m-1")
    snapshot = _snapshot("dabble", (repeated, repeated, _market(market_id="m-2")))

    service, _ = _service([snapshot])
    board = _read(service)

    assert board.groups[0].summary.market_count == 2

    with pytest.raises(MalformedProviderResponseError):
        _snapshot(
            "dabble",
            (_market(market_id="m-1"), _market(market_id="m-1", threshold="27.5")),
        )


def test_id_less_available_and_suspended_markets_are_distinct_offerings():
    markets = (
        _market(market_id=None),
        _market(market_id=None, status=MarketStatus.SUSPENDED),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert board.market_count == 2
    assert len(board.groups) == 1
    assert board.groups[0].summary.market_count == 2
    assert {member.status for member in board.groups[0].members} == {
        MarketStatus.AVAILABLE,
        MarketStatus.SUSPENDED,
    }
    assert len({market.market_reference for market in board.markets}) == 2
    assert board.unresolved == ()


# -- unresolved evidence ---------------------------------------------------


@pytest.mark.parametrize(
    ("athlete_states", "event_states", "reason", "detail"),
    [
        ({"a-1": "ambiguous"}, None, ComparisonExclusion.UNRESOLVED_ATHLETE, "ambiguous"),
        ({"a-1": "unmatched"}, None, ComparisonExclusion.UNRESOLVED_ATHLETE, "unmatched"),
        (
            {"a-1": "mapping_conflict"},
            None,
            ComparisonExclusion.UNRESOLVED_ATHLETE,
            "mapping_conflict",
        ),
        (None, {"e-1": "ambiguous"}, ComparisonExclusion.UNRESOLVED_EVENT, "ambiguous"),
        (
            None,
            {"e-1": "event_catalog_unavailable"},
            ComparisonExclusion.UNRESOLVED_EVENT,
            "event_catalog_unavailable",
        ),
    ],
)
def test_unresolved_identities_stay_visible_and_enter_no_group(
    athlete_states, event_states, reason, detail
):
    service, _ = _service(
        [_snapshot("dabble", (_market(),))],
        athlete_states=athlete_states,
        event_states=event_states,
    )

    board = _read(service)

    assert board.groups == ()
    assert [entry.reason for entry in board.unresolved] == [reason]
    assert board.unresolved[0].detail == detail
    assert board.unresolved[0].market_reference


def test_unmapped_and_missing_evidence_markets_stay_visible():
    markets = (
        _market(market_id="m-1", statistic="Fantasy Score"),
        _market(market_id="m-2", statistic=None),
        _market(market_id="m-3", athlete_id=None, athlete_name=None),
        _market(market_id="m-4", event_id=None),
        _market(market_id="m-5", threshold=None),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert board.groups == ()
    assert [entry.reason for entry in board.unresolved] == sorted(
        (
            ComparisonExclusion.MISSING_ATHLETE_EVIDENCE,
            ComparisonExclusion.MISSING_EVENT_EVIDENCE,
            ComparisonExclusion.MISSING_THRESHOLD,
            ComparisonExclusion.UNMAPPED_STATISTIC,
            ComparisonExclusion.UNMAPPED_STATISTIC,
        ),
        key=lambda value: value.value,
    )
    assert board.market_count == 5


def test_a_non_comparable_canonical_statistic_never_enters_a_group(tmp_path):
    document = tmp_path / "catalog.yaml"
    document.write_text(
        "schema_version: 1\n"
        "statistics:\n"
        "  - id: points\n"
        "    label: Points\n"
        "    unit: count\n"
        "    scoring_periods: [full_game]\n"
        "    components: [points]\n"
        "    comparable: false\n"
        "    provider_mappings:\n"
        "      dabble: [points]\n",
        encoding="utf-8",
    )
    service, _ = _service(
        [_snapshot("dabble", (_market(),))],
        statistic_catalog=StatisticCatalog.load(document),
    )

    board = _read(service)

    assert board.groups == ()
    assert board.unresolved[0].reason is ComparisonExclusion.NON_COMPARABLE_STATISTIC
    assert board.unresolved[0].detail == "points"


# -- comparison availability ----------------------------------------------


@pytest.mark.parametrize(
    ("catalog_name", "athlete_catalog", "event_catalog", "reason"),
    [
        (
            "athlete_catalog",
            _athlete_catalog(last_success_at=None),
            None,
            CatalogAvailabilityReason.MISSING,
        ),
        (
            "athlete_catalog",
            _athlete_catalog(fresh=False),
            None,
            CatalogAvailabilityReason.STALE,
        ),
        (
            "event_catalog",
            None,
            _event_catalog(last_success_at=None),
            CatalogAvailabilityReason.MISSING,
        ),
        (
            "event_catalog",
            None,
            _event_catalog(fresh=False),
            CatalogAvailabilityReason.STALE,
        ),
    ],
)
def test_missing_or_over_age_catalogs_make_comparisons_unavailable(
    catalog_name, athlete_catalog, event_catalog, reason
):
    service, _ = _service(
        [_snapshot("dabble", (_market(),))],
        athlete_catalog=athlete_catalog,
        event_catalog=event_catalog,
    )

    board = _read(service)

    assert not board.availability.available
    assert board.groups == ()
    unavailable = board.availability.unavailable_catalogs
    assert [entry.catalog for entry in unavailable] == [catalog_name]
    assert unavailable[0].reason is reason
    assert unavailable[0].season == SEASON
    if reason is CatalogAvailabilityReason.STALE:
        assert unavailable[0].age_seconds == Decimal("28830")
        assert unavailable[0].last_success_at == datetime(
            2026, 8, 9, 12, 0, tzinfo=timezone.utc
        )
    # The markets themselves are retained, only their comparability is withheld.
    assert [entry.reason for entry in board.unresolved] == [
        ComparisonExclusion.COMPARISON_UNAVAILABLE
    ]
    assert board.unresolved[0].detail == reason.value


def test_an_unconfigured_catalog_reports_its_own_unavailability():
    service, _ = _service(
        [_snapshot("dabble", (_market(),))],
        athlete_catalog=FakeCatalog(None),
    )

    board = _read(service)

    assert not board.availability.available
    reasons = {entry.catalog: entry.reason for entry in board.availability.catalogs}
    assert reasons["athlete_catalog"] is CatalogAvailabilityReason.MISSING
    assert reasons["event_catalog"] is None


# -- freshness -------------------------------------------------------------


def test_stale_markets_enter_only_within_the_permitted_window():
    within = _snapshot(
        "dabble",
        (_market("dabble", market_id="d-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1200),
    )
    beyond = _snapshot(
        "prizepicks",
        (_market("prizepicks", market_id="p-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=3600),
    )
    service, _ = _service([within, beyond])

    board = _read(service)

    assert len(board.groups) == 1
    assert [member.provider for member in board.groups[0].members] == ["dabble"]
    assert board.groups[0].members[0].freshness is MarketFreshness.STALE
    assert board.groups[0].summary.freshness is ComparisonFreshness.STALE
    assert [entry.reason for entry in board.unresolved] == [
        ComparisonExclusion.STALE_SNAPSHOT
    ]


def test_non_contemporaneous_groups_are_explicit_mixed_freshness_comparisons():
    fresh = _snapshot("dabble", (_market("dabble", market_id="d-1"),))
    stale = _snapshot(
        "prizepicks",
        (_market("prizepicks", market_id="p-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1200),
    )
    service, _ = _service([fresh, stale])

    board = _read(service)

    group = board.groups[0]
    assert group.is_mixed_freshness
    assert group.summary.freshness is ComparisonFreshness.MIXED
    assert board.mixed_freshness_groups == (group,)


# -- exact decimals --------------------------------------------------------


def test_summaries_are_exact_decimals_and_state_nothing_else():
    markets = (
        _market(market_id="m-1", threshold="25.5"),
        _market(market_id="m-2", threshold="27.25"),
        _market(market_id="m-3", threshold="0.1"),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    summary = _read(service).groups[0].summary

    assert summary.minimum_threshold == Decimal("0.1")
    assert summary.maximum_threshold == Decimal("27.25")
    assert summary.threshold_spread == Decimal("27.15")
    assert str(summary.threshold_spread) == "27.15"
    assert {field.name for field in fields(ComparisonSummary)} == {
        "minimum_threshold",
        "maximum_threshold",
        "threshold_spread",
        "provider_count",
        "market_count",
        "freshness",
        "market_references",
    }


def test_no_opinion_is_ever_produced():
    forbidden = (
        "probability",
        "expected_value",
        "recommendation",
        "average",
        "preferred",
        "payout",
    )
    declared = {
        name
        for module in (comparisons, comparison_board)
        for name in dir(module)
    }
    stated = {
        field.name
        for value_type in (
            ComparisonSummary,
            ComparisonGroup,
            ComparisonMember,
            ComparisonBoard,
        )
        for field in fields(value_type)
    }
    for word in forbidden:
        assert not [name for name in declared if word in name]
        assert not [name for name in stated if word in name]


# -- references ------------------------------------------------------------


def test_references_are_versioned_and_stable_while_identity_is_unchanged():
    market = _market(market_id="m-1")
    same_identity = _market(market_id="m-1", status=MarketStatus.SUSPENDED)
    other_identity = _market(market_id="m-2")

    assert market_reference(market).startswith(f"mkt_{REFERENCE_VERSION}_")
    assert market_reference(market) == market_reference(same_identity)
    assert market_reference(market) != market_reference(other_identity)


def test_identityless_markets_are_referenced_by_the_facts_they_report():
    market = _market(market_id=None)
    repeat = _market(market_id=None)
    changed = _market(market_id=None, threshold="27.5")

    assert market_reference(market) == market_reference(repeat)
    assert market_reference(market) != market_reference(changed)


def test_comparison_and_selection_references_are_deterministic():
    selections = (
        Selection(selection_id="s-2", direction="lower"),
        Selection(selection_id="s-1", direction="higher"),
    )
    service, _ = _service(
        [_snapshot("dabble", (_market(selections=selections),))]
    )

    board = _read(service)
    group = board.groups[0]

    assert group.reference.startswith(f"cmp_{REFERENCE_VERSION}_")
    assert group.reference == group.key.reference
    references = group.members[0].selection_references
    assert len(references) == 2
    assert tuple(sorted(references)) == references
    assert all(
        reference.startswith(f"sel_{REFERENCE_VERSION}_") for reference in references
    )


# -- ordering --------------------------------------------------------------


def test_ordering_does_not_depend_on_provider_completion_order():
    def read(slow_provider):
        service, providers = _service(
            [
                _snapshot("dabble", (_market("dabble", market_id="d-1"),)),
                _snapshot("prizepicks", (_market("prizepicks", market_id="p-1"),)),
            ],
            delays={slow_provider: 0.05},
        )
        board = service.get_comparisons(_query(), _context())
        assert providers[slow_provider].calls == 1
        return board

    first = read("dabble")
    second = read("prizepicks")

    assert [group.key.order for group in first.groups] == [
        group.key.order for group in second.groups
    ]
    assert [member.order for member in first.groups[0].members] == [
        member.order for member in second.groups[0].members
    ]
    assert [report.provider for report in first.provider_reports] == [
        "dabble",
        "prizepicks",
    ]


def test_unresolved_and_warning_ordering_is_deterministic():
    coverage = CoverageEvidence(
        fetched_count=2,
        eligible_count=2,
        normalized_count=2,
        pagination_complete=True,
        fanout_complete=True,
        warning_codes=("duplicate_source_identity",),
        skipped_reasons=("non_player_market",),
    )
    markets = (
        _market(market_id="m-1", statistic="Fantasy Score"),
        _market(market_id="m-2", event_id=None),
    )
    service, _ = _service([_snapshot("dabble", markets, coverage=coverage)])

    board = _read(service)

    assert [entry.order for entry in board.unresolved] == sorted(
        entry.order for entry in board.unresolved
    )
    assert board.provider_reports[0].warning_codes == (
        "duplicate_source_identity",
        "non_player_market",
    )


# -- filters ---------------------------------------------------------------


def test_a_provider_filter_avoids_the_call_entirely():
    service, providers = _service(
        [
            _snapshot("dabble", (_market("dabble", market_id="d-1"),)),
            _snapshot("prizepicks", (_market("prizepicks", market_id="p-1"),)),
        ]
    )

    board = service.get_comparisons(
        _query(), _context(), filters=ComparisonFilters(providers=("prizepicks",))
    )

    assert providers["dabble"].calls == 0
    assert providers["prizepicks"].calls == 1
    assert board.groups[0].summary.provider_count == 1
    assert [report.provider for report in board.provider_reports] == ["prizepicks"]
    assert board.disabled_providers == ("dabble", "underdog")


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (ComparisonFilters(canonical_athlete_ids=(203999,)), 3),
        (ComparisonFilters(canonical_athlete_ids=(2544,)), 1),
        (ComparisonFilters(canonical_event_ids=("0022500002",)), 1),
        (ComparisonFilters(canonical_statistic_ids=("rebounds",)), 1),
        (ComparisonFilters(canonical_statistic_ids=("assists",)), 0),
    ],
)
def test_canonical_identity_filters_narrow_the_board_centrally(filters, expected):
    markets = (
        _market(market_id="m-1"),
        _market(market_id="m-2", athlete_id="a-2", athlete_name="LeBron James"),
        _market(market_id="m-3", event_id="e-2"),
        _market(market_id="m-4", statistic="rebounds"),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = service.get_comparisons(_query(), _context(), filters=filters)

    assert len(board.groups) == expected
    assert board.filters is filters


def test_a_market_status_filter_narrows_without_removing_a_group():
    markets = (
        _market(market_id="m-1"),
        _market(market_id="m-2", status=MarketStatus.SUSPENDED),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = service.get_comparisons(
        _query(),
        _context(),
        filters=ComparisonFilters(market_statuses=(MarketStatus.SUSPENDED,)),
    )

    assert board.groups[0].summary.market_count == 1
    assert board.groups[0].members[0].status is MarketStatus.SUSPENDED


def test_canonical_filters_exclude_markets_that_have_no_canonical_identity():
    markets = (_market(market_id="m-1"), _market(market_id="m-2", statistic=None))
    service, _ = _service([_snapshot("dabble", markets)])

    board = service.get_comparisons(
        _query(),
        _context(),
        filters=ComparisonFilters(canonical_athlete_ids=(203999,)),
    )

    assert board.unresolved == ()
    assert board.market_count == 1


def test_there_is_no_fuzzy_name_filter():
    names = {field.name for field in fields(ComparisonFilters)}

    assert names == {
        "providers",
        "canonical_athlete_ids",
        "canonical_event_ids",
        "canonical_statistic_ids",
        "market_statuses",
    }
    assert not [name for name in names if "name" in name]


# -- limits ----------------------------------------------------------------


def test_a_board_over_the_ceiling_is_refused_rather_than_truncated():
    markets = tuple(
        _market(market_id=f"m-{index}", threshold=str(20 + index)) for index in range(4)
    )
    service, _ = _service([_snapshot("dabble", markets)], max_markets=3)

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        _read(service)

    assert error.value.code == "board_too_large"
    assert error.value.observed_market_count == 4
    assert error.value.market_limit == 3
    assert error.value.supported_filters == SUPPORTED_NARROWING_FILTERS


def test_a_refused_board_keeps_the_evidence_it_already_gathered():
    """A ceiling refusal knows as much about the read as a published board.

    The retrieval and the whole classification are complete by the time the
    ceiling is applied, so the provider outcomes, freshness, cache states,
    disabled providers, and comparison availability are all already facts.
    They travel with the refusal so an operator sees the same read a served
    board would have shown.
    """

    markets = tuple(
        _market(market_id=f"m-{index}", threshold=str(20 + index)) for index in range(4)
    )
    service, _ = _service([_snapshot("dabble", markets)], max_markets=3)

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        _read(service)

    evidence = error.value.board_evidence
    assert evidence.market_count == 4
    assert evidence.availability.available is True
    assert [report.provider for report in evidence.provider_reports] == ["dabble"]
    assert [report.status for report in evidence.provider_reports] == ["complete"]
    assert [report.freshness.value for report in evidence.provider_reports] == ["fresh"]
    assert evidence.group_count == 1
    assert evidence.unresolved_count == 0
    assert evidence.disabled_providers == ("prizepicks", "underdog")


def test_a_refusal_states_the_providers_it_did_not_read():
    """A provider filter narrows the refusal's evidence exactly as it narrows a board."""

    service, _ = _service(
        [
            _snapshot("dabble", (_market(market_id="m-1"),)),
            _snapshot(
                "prizepicks",
                (_market(provider="prizepicks", market_id="m-2"),),
            ),
        ],
        max_markets=1,
    )

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        service.get_comparisons(_query(), _context())

    evidence = error.value.board_evidence
    assert evidence.market_count == 2
    assert [report.provider for report in evidence.provider_reports] == [
        "dabble",
        "prizepicks",
    ]
    assert evidence.disabled_providers == ("underdog",)
    assert error.value.public_details == {
        "observed_market_count": 2,
        "market_limit": 1,
        "supported_filters": list(SUPPORTED_NARROWING_FILTERS),
    }


def test_the_ceiling_applies_after_filters():
    markets = (
        _market(market_id="m-1"),
        _market(market_id="m-2", athlete_id="a-2", athlete_name="LeBron James"),
    )
    service, _ = _service([_snapshot("dabble", markets)], max_markets=1)

    board = service.get_comparisons(
        _query(), _context(), filters=ComparisonFilters(canonical_athlete_ids=(203999,))
    )

    assert board.market_count == 1


def test_the_default_ceiling_is_ten_thousand():
    service, _ = _service([_snapshot("dabble", ())])

    assert service.max_markets == 10000


# -- readability before the ceiling ----------------------------------------


def _oversized_markets(count=2):
    return tuple(
        _market(market_id=f"m-{index}", threshold=str(20 + index))
        for index in range(count)
    )


def test_an_unreadable_stale_read_over_the_ceiling_is_not_refused_as_too_large():
    """Readability is settled before size: an outage is not an over-large board.

    Nothing on this read may be compared, so there is no board to be too large
    of, and none is built.  The read fails with its own type, carrying only the
    bounded evidence the response seam needs to state the outage.
    """

    service, _ = _service(
        [
            _snapshot(
                "dabble",
                _oversized_markets(),
                retrieved_at=GENERATED_AT - timedelta(seconds=1801),
            )
        ],
        max_markets=1,
    )

    with pytest.raises(UnreadableComparisonBoardError) as error:
        _read(service)

    evidence = error.value.board_evidence
    assert isinstance(evidence, BoardReadEvidence)
    assert evidence.market_count == 2
    assert evidence.unresolved_count == 2
    assert evidence.group_count == 0
    assert [report.freshness for report in evidence.provider_reports] == [None]
    assert [report.status for report in evidence.provider_reports] == ["complete"]


def test_an_unreadable_read_is_a_distinct_type_carrying_no_board():
    """The refusal is its own type, and it publishes nothing serializable.

    It is not a too-large refusal, so nothing can mistake an outage for a
    board a caller could narrow, and it carries no ``ComparisonBoard`` at all,
    so no count-only board can reach the serializer.
    """

    service, _ = _service(
        [
            _snapshot(
                "dabble",
                _oversized_markets(),
                retrieved_at=GENERATED_AT - timedelta(seconds=1801),
            )
        ],
        max_markets=1,
    )

    with pytest.raises(UnreadableComparisonBoardError) as error:
        _read(service)

    assert not isinstance(error.value, ComparisonBoardTooLargeError)
    # Its central public contract is the same safe 503 the response seam
    # states, so an escape can never publish a 400 telling a caller to narrow
    # filters that cannot make an outage readable.
    assert error.value.status_code == 503
    assert error.value.code == "provider_unavailable"
    assert error.value.public_details is None
    assert not any(
        isinstance(getattr(error.value, name, None), ComparisonBoard)
        for name in vars(error.value)
    )


def test_an_unreadable_future_read_over_the_ceiling_is_not_refused_as_too_large():
    service, _ = _service(
        [
            _snapshot(
                "dabble",
                _oversized_markets(),
                retrieved_at=GENERATED_AT + timedelta(seconds=60),
            )
        ],
        max_markets=1,
    )

    with pytest.raises(UnreadableComparisonBoardError) as error:
        _read(service)

    evidence = error.value.board_evidence
    assert evidence.market_count == 2
    assert evidence.unresolved_count == 2
    assert evidence.group_count == 0
    assert [report.future_observation for report in evidence.provider_reports] == [True]


def test_a_read_at_the_exact_stale_ceiling_is_readable_and_still_too_large():
    """The inclusive boundary is readable, so the ceiling decides this read."""

    service, _ = _service(
        [
            _snapshot(
                "dabble",
                _oversized_markets(),
                retrieved_at=GENERATED_AT - timedelta(seconds=1800),
            )
        ],
        max_markets=1,
    )

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        _read(service)

    evidence = error.value.board_evidence
    assert error.value.observed_market_count == 2
    assert [report.freshness for report in evidence.provider_reports] == [
        MarketFreshness.STALE
    ]


def test_one_readable_provider_keeps_the_ceiling_over_an_unreadable_one():
    """A board with something to state can still be too large to state it."""

    beyond = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    fresh = _snapshot(
        "prizepicks",
        (_market(provider="prizepicks", market_id="m-2", threshold="26.0"),),
    )
    service, _ = _service([beyond, fresh], max_markets=1)

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        _read(service)

    evidence = error.value.board_evidence
    assert error.value.observed_market_count == 2
    assert evidence.market_count == 2
    assert evidence.unresolved_count == 1
    assert evidence.group_count == 1


def test_an_empty_complete_read_is_readable_under_any_ceiling():
    """Emptiness is an offering, not an outage, and it is never too large."""

    service, _ = _service([_snapshot("dabble", ())], max_markets=1)

    board = _read(service)

    assert board.market_count == 0
    assert board.unresolved_count == 0
    assert [report.freshness for report in board.provider_reports] == [
        MarketFreshness.FRESH
    ]


@pytest.mark.parametrize("reversed_order", [False, True])
def test_precedence_does_not_depend_on_the_order_providers_were_read(reversed_order):
    """Which provider answered first cannot decide 400 against 503."""

    unreadable = _snapshot(
        "dabble",
        _oversized_markets(),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    also_unreadable = _snapshot(
        "prizepicks",
        (_market(provider="prizepicks", market_id="m-9", threshold="26.0"),),
        retrieved_at=GENERATED_AT + timedelta(seconds=60),
    )
    snapshots = [unreadable, also_unreadable]
    service, _ = _service(
        list(reversed(snapshots)) if reversed_order else snapshots, max_markets=1
    )

    with pytest.raises(UnreadableComparisonBoardError) as error:
        _read(service)

    evidence = error.value.board_evidence
    assert evidence.market_count == 3
    assert evidence.unresolved_count == 3
    assert evidence.group_count == 0
    assert evidence.disabled_providers == ("underdog",)


# -- empty and complete ----------------------------------------------------


def test_an_empty_complete_board_is_available_and_states_nothing():
    service, _ = _service([_snapshot("dabble", ())])

    board = _read(service)

    assert board.availability.available
    assert board.is_empty
    assert board.groups == ()
    assert board.unresolved == ()
    assert board.market_count == 0
    assert board.provider_reports[0].status == "complete"
    assert board.provider_reports[0].market_count == 0
    assert board.season == SEASON
    assert dataclasses.is_dataclass(board)


def test_a_failed_provider_never_removes_another_provider_s_comparisons():
    service, providers = _service(
        [
            _snapshot("dabble", (_market("dabble", market_id="d-1"),)),
            _snapshot("prizepicks", ()),
        ]
    )
    providers["prizepicks"].failure = requests.exceptions.Timeout()

    board = _read(service)

    assert board.groups[0].summary.provider_count == 1
    reports = {report.provider: report for report in board.provider_reports}
    assert reports["prizepicks"].status == "failed"
    assert reports["prizepicks"].reason == "timeout"
    assert reports["prizepicks"].retrieved_at is None
    assert reports["dabble"].freshness is MarketFreshness.FRESH


def test_a_market_without_a_provider_event_id_is_compared_on_its_evidence():
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="m-1",
        athlete=AthleteEvidence(provider_id="a-1", name="Nikola Jokic"),
        event=EventEvidence(
            label="DEN @ LAL",
            starts_at=RETRIEVED_AT,
            home_team=TeamEvidence(canonical_id=1610612747),
            away_team=TeamEvidence(canonical_id=1610612743),
        ),
        statistic=StatisticEvidence(label="points"),
        threshold=MarketThreshold("25.5", "count"),
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    service, _ = _service([_snapshot("dabble", (market,))], events={"": "0022500009"})

    board = _read(service)

    assert board.groups[0].key.canonical_event_id == "0022500009"


# -- value invariants ------------------------------------------------------


def test_a_comparison_key_requires_an_explicit_scoring_period():
    with pytest.raises(ValueError, match="explicit scoring period"):
        comparisons.ComparisonKey(
            canonical_event_id="0022500001",
            canonical_athlete_id=203999,
            canonical_statistic_id="points",
            scoring_period=ScoringPeriod.UNKNOWN,
        )


def _member(reference="mkt_1_a", threshold="25.5", provider="dabble"):
    return ComparisonMember(
        market_reference=reference,
        provider=provider,
        threshold=Decimal(threshold),
        threshold_unit="count",
        variant=MarketVariant.STANDARD,
        status=MarketStatus.AVAILABLE,
        retrieved_at=RETRIEVED_AT,
        freshness=MarketFreshness.FRESH,
    )


def test_a_summary_must_state_the_exact_difference_and_sorted_references():
    with pytest.raises(ValueError, match="exact difference"):
        ComparisonSummary(
            minimum_threshold=Decimal("1"),
            maximum_threshold=Decimal("3"),
            threshold_spread=Decimal("1"),
            provider_count=1,
            market_count=1,
            freshness=ComparisonFreshness.FRESH,
            market_references=("mkt_1_a",),
        )
    with pytest.raises(ValueError, match="must be sorted"):
        ComparisonSummary(
            minimum_threshold=Decimal("1"),
            maximum_threshold=Decimal("1"),
            threshold_spread=Decimal("0"),
            provider_count=1,
            market_count=2,
            freshness=ComparisonFreshness.FRESH,
            market_references=("mkt_1_b", "mkt_1_a"),
        )


def _summary(minimum, maximum):
    return ComparisonSummary.of(
        (
            _member(reference="mkt_1_a", threshold=minimum),
            _member(reference="mkt_1_b", threshold=maximum),
        )
    )


def test_a_threshold_spread_is_exact_whatever_context_a_caller_is_inside():
    # Both thresholds carry more digits than the default context permits, so a
    # spread computed under the ambient context would be a rounded number that
    # is not the difference of the values the board states.
    minimum = "25." + "0" * 30 + "1"
    maximum = "26." + "0" * 30 + "3"
    expected = Decimal("1." + "0" * 30 + "2")

    with localcontext() as context:
        context.prec = 5
        narrow = _summary(minimum, maximum)
    with localcontext() as context:
        context.prec = 90
        wide = _summary(minimum, maximum)

    assert narrow.threshold_spread == expected
    assert wide.threshold_spread == expected
    assert narrow.minimum_threshold == Decimal(minimum)
    assert narrow.maximum_threshold == Decimal(maximum)


@pytest.mark.parametrize(
    ("minimum", "maximum", "spread"),
    [
        ("-2.5", "1.5", "4.0"),
        ("-7.25", "-3.25", "4"),
        ("-0", "0.000", "0"),
        ("-1E-30", "1E-30", "2E-30"),
        ("1E-10", "1E+40", "9" * 40 + "." + "9" * 10),
        # The highest and lowest places the normalized domain admits, in both
        # signs, and a coefficient far longer than any decimal context default.
        (f"-1E+{PLACE_LIMIT}", f"1E+{PLACE_LIMIT}", f"2E+{PLACE_LIMIT}"),
        (f"-1E-{PLACE_LIMIT}", f"1E-{PLACE_LIMIT}", f"2E-{PLACE_LIMIT}"),
        (
            f"1E-{PLACE_LIMIT}",
            f"1E+{PLACE_LIMIT}",
            "9" * PLACE_LIMIT + "." + "9" * PLACE_LIMIT,
        ),
        ("-1." + "7" * 120, "1." + "3" * 120, "3." + "1" * 119 + "0"),
    ],
)
def test_a_threshold_spread_is_exact_across_signs_zero_and_exponents(
    minimum, maximum, spread
):
    summary = _summary(minimum, maximum)

    assert summary.minimum_threshold == Decimal(minimum)
    assert summary.maximum_threshold == Decimal(maximum)
    assert summary.threshold_spread == Decimal(spread)


def test_a_summary_spread_rounded_by_the_ambient_context_is_refused():
    minimum = Decimal("25." + "0" * 30 + "1")
    maximum = Decimal("26." + "0" * 30 + "3")
    exact = Decimal("1." + "0" * 30 + "2")

    with localcontext() as context:
        context.prec = 5
        # What subtraction under this context would have produced.
        with pytest.raises(ValueError, match="exact difference"):
            comparisons.ComparisonSummary(
                minimum_threshold=minimum,
                maximum_threshold=maximum,
                threshold_spread=Decimal("1.0000"),
                provider_count=1,
                market_count=1,
                freshness=ComparisonFreshness.FRESH,
                market_references=("mkt_1_a",),
            )
        accepted = comparisons.ComparisonSummary(
            minimum_threshold=minimum,
            maximum_threshold=maximum,
            threshold_spread=exact,
            provider_count=1,
            market_count=1,
            freshness=ComparisonFreshness.FRESH,
            market_references=("mkt_1_a",),
        )

    assert accepted.threshold_spread == exact


def test_the_maximum_exact_difference_span_is_the_widest_the_domain_admits():
    # The widest pair the normalized domain admits: one value occupying every
    # place from the highest permitted down to the lowest, less its negation.
    # The difference borrows one place above the highest, so the span is the
    # whole place range plus that carry.
    widest = Decimal("9" * (2 * PLACE_LIMIT + 1) + f"E-{PLACE_LIMIT}")
    expected = Decimal("1" + "9" * (2 * PLACE_LIMIT) + "8" + f"E-{PLACE_LIMIT}")

    difference = comparisons.exact_difference(widest, widest.copy_negate())
    _sign, digits, exponent = difference.as_tuple()

    assert difference == expected
    assert len(digits) == MAX_EXACT_DIFFERENCE_SPAN
    assert exponent == -PLACE_LIMIT
    assert MAX_EXACT_DIFFERENCE_SPAN == 2 * PLACE_LIMIT + 2


def test_a_decimal_at_the_place_boundary_is_inside_the_normalized_domain():
    minimum = f"1E-{PLACE_LIMIT}"
    maximum = f"1E+{PLACE_LIMIT}"

    summary = _summary(minimum, maximum)

    assert summary.threshold_spread == Decimal(
        "9" * PLACE_LIMIT + "." + "9" * PLACE_LIMIT
    )
    assert MarketThreshold(value=maximum, unit="points").value == Decimal(maximum)
    assert MarketThreshold(value=minimum, unit="points").value == Decimal(minimum)


@pytest.mark.parametrize(
    "value",
    [
        f"1E+{PLACE_LIMIT + 1}",
        f"-1E+{PLACE_LIMIT + 1}",
        f"1E-{PLACE_LIMIT + 1}",
        f"-1E-{PLACE_LIMIT + 1}",
        "1E+999999999",
        "-1E-999999999",
        "0E+999999999",
        "1." + "0" * (2 * PLACE_LIMIT + 1) + "1",
    ],
)
def test_a_decimal_beyond_the_place_boundary_is_outside_the_domain(value):
    with pytest.raises(NumericDomainError, match="normalized numeric domain"):
        comparisons.exact_difference(Decimal(value), Decimal("1"))
    with pytest.raises(NumericDomainError, match="normalized numeric domain"):
        comparisons.exact_difference(Decimal("1"), Decimal(value))
    with pytest.raises(NumericDomainError, match="normalized numeric domain"):
        MarketThreshold(value=value, unit="points")


def test_the_numeric_domain_is_decided_without_allocating_an_exponent():
    # A value whose fixed-point form would be a billion digits long is decided
    # from its own exponent and digit count, not by materializing the places
    # between them.
    started = time.monotonic()
    for value in ("1E+999999999", "-1E-999999999", "1E+1000000"):
        with pytest.raises(NumericDomainError):
            MarketThreshold(value=value, unit="points")
        with pytest.raises(NumericDomainError):
            comparisons.exact_difference(Decimal(value), Decimal("1"))

    assert time.monotonic() - started < 1.0


def test_an_exact_difference_ignores_ambient_precision_clamp_and_traps():
    minimum = Decimal(f"1E-{PLACE_LIMIT}")
    maximum = Decimal(f"1E+{PLACE_LIMIT}")
    expected = comparisons.exact_difference(maximum, minimum).as_tuple()
    signals = (
        Clamped,
        DivisionByZero,
        FloatOperation,
        Inexact,
        InvalidOperation,
        Overflow,
        Rounded,
        Subnormal,
        Underflow,
    )

    hostile = []
    for emax, emin in ((0, 0), (MAX_EMAX, MIN_EMIN)):
        with localcontext() as context:
            context.prec = 1
            context.Emax = emax
            context.Emin = emin
            context.clamp = 1
            context.capitals = 0
            context.rounding = ROUND_CEILING
            for signal in signals:
                context.traps[signal] = True
            hostile.append(comparisons.exact_difference(maximum, minimum).as_tuple())
            hostile.append(_summary(str(minimum), str(maximum)).threshold_spread.as_tuple())

    assert hostile == [expected] * 4


def _under_hostile_contexts(call):
    """Run one call inside every hostile ambient decimal context."""

    signals = (
        Clamped,
        DivisionByZero,
        FloatOperation,
        Inexact,
        InvalidOperation,
        Overflow,
        Rounded,
        Subnormal,
        Underflow,
    )
    results = []
    for emax, emin in ((0, 0), (MAX_EMAX, MIN_EMIN)):
        with localcontext() as context:
            context.prec = 1
            context.Emax = emax
            context.Emin = emin
            context.clamp = 1
            context.capitals = 0
            context.rounding = ROUND_CEILING
            for signal in signals:
                context.traps[signal] = True
            results.append(call())
    return results


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), "0"),
        (timedelta(seconds=300), "300"),
        (timedelta(seconds=300, microseconds=500000), "300.5"),
        (timedelta(days=7), "604800"),
        (timedelta(microseconds=1), "0.000001"),
        (timedelta(days=-1, microseconds=1), "-86399.999999"),
        (timedelta.max, "86399999999999.999999"),
        (timedelta.min, "-86399999913600"),
    ],
)
def test_exact_seconds_is_one_number_whatever_context_a_caller_is_inside(
    elapsed, expected
):
    value = comparisons.exact_seconds(elapsed)

    assert value == Decimal(expected)
    assert _under_hostile_contexts(
        lambda: comparisons.exact_seconds(elapsed).as_tuple()
    ) == [value.as_tuple()] * 2


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"max_age_seconds": Decimal("1200.000000")}, "1200"),
        ({"max_age_seconds": Decimal("1200"), "max_age_hours": 999.0}, "1200"),
        ({"max_age_hours": 72.0}, "259200"),
        ({"max_age_hours": 72.5}, "261000"),
        ({"max_age_hours": "0.5"}, "1800"),
        ({"freshness_days": 7}, "604800"),
        ({"freshness_days": "1.5"}, "129600"),
        ({}, None),
    ],
)
def test_a_configured_catalog_window_is_exact_in_any_ambient_context(
    document, expected
):
    freshness = {"season": SEASON, "is_fresh": True, **document}

    def read():
        value = comparison_board._catalog_max_age_seconds(freshness)
        return None if value is None else value.as_tuple()

    stated = read()

    if expected is None:
        assert stated is None
    else:
        assert Decimal(stated) == Decimal(expected)
    assert _under_hostile_contexts(read) == [stated] * 2


def test_a_board_states_the_same_freshness_arithmetic_in_any_ambient_context():
    # Nothing a board says about how old an observation or a catalog is may
    # depend on the decimal context the request happened to be served inside.
    def read():
        catalog = FakeCatalog(
            {
                "season": SEASON,
                "is_fresh": True,
                "max_age_hours": 72.5,
                "last_success_at": (
                    GENERATED_AT - timedelta(seconds=90, microseconds=500000)
                ).isoformat(),
            }
        )
        service, _ = _service(
            [
                _snapshot(
                    "dabble",
                    (_market(),),
                    retrieved_at=GENERATED_AT - timedelta(seconds=300, microseconds=500000),
                )
            ],
            athlete_catalog=catalog,
        )
        board = _read(service)
        entry = next(
            entry
            for entry in board.availability.catalogs
            if entry.catalog == "athlete_catalog"
        )
        observation = board.markets[0].observation
        return tuple(
            str(value)
            for value in (
                entry.age_seconds,
                entry.max_age_seconds,
                observation.age_seconds,
                observation.freshness,
                board.groups[0].reference,
            )
        )

    stated = read()

    assert _under_hostile_contexts(read) == [stated] * 2
    assert Decimal(stated[0]) == Decimal("90.5")
    assert Decimal(stated[1]) == Decimal(261000)
    assert Decimal(stated[2]) == Decimal("300.5")
    assert stated[3] == str(MarketFreshness.STALE)


def test_every_threshold_the_provider_contract_accepts_can_be_compared():
    # Nothing the normalized numeric boundary accepts may later refuse to
    # assemble into a stated comparison.
    accepted = tuple(
        MarketThreshold(value=value, unit="points").value
        for value in (
            "0",
            "25.5",
            "-25.5",
            f"1E+{PLACE_LIMIT}",
            f"-1E+{PLACE_LIMIT}",
            f"1E-{PLACE_LIMIT}",
            f"-1E-{PLACE_LIMIT}",
            "9" * (2 * PLACE_LIMIT + 1) + f"E-{PLACE_LIMIT}",
            "-" + "9" * (2 * PLACE_LIMIT + 1) + f"E-{PLACE_LIMIT}",
        )
    )

    for left in accepted:
        for right in accepted:
            summary = _summary(str(min(left, right)), str(max(left, right)))
            assert summary.threshold_spread == comparisons.exact_difference(
                max(left, right), min(left, right)
            )


def test_a_group_requires_deterministically_ordered_members():
    members = (_member(reference="mkt_1_b"), _member(reference="mkt_1_a"))
    with pytest.raises(ValueError, match="deterministically ordered"):
        ComparisonGroup(
            key=comparisons.ComparisonKey(
                canonical_event_id="0022500001",
                canonical_athlete_id=203999,
                canonical_statistic_id="points",
                scoring_period=ScoringPeriod.FULL_GAME,
            ),
            members=members,
            summary=ComparisonSummary.of(members),
        )


def test_an_unavailable_board_cannot_carry_groups():
    key = comparisons.ComparisonKey(
        canonical_event_id="0022500001",
        canonical_athlete_id=203999,
        canonical_statistic_id="points",
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    members = (_member(),)
    group = ComparisonGroup(key=key, members=members, summary=ComparisonSummary.of(members))
    with pytest.raises(ValueError, match="unavailable comparison board"):
        ComparisonBoard(
            season=SEASON,
            generated_at=GENERATED_AT,
            availability=comparisons.ComparisonAvailability(
                available=False,
                catalogs=(
                    comparisons.CatalogAvailability(
                        catalog="athlete_catalog",
                        season=SEASON,
                        available=False,
                        reason=CatalogAvailabilityReason.MISSING,
                    ),
                ),
            ),
            groups=(group,),
        )


def _available():
    return comparisons.ComparisonAvailability(
        available=True,
        catalogs=(
            comparisons.CatalogAvailability(
                catalog="athlete_catalog", season=SEASON, available=True
            ),
        ),
    )


def test_a_board_cannot_state_a_market_count_it_did_not_retain():
    """Every actual board agrees with itself: what it counted, it kept."""

    with pytest.raises(ValueError, match="retain every market it counted"):
        ComparisonBoard(
            season=SEASON,
            generated_at=GENERATED_AT,
            availability=_available(),
            market_count=2,
        )


def test_a_board_cannot_state_an_unresolved_count_it_did_not_retain():
    with pytest.raises(ValueError, match="retain every unresolved market it counted"):
        ComparisonBoard(
            season=SEASON,
            generated_at=GENERATED_AT,
            availability=_available(),
            unresolved_count=2,
        )


def test_a_board_counts_exactly_what_it_retained():
    unresolved = (
        comparisons.UnresolvedMarket(
            market_reference="ref-1",
            provider="dabble",
            reason=ComparisonExclusion.STALE_SNAPSHOT,
        ),
    )
    board = ComparisonBoard(
        season=SEASON,
        generated_at=GENERATED_AT,
        availability=_available(),
        unresolved=unresolved,
    )

    assert board.unresolved_count == 1
    assert board.market_count == 0
    assert board.is_empty is False


def test_a_board_retaining_a_market_is_never_empty():
    """Emptiness is read from everything the board kept, counts included."""

    service, _ = _service([_snapshot("dabble", (_market(market_id="m-1"),))])
    retained = _read(service).markets

    board = ComparisonBoard(
        season=SEASON,
        generated_at=GENERATED_AT,
        availability=_available(),
        markets=retained,
        market_count=len(retained),
    )

    assert retained
    assert board.is_empty is False


def test_an_empty_board_states_nothing_at_all():
    board = ComparisonBoard(
        season=SEASON, generated_at=GENERATED_AT, availability=_available()
    )

    assert board.is_empty is True
    assert board.market_count == 0
    assert board.unresolved_count == 0


def test_filters_normalize_and_reject_values_that_name_nothing():
    filters = ComparisonFilters(
        providers=("PrizePicks", " dabble ", "dabble"),
        canonical_statistic_ids=("points", "points", "assists"),
        market_statuses=("suspended", MarketStatus.AVAILABLE),
    )

    assert filters.providers == ("dabble", "prizepicks")
    assert filters.canonical_statistic_ids == ("assists", "points")
    assert filters.market_statuses == (MarketStatus.AVAILABLE, MarketStatus.SUSPENDED)
    assert not filters.is_empty
    assert ComparisonFilters().is_empty
    assert filters.supported_filters == SUPPORTED_NARROWING_FILTERS
    with pytest.raises(ValueError, match="non-empty name"):
        ComparisonFilters(providers=("",))
    with pytest.raises(ValueError, match="integer id"):
        ComparisonFilters(canonical_athlete_ids=("203999",))


def test_the_service_rejects_inputs_it_cannot_answer_for():
    service, _ = _service([_snapshot("dabble", ())])

    with pytest.raises(TypeError, match="NBAMarketQuery"):
        service.get_comparisons("2025-26")
    with pytest.raises(TypeError, match="ComparisonFilters"):
        service.get_comparisons(_query(), _context(), filters={"providers": ()})
    with pytest.raises(TypeError, match="board collector"):
        ComparisonBoardService(object())
    with pytest.raises(ValueError, match="positive integer"):
        ComparisonBoardService(service.board_service, max_markets=0)


def test_a_board_read_without_a_season_states_it_cannot_compare():
    service, _ = _service([_snapshot("dabble", (_market(),))])

    board = service.get_comparisons(NBAMarketQuery(), _context())

    assert not board.availability.available
    assert board.season is None
    assert {entry.reason for entry in board.availability.catalogs} == {
        CatalogAvailabilityReason.NOT_CONFIGURED
    }
    assert board.unresolved[0].reason is ComparisonExclusion.COMPARISON_UNAVAILABLE


# -- retained normalized evidence ------------------------------------------


def _rich_selection(**overrides):
    """One fully described selection, varied one fact at a time."""

    values = {
        "selection_id": "s-1",
        "label": "Higher",
        "direction": "higher",
        "status": "open",
        "modifiers": (
            SelectionModifier(value="1.25", kind="boost", scope="selection"),
        ),
        "american_price": -120,
        "decimal_price": "1.83",
    }
    return Selection(**{**values, **overrides})


def _rich_market():
    return dataclasses.replace(
        _market(selections=(_rich_selection(),)),
        team=TeamEvidence(provider_id="t-1", name="Denver Nuggets", abbreviation="den"),
        opponent=TeamEvidence(abbreviation="lal"),
        league=LeagueEvidence(provider_id="l-1", canonical_id="nba", label="NBA"),
        competition=CompetitionEvidence(
            provider_id="c-1", label="Regular Season", sport=SportEvidence(label="Basketball")
        ),
        sport=SportEvidence(provider_id="sp-1", label="Basketball"),
        appearance=AppearanceEvidence(provider_id="ap-1", appearance_type="starter"),
        status_label="OPEN",
        variant_label="Standard",
        scoring_period_label="Full Game",
    )


def test_the_board_retains_every_normalized_market_with_its_typed_evidence():
    service, _ = _service([_snapshot("dabble", (_rich_market(),))])

    board = _read(service)
    group = board.groups[0]
    retained = board.markets_by_reference[group.members[0].market_reference]

    assert board.markets_for(group.reference) == (retained,)
    assert retained.comparison_reference == group.reference
    assert retained.exclusion is None
    assert (retained.athlete.provider_id, retained.athlete.name) == ("a-1", "Nikola Jokic")
    assert retained.event.label == "DEN @ LAL"
    assert retained.event.home_team.canonical_id == 1610612747
    assert retained.event.away_team.canonical_id == 1610612743
    assert retained.team.abbreviation == "DEN"
    assert retained.opponent.abbreviation == "LAL"
    assert retained.league.label == "NBA"
    assert retained.competition.sport.label == "Basketball"
    assert retained.sport.provider_id == "sp-1"
    assert retained.appearance.appearance_type == "starter"
    assert retained.statistic.label == "points"
    assert retained.statistic_resolution.canonical_id == "points"
    assert retained.statistic_resolution.state is MatchState.CANONICAL
    assert retained.statistic_resolution.comparable
    assert retained.threshold.value == Decimal("25.5")
    assert retained.threshold.unit == "count"
    assert (retained.status, retained.status_label) == (MarketStatus.AVAILABLE, "OPEN")
    assert (retained.variant, retained.variant_label) == (MarketVariant.STANDARD, "Standard")
    assert retained.scoring_period is ScoringPeriod.FULL_GAME
    assert retained.scoring_period_label == "Full Game"


def test_a_retained_market_keeps_every_offered_selection_and_its_prices():
    service, _ = _service([_snapshot("dabble", (_rich_market(),))])

    retained = _read(service).markets[0]
    selection = retained.selections[0]

    assert selection.selection_reference == selection_reference(
        retained.market_reference, _rich_selection()
    )
    assert (selection.selection_id, selection.label) == ("s-1", "Higher")
    assert (selection.direction, selection.direction_label) == ("higher", "higher")
    assert selection.status == "open"
    assert selection.american_price == -120
    assert selection.decimal_price == Decimal("1.83")
    assert selection.modifiers[0].value == Decimal("1.25")
    assert (selection.modifiers[0].kind, selection.modifiers[0].scope) == (
        "boost",
        "selection",
    )


def test_a_retained_market_states_its_snapshot_and_provider_freshness():
    service, _ = _service([_snapshot("dabble", (_market(),))])

    retained = _read(service).markets[0]

    assert retained.observation.provider == "dabble"
    assert retained.observation.snapshot_status == "complete"
    assert retained.observation.retrieved_at == RETRIEVED_AT
    assert retained.observation.observed_at == GENERATED_AT
    assert retained.observation.age_seconds == Decimal(30)
    assert retained.observation.freshness is MarketFreshness.FRESH
    assert not retained.observation.is_future


@pytest.mark.parametrize(
    ("markets", "kwargs", "reason"),
    [
        ((_market(statistic="Fantasy Score"),), {}, ComparisonExclusion.UNMAPPED_STATISTIC),
        ((_market(),), {"athlete_states": {"a-1": "ambiguous"}}, ComparisonExclusion.UNRESOLVED_ATHLETE),
        ((_market(),), {"event_states": {"e-1": "ambiguous"}}, ComparisonExclusion.UNRESOLVED_EVENT),
    ],
)
def test_an_unresolved_market_stays_auditable_rather_than_an_opaque_reference(
    markets, kwargs, reason
):
    service, _ = _service([_snapshot("dabble", markets)], **kwargs)

    board = _read(service)
    entry = board.unresolved[0]
    retained = board.markets_by_reference[entry.market_reference]

    assert entry.reason is reason
    assert retained.exclusion is reason
    assert retained.exclusion_detail == entry.detail
    assert retained.comparison_reference is None
    assert retained.athlete.name == "Nikola Jokic"
    assert retained.event.label == "DEN @ LAL"
    assert retained.statistic.label == markets[0].statistic.label
    assert retained.threshold.value == Decimal("25.5")
    assert retained.observation.freshness is MarketFreshness.FRESH


def test_a_market_past_the_stale_window_is_retained_with_its_exact_age():
    beyond = _snapshot(
        "dabble",
        (_market(),),
        retrieved_at=GENERATED_AT - timedelta(seconds=3600),
    )
    service, _ = _service([beyond])

    board = _read(service)
    retained = board.markets[0]

    assert board.groups == ()
    assert retained.exclusion is ComparisonExclusion.STALE_SNAPSHOT
    assert retained.observation.freshness is None
    assert retained.observation.age_seconds == Decimal(3600)
    assert retained.threshold.value == Decimal("25.5")


def test_markets_stay_retained_while_a_catalog_cannot_support_comparison():
    service, _ = _service(
        [_snapshot("dabble", (_market(),))],
        athlete_catalog=_athlete_catalog(fresh=False),
    )

    board = _read(service)
    retained = board.markets[0]

    assert not board.availability.available
    assert retained.exclusion is ComparisonExclusion.COMPARISON_UNAVAILABLE
    assert retained.exclusion_detail == CatalogAvailabilityReason.STALE.value
    assert retained.athlete.provider_id == "a-1"
    assert retained.statistic_resolution.canonical_id == "points"


# -- canonical injective references ----------------------------------------


def test_a_separator_in_one_field_cannot_forge_another_market_identity():
    # Under delimiter-joined hashing both of these joined to the same payload.
    one = _market(market_id=None, athlete_name="A\x1fB", event_id="C")
    two = _market(market_id=None, athlete_name="A", event_id="B\x1fC")
    provider_one = _market(provider="dabble\x1fa", market_id="b")
    provider_two = _market(provider="dabble", market_id="a\x1fb")

    assert market_reference(one) != market_reference(two)
    assert market_reference(provider_one) != market_reference(provider_two)


def test_a_separator_in_a_modifier_cannot_forge_another_selection_identity():
    one = _rich_selection(
        modifiers=(SelectionModifier(value="1.25", kind="a\x1fb", scope="c"),)
    )
    two = _rich_selection(
        modifiers=(SelectionModifier(value="1.25", kind="a", scope="b\x1fc"),)
    )

    assert selection_reference("mkt", one) != selection_reference("mkt", two)


def test_an_exact_decimal_is_canonical_whatever_scale_it_was_written_in():
    assert canonical_decimal(Decimal("25.50")) == canonical_decimal(Decimal("25.5"))
    assert canonical_decimal(Decimal("25.5")) == "255E1"
    assert canonical_decimal(Decimal("0.00")) == "0"
    assert canonical_decimal(Decimal("-0")) == "0"
    assert canonical_decimal(Decimal("0E+100")) == "0"
    assert canonical_decimal(Decimal("1E+2")) == canonical_decimal(Decimal("100.00"))
    assert canonical_decimal(Decimal("-25.5")) == "-255E1"
    with pytest.raises(ValueError, match="finite Decimal"):
        canonical_decimal(Decimal("NaN"))


def test_a_canonical_decimal_separates_every_distinct_finite_value():
    values = (
        "0",
        "1",
        "-1",
        "10",
        "0.1",
        "1.01",
        "25.5",
        "25.05",
        "255",
        "1E+2",
        f"1E+{PLACE_LIMIT}",
        f"1E-{PLACE_LIMIT}",
        f"-1E+{PLACE_LIMIT}",
    )
    encodings = {canonical_decimal(Decimal(value)) for value in values}

    assert len(encodings) == len(values)


def test_a_canonical_decimal_is_bounded_however_large_its_exponent():
    # A fixed-point rendering of the highest and lowest places the normalized
    # domain admits would be hundreds of characters long.
    for value in (
        f"1E+{PLACE_LIMIT}",
        f"-1E+{PLACE_LIMIT}",
        f"1.5E-{PLACE_LIMIT - 1}",
    ):
        decimal = MarketThreshold(value=value, unit="points").value
        assert len(canonical_decimal(decimal)) < 32


def test_a_reference_does_not_depend_on_the_scale_a_number_was_written_in():
    assert market_reference(_market(market_id=None, threshold="25.5")) == market_reference(
        _market(market_id=None, threshold="25.50")
    )
    assert market_reference(_market(market_id=None, threshold="25.5")) != market_reference(
        _market(market_id=None, threshold="25.05")
    )
    assert selection_reference("mkt", _rich_selection(decimal_price="1.9")) == (
        selection_reference("mkt", _rich_selection(decimal_price="1.90"))
    )
    assert selection_reference("mkt", _rich_selection(decimal_price="1.9")) != (
        selection_reference("mkt", _rich_selection(decimal_price="1.09"))
    )


def test_a_canonical_decimal_is_exact_beyond_the_ambient_context_precision():
    # Thirty-two significant digits: an ambient-context rounding would make
    # these two distinct numbers one.
    fine = Decimal("1." + "0" * 30 + "1")
    finer = Decimal("1." + "0" * 30 + "2")

    assert canonical_decimal(fine) == "1" + "0" * 30 + "1E0"
    assert canonical_decimal(fine) != canonical_decimal(finer)
    assert market_reference(
        _market(market_id=None, threshold=str(fine))
    ) != market_reference(_market(market_id=None, threshold=str(finer)))


def test_a_canonical_decimal_does_not_depend_on_the_ambient_decimal_context():
    value = Decimal("1." + "0" * 30 + "1")
    trailing = Decimal("25.500000")

    with localcontext() as context:
        context.prec = 5
        assert canonical_decimal(value) == "1" + "0" * 30 + "1E0"
        assert canonical_decimal(trailing) == canonical_decimal(Decimal("25.5"))
        assert canonical_decimal(Decimal("123456789")) == "123456789E8"
        narrow = market_reference(_market(market_id=None, threshold=str(value)))

    assert narrow == market_reference(_market(market_id=None, threshold=str(value)))


def test_the_written_spelling_of_a_threshold_never_changes_market_identity():
    def market(value, original):
        return dataclasses.replace(
            _market(market_id=None),
            threshold=MarketThreshold(value, "count", original_value=original),
        )

    plain = market("25.5", "25.5")
    padded = market("25.50", "25.50")

    assert market_reference(plain) == market_reference(padded)
    assert market_reference(plain) != market_reference(market("25.05", "25.05"))

    service, _ = _service([_snapshot("dabble", (padded,))])
    retained = _read(service).markets[0]

    assert retained.threshold.original_value == "25.50"
    assert retained.threshold.value == Decimal("25.50")


def test_the_order_selections_arrive_in_never_changes_a_market_or_its_board():
    first = _rich_selection(selection_id="s-1", decimal_price="1.83")
    second = _rich_selection(selection_id="s-2", decimal_price="1.91")
    forward = _market(market_id=None, selections=(first, second))
    reversed_market = _market(market_id=None, selections=(second, first))

    assert market_reference(forward) == market_reference(reversed_market)

    def retained(market):
        service, _ = _service([_snapshot("dabble", (market,))])
        board = _read(service)
        return board.markets[0]

    assert retained(forward).selections == retained(reversed_market).selections
    assert len(retained(forward).selections) == 2
    assert {selection.selection_id for selection in retained(forward).selections} == {
        "s-1",
        "s-2",
    }
    assert market_reference(
        _market(market_id=None, selections=(first, first))
    ) != market_reference(forward)


def test_id_less_markets_of_different_events_never_share_a_reference():
    base = EventEvidence(
        label="DEN @ LAL",
        starts_at="2026-08-10T00:00:00+00:00",
        ends_at="2026-08-10T02:30:00+00:00",
        updated_at="2026-08-09T19:00:00+00:00",
        home_team=TeamEvidence(provider_id="h-1", canonical_id=1610612747, name="Lakers", abbreviation="lal"),
        away_team=TeamEvidence(provider_id="a-1", canonical_id=1610612743, name="Nuggets", abbreviation="den"),
        status_label="scheduled",
    )
    events = (
        base,
        dataclasses.replace(base, canonical_id="0022500001"),
        dataclasses.replace(base, label="LAL vs DEN"),
        dataclasses.replace(base, starts_at="2026-08-10T00:30:00+00:00"),
        dataclasses.replace(base, ends_at="2026-08-10T03:00:00+00:00"),
        dataclasses.replace(base, updated_at="2026-08-09T19:30:00+00:00"),
        dataclasses.replace(base, status_label="delayed"),
        dataclasses.replace(base, home_team=dataclasses.replace(base.home_team, canonical_id=1610612744)),
        dataclasses.replace(base, home_team=dataclasses.replace(base.home_team, name="Los Angeles Lakers")),
        dataclasses.replace(base, home_team=dataclasses.replace(base.home_team, abbreviation="lak")),
        dataclasses.replace(base, home_team=dataclasses.replace(base.home_team, provider_id="h-2")),
        dataclasses.replace(base, away_team=dataclasses.replace(base.away_team, canonical_id=1610612745)),
    )
    references = {
        market_reference(dataclasses.replace(_market(market_id=None), event=event))
        for event in events
    }

    assert len(references) == len(events)


def test_id_less_markets_of_different_source_identity_never_share_a_reference():
    base = _market(market_id=None)
    variants = (
        base,
        dataclasses.replace(base, athlete=AthleteEvidence(provider_id="a-2", name="Nikola Jokic")),
        dataclasses.replace(base, athlete=AthleteEvidence(provider_id="a-1", name="Jamal Murray")),
        dataclasses.replace(
            base, athlete=AthleteEvidence(provider_id="a-1", name="Nikola Jokic", canonical_id=203999)
        ),
        dataclasses.replace(base, statistic=StatisticEvidence(label="points", provider_id="p-9")),
        dataclasses.replace(base, statistic=StatisticEvidence(label="points", components=("points",))),
        dataclasses.replace(base, threshold=MarketThreshold("25.5", "points")),
        dataclasses.replace(base, variant=MarketVariant.ALTERNATE),
        dataclasses.replace(base, scoring_period=ScoringPeriod.FIRST_HALF),
        dataclasses.replace(base, starts_at="2026-08-10T00:00:00+00:00"),
        dataclasses.replace(base, updated_at="2026-08-09T19:00:00+00:00"),
        dataclasses.replace(base, team=TeamEvidence(abbreviation="den")),
        dataclasses.replace(base, opponent=TeamEvidence(abbreviation="lal")),
        dataclasses.replace(base, league=LeagueEvidence(label="NBA")),
        dataclasses.replace(base, sport=SportEvidence(label="Basketball")),
        dataclasses.replace(base, competition=CompetitionEvidence(provider_id="c-1")),
        dataclasses.replace(base, appearance=AppearanceEvidence(provider_id="ap-1")),
        dataclasses.replace(base, selections=(_rich_selection(),)),
    )

    assert len({market_reference(variant) for variant in variants}) == len(variants)


def test_selection_references_separate_every_distinct_offering():
    variants = (
        _rich_selection(),
        _rich_selection(selection_id="s-2"),
        _rich_selection(label="Higher than"),
        _rich_selection(direction="lower"),
        _rich_selection(direction="higher", direction_label="Over"),
        _rich_selection(status="suspended"),
        _rich_selection(american_price=-125),
        _rich_selection(decimal_price="1.91"),
        _rich_selection(modifiers=()),
        _rich_selection(
            modifiers=(SelectionModifier(value="1.5", kind="boost", scope="selection"),)
        ),
        _rich_selection(
            modifiers=(SelectionModifier(value="1.25", kind="boost", scope="entry"),)
        ),
        _rich_selection(
            modifiers=(
                SelectionModifier(value="1.25", kind="boost", scope="selection", label="Boosted"),
            )
        ),
    )

    references = {selection_reference("mkt", variant) for variant in variants}

    assert len(references) == len(variants)


def test_id_less_offerings_that_differ_only_in_price_stay_distinct_members():
    markets = (
        _market(market_id=None, selections=(_rich_selection(decimal_price="1.83"),)),
        _market(market_id=None, selections=(_rich_selection(decimal_price="1.91"),)),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert board.market_count == 2
    assert board.groups[0].summary.market_count == 2
    assert len({market.market_reference for market in board.markets}) == 2


def test_exact_repeated_id_less_markets_are_one_offering():
    repeated = _market(market_id=None)
    service, _ = _service([_snapshot("dabble", (repeated, repeated))])

    board = _read(service)

    assert board.market_count == 1
    assert board.groups[0].summary.market_count == 1
    assert len(board.markets) == 1


def _spelled(threshold, original, **overrides):
    """One market whose threshold carries the exact text the provider wrote."""

    return dataclasses.replace(
        _market(market_id=None, **overrides),
        threshold=MarketThreshold(threshold, "count", original),
    )


def _read_markets(markets):
    service, _ = _service([_snapshot("dabble", markets)])
    return _read(service)


def test_selections_listed_in_either_order_are_one_semantic_repeat():
    # Listing order is not a fact about an offering, so a provider that repeats
    # one market with its selections the other way round has not contradicted
    # itself.
    higher = _rich_selection(selection_id="s-1", label="Higher", direction="higher")
    lower = _rich_selection(selection_id="s-2", label="Lower", direction="lower")

    board = _read_markets(
        (
            _market(market_id=None, selections=(higher, lower)),
            _market(market_id=None, selections=(lower, higher)),
        )
    )

    assert board.market_count == 1
    assert board.markets[0].conflict_ordinal is None
    assert board.markets[0].conflict_count is None
    assert board.groups[0].summary.market_count == 1


def test_one_threshold_written_at_two_scales_is_one_semantic_repeat():
    # 25.50 and 25.5 are the same line, whatever the provider wrote them as.
    written = _spelled("25.5", "25.5")
    rewritten = _spelled("25.50", "25.50")

    forward = _read_markets((written, rewritten))
    backward = _read_markets((rewritten, written))

    assert forward.market_count == 1
    assert forward.markets[0].conflict_ordinal is None
    assert forward.groups[0].summary.market_count == 1
    # The one retained spelling is chosen by content, not by arrival order.
    assert forward.markets == backward.markets


def _scaled_selection(price, modifier):
    """One selection whose exact prices carry the scale the provider wrote."""

    return _rich_selection(
        decimal_price=price,
        modifiers=(SelectionModifier(value=modifier, kind="boost", scope="selection"),),
    )


def _written_prices(selections):
    return [
        (
            selection.decimal_price.as_tuple(),
            tuple(modifier.value.as_tuple() for modifier in selection.modifiers),
        )
        for selection in selections
    ]


def test_selections_of_one_semantic_offering_are_ordered_by_retained_audit_facts():
    # Both selections state the same offering; only the scale each exact price
    # was written at tells them apart, and that is a retained audit fact.
    plain = _scaled_selection("1.9", "1.25")
    padded = _scaled_selection("1.90", "1.250")

    forward = canonical_selections((plain, padded))
    backward = canonical_selections((padded, plain))

    assert len(forward) == 2
    assert _written_prices(forward) == _written_prices(backward)
    assert market_evidence_key(
        _market(market_id=None, selections=(plain, padded))
    ) == market_evidence_key(_market(market_id=None, selections=(padded, plain)))


def test_the_scale_a_price_was_written_at_never_changes_market_identity():
    plain = _scaled_selection("1.9", "1.25")
    padded = _scaled_selection("1.90", "1.250")

    assert market_reference(
        _market(market_id=None, selections=(plain,))
    ) == market_reference(_market(market_id=None, selections=(padded,)))
    assert market_reference(
        _market(market_id=None, selections=(plain, padded))
    ) == market_reference(_market(market_id=None, selections=(padded, plain)))


def test_a_boards_retained_selections_do_not_depend_on_their_arrival_order():
    plain = _scaled_selection("1.9", "1.25")
    padded = _scaled_selection("1.90", "1.250")

    def retained(selections):
        board = _read_markets((_market(market_id=None, selections=selections),))
        return board.markets[0].selections

    forward = retained((plain, padded))
    backward = retained((padded, plain))

    assert len(forward) == 2
    assert _written_prices(forward) == _written_prices(backward)
    assert [entry.selection_reference for entry in forward] == [
        entry.selection_reference for entry in backward
    ]


def test_selections_that_differ_in_a_stated_fact_are_never_collapsed():
    variants = (
        _rich_selection(selection_id="s-1"),
        _rich_selection(selection_id="s-2"),
        _rich_selection(selection_id="s-1", decimal_price="1.91"),
        _rich_selection(selection_id="s-1", status="suspended"),
        _rich_selection(selection_id="s-1", modifiers=()),
    )

    assert len(canonical_selections(variants)) == len(variants)


def test_a_contradiction_among_semantic_repeats_does_not_depend_on_input_order(
    monkeypatch,
):
    _forced_reference(monkeypatch)
    written = _spelled("25.5", "25.5")
    rewritten = _spelled("25.50", "25.50")
    contradicting = _spelled("27.5", "27.5")

    forward = _read_markets((written, rewritten, contradicting))
    backward = _read_markets((contradicting, rewritten, written))

    assert forward.market_count == 2
    assert [market.conflict_ordinal for market in forward.markets] == [0, 1]
    assert {market.conflict_count for market in forward.markets} == {2}
    assert forward.markets == backward.markets
    assert forward.unresolved == backward.unresolved


def _forced_reference(monkeypatch, reference="mkt_2_forced"):
    """Collapse every market onto one reference, whatever facts it reports.

    Two distinct normalized markets never share a derived reference, so the
    contradiction the board must survive is provoked at the seam that derives
    it rather than by inventing an impossible market.
    """

    monkeypatch.setattr(
        comparison_board, "market_reference", lambda market: reference
    )
    return reference


def test_a_repeated_identity_whose_content_disagrees_stays_unresolved(monkeypatch):
    reference = _forced_reference(monkeypatch)
    markets = (
        _market(market_id="m-1", threshold="25.5"),
        _market(market_id="m-2", threshold="27.5"),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert board.groups == ()
    assert [entry.reason for entry in board.unresolved] == [
        ComparisonExclusion.CONFLICTING_MARKET_IDENTITY
    ]
    assert board.unresolved[0].market_reference == reference
    assert {market.exclusion for market in board.markets} == {
        ComparisonExclusion.CONFLICTING_MARKET_IDENTITY
    }
    assert board.markets[0].exclusion_detail == "conflicting_normalized_content"


def test_every_contradicting_observation_of_one_reference_is_retained(monkeypatch):
    _forced_reference(monkeypatch)
    markets = (
        _market(market_id="m-1", threshold="25.5"),
        _market(market_id="m-2", threshold="27.5"),
    )
    service, _ = _service([_snapshot("dabble", markets)])

    board = _read(service)

    assert board.market_count == 2
    assert len(board.markets) == 2
    assert len(board.conflicting_markets) == 2
    assert [market.conflict_ordinal for market in board.markets] == [0, 1]
    assert {market.conflict_count for market in board.markets} == {2}
    assert {market.threshold.value for market in board.markets} == {
        Decimal("25.5"),
        Decimal("27.5"),
    }


def test_a_contradicted_reference_does_not_depend_on_input_order(monkeypatch):
    _forced_reference(monkeypatch)

    def read(markets):
        service, _ = _service([_snapshot("dabble", markets)])
        return _read(service)

    markets = (
        _market(market_id="m-1", threshold="25.5", status=MarketStatus.SUSPENDED),
        _market(market_id="m-2", threshold="27.5"),
    )

    forward = read(markets)
    reversed_read = read(tuple(reversed(markets)))

    assert forward.markets == reversed_read.markets
    assert forward.unresolved == reversed_read.unresolved
    assert forward.market_count == reversed_read.market_count == 2


class _CanonicalStatistic:
    """One reviewed canonical statistic, stated as the catalog contract reads it."""

    def __init__(self, comparable):
        self.id = "points"
        self.components = ("points",)
        self.comparable = comparable


class _AlternatingStatisticResolver:
    """Resolve successive markets to the comparabilities given, in order.

    A reviewed catalog cannot resolve one market's own evidence two ways, so
    the disagreement the board must survive is provoked at the seam that states
    it -- exactly as a forced reference provokes a contradicted identity.
    """

    def __init__(self, comparabilities):
        self.comparabilities = tuple(comparabilities)
        self.calls = 0

    def resolve_market(self, market):
        comparable = self.comparabilities[self.calls % len(self.comparabilities)]
        self.calls += 1
        return StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=market.statistic,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=_CanonicalStatistic(comparable),
            provider=market.provider,
        )


def _comparability_board(comparabilities):
    """One board reading the same offering twice, resolved in the order given."""

    repeated = _market(market_id=None)
    service, _ = _service([_snapshot("dabble", (repeated, repeated))])
    service.board_service.statistic_resolver = _AlternatingStatisticResolver(
        comparabilities
    )
    return _read(service)


def test_two_readings_of_one_offering_that_agree_on_comparability_are_one():
    board = _comparability_board((True, True))

    assert board.market_count == 1
    assert board.markets[0].conflict_ordinal is None
    assert board.groups[0].summary.market_count == 1


def test_readings_that_disagree_on_comparability_conflict_in_either_order():
    forward = _comparability_board((True, False))
    backward = _comparability_board((False, True))

    assert forward.groups == () and backward.groups == ()
    assert [entry.reason for entry in forward.unresolved] == [
        ComparisonExclusion.CONFLICTING_MARKET_IDENTITY
    ]
    assert {market.exclusion for market in forward.markets} == {
        ComparisonExclusion.CONFLICTING_MARKET_IDENTITY
    }
    assert {
        market.statistic_resolution.comparable for market in forward.markets
    } == {True, False}
    # Neither reading wins by arriving first, and both stay auditable.
    assert forward.markets == backward.markets
    assert forward.unresolved == backward.unresolved
    assert forward.market_count == backward.market_count == 2


def test_exact_repeats_of_one_reference_collapse_to_one_observation(monkeypatch):
    _forced_reference(monkeypatch)
    repeated = _market(market_id=None)
    service, _ = _service([_snapshot("dabble", (repeated, repeated))])

    board = _read(service)

    assert board.market_count == 1
    assert board.markets[0].conflict_ordinal is None
    assert board.markets[0].conflict_count is None
    assert board.groups[0].summary.market_count == 1


# -- one observation timestamp ---------------------------------------------


def test_the_observation_timestamp_is_taken_after_the_collector_returns():
    # The collector stamped its board at GENERATED_AT; this read only finished
    # ten minutes later, and every age states that.
    observed_at = GENERATED_AT + timedelta(seconds=600)
    service, _ = _service([_snapshot("dabble", (_market(),))], observed_at=observed_at)

    board = _read(service)

    assert board.generated_at == observed_at
    assert board.markets[0].observation.age_seconds == Decimal(630)
    assert board.markets[0].observation.freshness is MarketFreshness.STALE
    assert board.groups[0].members[0].freshness is MarketFreshness.STALE
    assert board.provider_reports[0].age_seconds == Decimal(630)
    assert board.provider_reports[0].freshness is MarketFreshness.STALE
    assert board.provider_reports[0].snapshot_status == "complete"


def test_both_catalogs_are_aged_against_the_same_observation():
    athlete_catalog = _athlete_catalog()
    event_catalog = _event_catalog()
    service, _ = _service(
        [_snapshot("dabble", (_market(),))],
        athlete_catalog=athlete_catalog,
        event_catalog=event_catalog,
        observed_at=GENERATED_AT + timedelta(seconds=600),
    )

    board = _read(service)

    assert athlete_catalog.observed == [board.generated_at]
    assert event_catalog.observed == [board.generated_at]
    ages = {entry.catalog: entry.age_seconds for entry in board.availability.catalogs}
    assert ages == {
        "athlete_catalog": Decimal(29430),
        "event_catalog": Decimal(29430),
    }


@pytest.mark.parametrize("elapsed_seconds", [604800, 604801])
def test_a_catalog_age_and_its_availability_agree_at_the_ttl_boundary(elapsed_seconds):
    ttl = 604800
    last_success = GENERATED_AT - timedelta(seconds=elapsed_seconds)
    catalog = FakeCatalog(
        {
            "season": SEASON,
            "is_fresh": True,
            "freshness_days": 7,
            "last_success_at": last_success.isoformat(),
        },
        ttl_seconds=ttl,
    )
    service, _ = _service(
        [_snapshot("dabble", (_market(),))], athlete_catalog=catalog
    )

    board = _read(service)
    entry = next(
        entry for entry in board.availability.catalogs if entry.catalog == "athlete_catalog"
    )

    assert entry.age_seconds == Decimal(elapsed_seconds)
    assert entry.available is (elapsed_seconds <= ttl)
    assert entry.max_age_seconds == Decimal(ttl)


@pytest.mark.parametrize(
    ("elapsed_seconds", "freshness"),
    [
        ("299.999999", MarketFreshness.FRESH),
        (300, MarketFreshness.STALE),
        ("300.000001", MarketFreshness.STALE),
        (1800, MarketFreshness.STALE),
        ("1800.000001", None),
    ],
)
def test_freshness_windows_are_exact_at_their_boundaries(elapsed_seconds, freshness):
    elapsed = Decimal(str(elapsed_seconds))
    snapshot = _snapshot(
        "dabble",
        (_market(),),
        retrieved_at=GENERATED_AT - timedelta(seconds=float(elapsed)),
    )
    service, _ = _service([snapshot])

    board = _read(service)
    retained = board.markets[0]

    assert retained.observation.age_seconds == elapsed
    assert retained.observation.freshness is freshness
    if freshness is None:
        assert retained.exclusion is ComparisonExclusion.STALE_SNAPSHOT
    else:
        assert retained.comparison_reference == board.groups[0].reference


def test_a_slow_collection_never_reports_a_market_as_fresher_than_the_board():
    def read(observed_at):
        service, _ = _service(
            [_snapshot("dabble", (_market(),))], observed_at=observed_at
        )
        return _read(service)

    prompt = read(GENERATED_AT)
    slow = read(GENERATED_AT + timedelta(seconds=600))

    assert prompt.markets[0].observation.age_seconds < slow.markets[0].observation.age_seconds
    assert prompt.generated_at < slow.generated_at


def test_a_future_snapshot_fails_closed_without_a_negative_age():
    ahead = _snapshot(
        "dabble", (_market(),), retrieved_at=GENERATED_AT + timedelta(seconds=60)
    )
    service, _ = _service([ahead])

    board = _read(service)
    retained = board.markets[0]
    report = board.provider_reports[0]

    assert board.groups == ()
    assert [entry.reason for entry in board.unresolved] == [
        ComparisonExclusion.FUTURE_SNAPSHOT
    ]
    assert retained.observation.is_future
    assert retained.observation.age_seconds == Decimal(0)
    assert retained.observation.freshness is None
    assert report.future_observation
    assert report.age_seconds == Decimal(0)
    assert report.freshness is None


def test_a_naive_observation_clock_is_refused():
    service, _ = _service([_snapshot("dabble", (_market(),))])
    service.clock = lambda: datetime(2026, 8, 9, 20, 0)

    with pytest.raises(ValueError, match="aware datetime"):
        _read(service)


# -- provider and cache provenance -----------------------------------------


class FakeCollector:
    """A collector seam returning one hand-built board read."""

    def __init__(self, board):
        self.board = board

    def get_board(self, query, context=None, *, providers=None):
        return self.board


def _outcome_service(outcome):
    board = DFSBoard(
        query=_query(),
        provider_outcomes=(outcome,),
        generated_at=GENERATED_AT,
    )
    return ComparisonBoardService(
        FakeCollector(board),
        athlete_catalog=_athlete_catalog(),
        event_catalog=_event_catalog(),
        clock=lambda: GENERATED_AT,
    )


def test_a_provider_report_states_its_complete_coverage_evidence():
    snapshot = _snapshot(
        "dabble",
        (_market(),),
        coverage=CoverageEvidence(
            fetched_count=9,
            eligible_count=7,
            normalized_count=5,
            skipped_count=2,
            pagination_complete=True,
            fanout_complete=True,
            expected_total=9,
            skipped_reasons=("non_player_market", "ineligible_status"),
        ),
    )
    outcome = ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.COMPLETE,
        snapshot=snapshot,
        cache_status="hit",
        cache_retrieved_at=RETRIEVED_AT,
        cache_age_seconds=30.5,
    )

    report = _outcome_service(outcome).get_comparisons(_query(), _context()).provider_reports[0]

    assert report.coverage.fetched_count == 9
    assert report.coverage.eligible_count == 7
    assert report.coverage.normalized_count == 5
    assert report.coverage.skipped_count == 2
    assert report.coverage.expected_total == 9
    assert report.coverage.pagination_complete is True
    assert report.coverage.fanout_complete is True
    assert report.coverage.is_complete is True
    assert report.coverage.skipped_reasons == ("ineligible_status", "non_player_market")
    assert report.coverage.warning_codes == ()
    assert report.cache.status == "hit"
    assert report.cache.retrieved_at == RETRIEVED_AT
    assert report.cache.age_seconds == Decimal("30.5")
    assert report.cache.failure_reason is None


def test_a_provider_report_states_partial_completion_evidence():
    snapshot = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.PARTIAL,
        markets=(_market(),),
        coverage=CoverageEvidence(
            fetched_count=3,
            eligible_count=3,
            normalized_count=1,
            pagination_complete=False,
            expected_total=8,
            warning_codes=("page_fetch_failed",),
        ),
        retrieved_at=RETRIEVED_AT,
    )
    outcome = ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.PARTIAL,
        snapshot=snapshot,
        reason=ProviderFailureReason.UPSTREAM_ERROR,
    )

    report = _outcome_service(outcome).get_comparisons(_query(), _context()).provider_reports[0]

    assert report.status == "partial"
    assert report.reason == "upstream_error"
    assert report.coverage.pagination_complete is False
    assert report.coverage.fanout_complete is None
    assert report.coverage.is_complete is False
    assert report.coverage.expected_total == 8
    assert report.coverage.warning_codes == ("page_fetch_failed",)
    assert report.warning_codes == ("page_fetch_failed",)
    assert report.cache is None


def test_a_provider_report_states_a_stale_cache_and_its_refresh_failure():
    failed_at = RETRIEVED_AT + timedelta(seconds=10)
    outcome = ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.COMPLETE,
        snapshot=_snapshot("dabble", (_market(),)),
        cache_status="stale",
        cache_retrieved_at=RETRIEVED_AT,
        cache_age_seconds=1200.25,
        cache_failure_reason="deadline_exceeded",
        cache_failure_at=failed_at,
    )

    report = _outcome_service(outcome).get_comparisons(_query(), _context()).provider_reports[0]

    assert report.cache.status == "stale"
    assert report.cache.age_seconds == Decimal("1200.25")
    assert report.cache.failure_reason == "deadline_exceeded"
    assert report.cache.failure_at == failed_at


def test_a_failed_provider_report_states_its_cache_state_without_a_snapshot():
    outcome = ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.FAILED,
        reason=ProviderFailureReason.TIMEOUT,
        cache_status="error",
        cache_failure_reason="timeout",
    )

    report = _outcome_service(outcome).get_comparisons(_query(), _context()).provider_reports[0]

    assert report.status == "failed"
    assert report.coverage is None
    assert report.cache.status == "error"
    assert report.cache.failure_reason == "timeout"
    assert report.market_count == 0


def test_provider_reports_serialize_deterministically():
    outcome = ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.COMPLETE,
        snapshot=_snapshot("dabble", (_market(),)),
        cache_status="hit",
        cache_retrieved_at=RETRIEVED_AT,
        cache_age_seconds=30.5,
    )
    service = _outcome_service(outcome)

    first = service.get_comparisons(_query(), _context()).provider_reports
    second = service.get_comparisons(_query(), _context()).provider_reports

    assert first == second
    assert dataclasses.asdict(first[0]) == dataclasses.asdict(second[0])
    assert {field.name for field in fields(comparisons.ProviderReport)} == {
        "provider",
        "status",
        "reason",
        "retrieved_at",
        "age_seconds",
        "freshness",
        "market_count",
        "warning_codes",
        "snapshot_status",
        "future_observation",
        "coverage",
        "cache",
    }


def test_one_boundary_decides_the_cache_and_the_comparison_alike():
    """A window's endpoint is outside it at every seam that reads the window.

    The provider cache serves an observation exactly one fresh window old as a
    miss rather than a hit, so the board must not state the same observation as
    a fresh comparison member.
    """

    endpoint = _snapshot(
        "dabble",
        (_market("dabble", market_id="d-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=300),
    )
    inside = _snapshot(
        "prizepicks",
        (_market("prizepicks", market_id="p-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=299, microseconds=999999),
    )
    service, _ = _service([endpoint, inside])

    board = _read(service)
    group = board.groups[0]
    freshness = {member.provider: member.freshness for member in group.members}

    assert freshness == {
        "dabble": MarketFreshness.STALE,
        "prizepicks": MarketFreshness.FRESH,
    }
    assert group.is_mixed_freshness
    assert group.summary.freshness is ComparisonFreshness.MIXED
    reports = {report.provider: report.freshness for report in board.provider_reports}
    assert reports == {
        "dabble": MarketFreshness.STALE,
        "prizepicks": MarketFreshness.FRESH,
    }


@pytest.mark.parametrize(
    "windows",
    [
        {"dfs_cache_fresh_seconds": Decimal("1E+129")},
        {"dfs_cache_fresh_seconds": Decimal("1E-200")},
        {"dfs_cache_stale_if_error_seconds": Decimal("1E+129")},
    ],
)
def test_a_window_the_board_would_refuse_cannot_be_configured(windows):
    """Nothing a request refuses may sit in an accepted configuration."""

    from app.config.settings import ProviderSettings

    with pytest.raises(ValueError):
        ProviderSettings(**windows)


def test_a_catalog_states_its_ttl_as_the_exact_duration_it_gated_on():
    """A catalog's own exact seconds are read ahead of any rewritten unit."""

    ttl = Decimal("1200.000000")
    last_success = GENERATED_AT - timedelta(seconds=1200)
    catalog = FakeCatalog(
        {
            "season": SEASON,
            "fresh": True,
            "max_age_seconds": ttl,
            "last_success_at": last_success.isoformat(),
        },
        ttl_seconds=1200,
        fresh_key="fresh",
    )
    service, _ = _service(
        [_snapshot("dabble", (_market(),))], event_catalog=catalog
    )

    board = _read(service)
    entry = next(
        entry for entry in board.availability.catalogs if entry.catalog == "event_catalog"
    )

    assert entry.max_age_seconds == ttl
    assert entry.age_seconds == ttl
    assert entry.available is True


# -- one finite, exact cache age -------------------------------------------


def _cache_outcome(age):
    return ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.FAILED,
        reason=ProviderFailureReason.UPSTREAM_ERROR,
        cache_status="error",
        cache_age_seconds=age,
    )


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (Decimal("30.5"), "30.5"),
        ("30.5", "30.5"),
        (30, "30"),
        (30.5, "30.5"),
        (0, "0"),
        ("1E+128", "1E+128"),
        ("1E-128", "1E-128"),
    ],
)
def test_a_provider_outcome_holds_one_exact_finite_cache_age(age, expected):
    outcome = _cache_outcome(age)

    assert isinstance(outcome.cache_age_seconds, Decimal)
    assert outcome.cache_age_seconds == Decimal(expected)
    assert BoardCacheState.of(outcome).age_seconds == Decimal(expected)


@pytest.mark.parametrize(
    "age",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "nan",
        "inf",
        -1,
        Decimal("-0.000001"),
        True,
        "1E+129",
        "1E-129",
        "not-a-number",
        object(),
    ],
)
def test_an_unusable_cache_age_is_one_sanitized_provider_outcome_error(age):
    with pytest.raises(ValueError) as error:
        _cache_outcome(age)

    assert "cache_age_seconds" in str(error.value)
    assert str(age) not in str(error.value)


def test_a_cache_age_is_exact_inside_a_hostile_decimal_context():
    with localcontext() as context:
        context.prec = 1
        context.traps[InvalidOperation] = True
        outcome = _cache_outcome("1234.5678")

    assert outcome.cache_age_seconds == Decimal("1234.5678")


def test_a_comparison_board_reports_an_exact_cache_age_from_a_float():
    outcome = ProviderOutcome(
        provider="dabble",
        status=ProviderOutcomeStatus.FAILED,
        reason=ProviderFailureReason.TIMEOUT,
        cache_status="error",
        cache_age_seconds=30.5,
    )

    report = _outcome_service(outcome).get_comparisons(_query(), _context()).provider_reports[0]

    assert report.cache.age_seconds == Decimal("30.5")
