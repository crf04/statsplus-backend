"""Offline, high-level tests for the factual Comparison Group board."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests

from app.domain import comparisons
from app.domain.comparisons import (
    SUPPORTED_NARROWING_FILTERS,
    ComparisonBoard,
    ComparisonGroup,
    ComparisonMember,
    CatalogAvailabilityReason,
    ComparisonExclusion,
    ComparisonFilters,
    ComparisonFreshness,
    ComparisonSummary,
    MarketFreshness,
    market_reference,
)
from app.domain.statistics import ScoringPeriod
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MalformedProviderResponseError,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    Selection,
    SnapshotStatus,
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
)
from app.services.dfs_board import DFSBoardService
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
    """A canonical catalog reporting one reviewed freshness document."""

    def __init__(self, document):
        self.document = document
        self.seasons: list[str] = []

    def get_freshness(self, season):
        self.seasons.append(season)
        return self.document


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
    service = ComparisonBoardService(
        board_service,
        athlete_catalog=athlete_catalog or _athlete_catalog(),
        event_catalog=event_catalog or _event_catalog(),
        max_markets=max_markets,
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

    assert market_reference(market).startswith("mkt_1_")
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

    assert group.reference.startswith("cmp_1_")
    assert group.reference == group.key.reference
    references = group.members[0].selection_references
    assert len(references) == 2
    assert tuple(sorted(references)) == references
    assert all(reference.startswith("sel_1_") for reference in references)


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
