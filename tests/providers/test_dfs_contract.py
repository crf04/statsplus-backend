"""Focused tests for the shared NBA DFS provider contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.providers.dfs import (
    AthleteEvidence,
    AppearanceEvidence,
    CompetitionEvidence,
    CoverageCode,
    EventEvidence,
    LeagueEvidence,
    MarketThreshold,
    MarketStatus,
    MarketVariant,
    PlayerProjectionMarket,
    NBAMarketQuery,
    CoverageEvidence,
    CoverageRecordExcluded,
    CoverageRecordMalformed,
    MalformedProviderResponseError,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    SnapshotStatus,
    SelectionModifier,
    SelectionDirection,
    Selection,
    SportEvidence,
    ScoringPeriod,
    StatisticEvidence,
    TeamEvidence,
    normalize_market_variant,
    normalize_market_status,
    normalize_selection_direction,
    normalize_scoring_period,
)
from app.domain.market_content import (
    NORMALIZED_DECIMAL_PLACE_LIMIT,
    NumericDomainError,
    market_content_key,
    market_evidence_key,
)
from app.domain.statistics import MatchReason, MatchState, StatisticMatch
from app.providers.dfs import _SnapshotMarketCollector


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Standard", MarketVariant.STANDARD),
        ("main", MarketVariant.STANDARD),
        ("ALT", MarketVariant.ALTERNATE),
        ("promotional", MarketVariant.PROMOTIONAL),
        ("new partner offer", MarketVariant.UNKNOWN),
        (None, MarketVariant.UNKNOWN),
    ],
)
def test_market_variant_normalization_is_closed_and_retains_original_label(
    label: str | None,
    expected: MarketVariant,
):
    normalized = normalize_market_variant(label)

    assert normalized.value is expected
    assert normalized.original_label == label


def test_unknown_variant_enum_does_not_create_provider_label() -> None:
    normalized = normalize_market_variant(MarketVariant.UNKNOWN)

    assert normalized.value is MarketVariant.UNKNOWN
    assert normalized.original_label is None

    explicit = normalize_market_variant("unknown")
    assert explicit.value is MarketVariant.UNKNOWN
    assert explicit.original_label == "unknown"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Over", SelectionDirection.HIGHER),
        ("more than", SelectionDirection.HIGHER),
        ("UNDER", SelectionDirection.LOWER),
        ("less", SelectionDirection.LOWER),
        ("provider-specific", SelectionDirection.UNKNOWN),
        (None, SelectionDirection.UNKNOWN),
    ],
)
def test_selection_direction_normalization_is_closed_and_retains_original_label(
    label: str | None,
    expected: SelectionDirection,
):
    normalized = normalize_selection_direction(label)

    assert normalized.value is expected
    assert normalized.original_label == label


def test_threshold_keeps_exact_decimal_and_source_value():
    threshold = MarketThreshold(
        value="25.500",
        unit="points",
        original_value="25.500 pts",
    )

    assert threshold.value == Decimal("25.500")
    assert threshold.original_value == "25.500 pts"
    assert not hasattr(threshold, "displayed_value")

    with pytest.raises((AttributeError, TypeError)):
        threshold.value = Decimal("26")  # type: ignore[misc]


def test_the_normalized_numeric_domain_accepts_its_documented_boundary():
    limit = NORMALIZED_DECIMAL_PLACE_LIMIT
    highest = f"1E+{limit}"
    lowest = f"1E-{limit}"
    widest = "9" * (2 * limit + 1) + f"E-{limit}"

    assert MarketThreshold(value=highest, unit="points").value == Decimal(highest)
    assert MarketThreshold(value=lowest, unit="points").value == Decimal(lowest)
    assert MarketThreshold(value=widest, unit="points").value == Decimal(widest)
    assert SelectionModifier(
        value=highest, kind="multiplier", scope="selection"
    ).value == Decimal(highest)
    assert Selection(
        selection_id="s-1", decimal_price=lowest
    ).decimal_price == Decimal(lowest)


@pytest.mark.parametrize(
    "value",
    [
        f"1E+{NORMALIZED_DECIMAL_PLACE_LIMIT + 1}",
        f"-1E+{NORMALIZED_DECIMAL_PLACE_LIMIT + 1}",
        f"1E-{NORMALIZED_DECIMAL_PLACE_LIMIT + 1}",
        f"-1E-{NORMALIZED_DECIMAL_PLACE_LIMIT + 1}",
        "9" * (2 * NORMALIZED_DECIMAL_PLACE_LIMIT + 2)
        + f"E-{NORMALIZED_DECIMAL_PLACE_LIMIT}",
        "1E+999999999",
    ],
)
def test_a_provider_number_beyond_the_numeric_domain_is_refused(value):
    assert issubclass(NumericDomainError, ValueError)
    for build in (
        lambda: MarketThreshold(value=value, unit="points"),
        lambda: SelectionModifier(value=value, kind="multiplier", scope="selection"),
        lambda: Selection(selection_id="s-1", decimal_price=value),
    ):
        with pytest.raises(NumericDomainError, match="normalized numeric domain") as raised:
            build()
        assert value not in str(raised.value)


def test_an_unknown_direction_enum_carries_no_provider_label():
    # ``SelectionDirection.UNKNOWN`` is this application's own word for "the
    # provider did not say", so it is not a label the provider wrote.
    normalized = normalize_selection_direction(SelectionDirection.UNKNOWN)

    assert normalized.value is SelectionDirection.UNKNOWN
    assert normalized.original_label is None
    assert (
        normalize_selection_direction(SelectionDirection.HIGHER).original_label
        == "higher"
    )
    assert Selection(selection_id="s-1").direction_label is None
    assert (
        Selection(
            selection_id="s-1", direction=SelectionDirection.UNKNOWN
        ).direction_label
        is None
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (110, 110),
        (-110, -110),
        (0, 0),
        ("110", 110),
        ("-110", -110),
        (110.0, 110),
        (Decimal("-110"), -110),
        (Decimal("1.10E+2"), 110),
        ("-0", 0),
        (f"1E+{NORMALIZED_DECIMAL_PLACE_LIMIT}", 10**NORMALIZED_DECIMAL_PLACE_LIMIT),
    ],
)
def test_an_american_price_accepts_every_exactly_integral_value(value, expected):
    selection = Selection(selection_id="s-1", american_price=value)

    assert selection.american_price == expected
    assert isinstance(selection.american_price, int)
    assert not isinstance(selection.american_price, bool)


@pytest.mark.parametrize(
    "value",
    [
        110.5,
        -110.5,
        "110.5",
        Decimal("1.5"),
        float("inf"),
        float("-inf"),
        float("nan"),
        "Infinity",
        "-Infinity",
        "NaN",
        1e400,
        "1E+400",
        f"1E+{NORMALIZED_DECIMAL_PLACE_LIMIT + 1}",
        Decimal("Infinity"),
        Decimal("NaN"),
        True,
        False,
        "",
        "  ",
        "even",
        object(),
    ],
)
def test_an_american_price_that_is_not_exactly_integral_is_refused(value):
    # Every rejection is a ValueError, because that is what each provider
    # adapter converts into one typed malformed record, and none of them quotes
    # the value it refused.
    with pytest.raises(ValueError) as raised:
        Selection(selection_id="s-1", american_price=value)

    if str(value).strip():
        assert str(value) not in str(raised.value)


def test_selection_modifier_is_decimal_evidence_not_a_payout():
    modifier = SelectionModifier(
        value="1.000",
        kind="multiplier",
        scope="selection",
        label="1.000x",
    )

    assert modifier.value == Decimal("1.000")
    assert modifier.kind == "multiplier"
    assert modifier.scope == "selection"
    assert modifier.label == "1.000x"
    assert not hasattr(modifier, "payout")
    assert not hasattr(modifier, "original_label")


def test_selection_modifier_can_preserve_a_missing_provider_label():
    modifier = SelectionModifier(
        value="1.500",
        kind="multiplier",
        scope="selection",
    )

    assert modifier.value == Decimal("1.500")
    assert modifier.kind == "multiplier"
    assert modifier.scope == "selection"
    assert modifier.label is None
    assert not hasattr(modifier, "original_label")


def test_identity_evidence_retains_nullable_provider_ids_and_normalizes_utc():
    athlete = AthleteEvidence(
        provider_id=None,
        canonical_id=2544,
        name="LeBron James",
        team=TeamEvidence(
            provider_id="team-1",
            canonical_id=1610612747,
            name="Los Angeles Lakers",
            abbreviation="LAL",
        ),
    )
    event = EventEvidence(
        provider_id="fixture-1",
        canonical_id="0022600001",
        label="Los Angeles Lakers @ Chicago Bulls",
        status_label="scheduled",
        starts_at=datetime(2026, 8, 9, 11, 30, tzinfo=timezone(timedelta(hours=-5))),
        home_team=athlete.team,
    )
    statistic = StatisticEvidence(
        provider_id="stat-1",
        canonical_id="points",
        label="Points",
    )

    assert athlete.provider_id is None
    appearance = AppearanceEvidence(
        provider_id="appearance-1", appearance_type="Player", label="LeBron James"
    )
    assert appearance.provider_id == "appearance-1"
    assert appearance.appearance_type == "Player"
    assert appearance.label == "LeBron James"
    assert event.status_label == "scheduled"
    assert event.starts_at == datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc)
    assert statistic.label == "Points"

    with pytest.raises(ValueError, match="timezone-aware"):
        EventEvidence(label="ambiguous", starts_at=datetime(2026, 8, 9, 16, 30))


def test_market_and_selection_keep_typed_evidence_and_closed_labels():
    selection = Selection(
        selection_id=None,
        label="Higher",
        direction="Over",
        status="active",
        decimal_price="1.91",
    )
    assert selection.selection_id is None
    assert selection.direction is SelectionDirection.HIGHER
    assert selection.direction_label == "Over"
    assert selection.decimal_price == Decimal("1.91")
    assert not hasattr(selection, "provider_id")
    assert not hasattr(selection, "status_label")

    assert CompetitionEvidence(provider_id="competition-1", label="NBA").label == "NBA"
    assert LeagueEvidence(provider_id=None, label="NBA").label == "NBA"
    assert SportEvidence(provider_id="sport-1", label="Basketball").label == "Basketball"


def test_player_projection_market_preserves_evidence_and_uses_exact_threshold():
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id=None,
        athlete=AthleteEvidence(provider_id="player-1", name="LeBron James"),
        event=EventEvidence(provider_id="fixture-1", label="LAL @ CHI"),
        team=TeamEvidence(provider_id="team-1", name="Los Angeles Lakers"),
        opponent=TeamEvidence(provider_id="team-2", name="Chicago Bulls"),
        league=LeagueEvidence(provider_id="competition-1", label="NBA"),
        competition=CompetitionEvidence(provider_id="competition-1", label="NBA"),
        sport=SportEvidence(provider_id="sport-1", label="Basketball"),
        statistic=StatisticEvidence(provider_id="stat-1", label="Points"),
        threshold=MarketThreshold("25.50", unit="points"),
        status="suspended",
        status_label="Temporarily unavailable",
        variant="new partner offer",
        scoring_period="full game",
        selections=(Selection(label="Over", direction="over"),),
    )

    assert market.status is MarketStatus.SUSPENDED
    assert market.status_label == "Temporarily unavailable"
    assert market.variant is MarketVariant.UNKNOWN
    assert market.variant_label == "new partner offer"
    assert market.scoring_period is ScoringPeriod.FULL_GAME
    assert market.threshold.value == Decimal("25.50")
    assert market.selections[0].selection_id is None
    assert not hasattr(market, "provider_market_id")
    assert not hasattr(market, "period")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("full_game", ScoringPeriod.FULL_GAME),
        ("FULL_GAME", ScoringPeriod.FULL_GAME),
        (" full_game ", ScoringPeriod.FULL_GAME),
        ("first_half", ScoringPeriod.FIRST_HALF),
        ("second_half", ScoringPeriod.SECOND_HALF),
        ("first_quarter", ScoringPeriod.FIRST_QUARTER),
        ("second_quarter", ScoringPeriod.SECOND_QUARTER),
    ],
)
def test_normalize_scoring_period_resolves_canonical_closed_values(label, expected):
    normalized = normalize_scoring_period(label)

    assert normalized.value is expected
    assert normalized.original_label == label


@pytest.mark.parametrize("label", ["unknown", "overtime", "full_gam", "full_game_2", ""])
def test_normalize_scoring_period_keeps_unreviewed_labels_unknown(label):
    normalized = normalize_scoring_period(label)

    assert normalized.value is ScoringPeriod.UNKNOWN
    assert normalized.original_label == label


def test_player_projection_market_accepts_canonical_scoring_period_string():
    market = PlayerProjectionMarket(
        provider="prizepicks",
        market_id="market-1",
        statistic=StatisticEvidence(label="Points"),
        scoring_period="full_game",
    )

    assert market.scoring_period is ScoringPeriod.FULL_GAME
    assert market.scoring_period_label == "full_game"


def test_player_projection_market_omitted_scoring_period_stays_unknown():
    market = PlayerProjectionMarket(
        provider="prizepicks",
        market_id="market-1",
        statistic=StatisticEvidence(label="Points"),
    )

    assert market.scoring_period is ScoringPeriod.UNKNOWN
    assert market.scoring_period_label is None
    assert (
        PlayerProjectionMarket(provider="prizepicks", scoring_period=None).scoring_period
        is ScoringPeriod.UNKNOWN
    )


@pytest.mark.parametrize(
    ("field", "evidence", "expected_type"),
    [
        ("league", CompetitionEvidence(label="NBA"), LeagueEvidence),
        ("league", SportEvidence(label="Basketball"), LeagueEvidence),
        ("competition", LeagueEvidence(label="NBA"), CompetitionEvidence),
        ("competition", SportEvidence(label="Basketball"), CompetitionEvidence),
        ("sport", LeagueEvidence(label="NBA"), SportEvidence),
        ("sport", CompetitionEvidence(label="NBA"), SportEvidence),
    ],
)
def test_player_projection_market_rejects_cross_slot_evidence_types(
    field: str,
    evidence: object,
    expected_type: type[object],
) -> None:
    with pytest.raises(
        ValueError,
        match=f"market {field} must be {expected_type.__name__} or None",
    ):
        PlayerProjectionMarket(provider="dabble", **{field: evidence})


def test_player_projection_market_keeps_missing_variant_label_missing():
    market = PlayerProjectionMarket(provider="underdog", variant=MarketVariant.UNKNOWN)

    assert market.variant is MarketVariant.UNKNOWN
    assert market.variant_label is None


def test_provider_snapshot_status_must_match_coverage_completion():
    market = PlayerProjectionMarket(provider="dabble")
    incomplete = CoverageEvidence(pagination_complete=False)
    complete = CoverageEvidence(pagination_complete=True, fanout_complete=True)

    with pytest.raises(ValueError, match="complete snapshots require complete coverage"):
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(market,),
            coverage=incomplete,
            retrieved_at="2026-08-09T16:30:00Z",
        )

    with pytest.raises(ValueError, match="partial snapshots require incomplete coverage"):
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.PARTIAL,
            markets=(market,),
            coverage=complete,
            retrieved_at="2026-08-09T16:30:00Z",
        )


def test_contract_models_expose_only_canonical_count_and_timestamp_fields():
    coverage = CoverageEvidence(
        fetched_count=1,
        eligible_count=1,
        normalized_count=1,
        skipped_count=0,
    )
    assert coverage.fetched_count == 1
    assert coverage.eligible_count == 1
    assert coverage.normalized_count == 1
    assert coverage.skipped_count == 0
    for alias in ("fetched", "eligible", "normalized", "skipped"):
        assert not hasattr(coverage, alias)

    snapshot = ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.COMPLETE,
        markets=(),
        coverage=coverage,
        retrieved_at="2026-08-09T16:30:00Z",
    )
    assert snapshot.retrieved_at.isoformat() == "2026-08-09T16:30:00+00:00"
    assert not hasattr(snapshot, "fetched_at")


def test_snapshot_deduplicates_agreeing_source_identity_and_retains_coverage():
    market = PlayerProjectionMarket(
        provider="prizepicks",
        market_id="projection-1",
        athlete=AthleteEvidence(provider_id="player-1", name="LeBron James"),
        statistic=StatisticEvidence(label="Points"),
        threshold=MarketThreshold("25.5", unit="points"),
    )
    coverage = CoverageEvidence(
        fetched_count=2,
        eligible_count=2,
        normalized_count=2,
        skipped_count=0,
        pagination_complete=True,
        fanout_complete=True,
        expected_total=2,
        warning_codes=("duplicate_source_identity",),
    )
    snapshot = ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.COMPLETE,
        markets=(market, market),
        coverage=coverage,
        retrieved_at=datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc),
    )

    assert len(snapshot.markets) == 1
    assert snapshot.coverage.expected_total == 2
    assert snapshot.retrieved_at.tzinfo is timezone.utc
    with pytest.raises((AttributeError, TypeError)):
        snapshot.markets += (market,)  # type: ignore[misc]


def test_snapshot_rejects_conflicting_content_for_the_same_source_identity():
    first = PlayerProjectionMarket(
        provider="underdog",
        market_id="line-1",
        athlete=AthleteEvidence(provider_id="player-1", name="LeBron James"),
        statistic=StatisticEvidence(label="Points"),
        threshold=MarketThreshold("25.5", unit="points"),
    )
    conflict = PlayerProjectionMarket(
        provider="underdog",
        market_id="line-1",
        athlete=AthleteEvidence(provider_id="player-1", name="LeBron James"),
        statistic=StatisticEvidence(label="Points"),
        threshold=MarketThreshold("26.5", unit="points"),
    )

    with pytest.raises(MalformedProviderResponseError):
        ProviderSnapshot(
            provider="underdog",
            status="complete",
            markets=(first, conflict),
            coverage=CoverageEvidence(
                fetched_count=2,
                eligible_count=2,
                normalized_count=2,
                skipped_count=0,
                pagination_complete=True,
                fanout_complete=True,
            ),
            retrieved_at=datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc),
        )


def _repeatable_market(**overrides):
    """One normalized market, varied only in the facts a repeat may restate."""

    values = {
        "provider": "underdog",
        "market_id": "line-1",
        "athlete": AthleteEvidence(provider_id="player-1", name="LeBron James"),
        "statistic": StatisticEvidence(label="Points"),
        "threshold": MarketThreshold("25.5", unit="points", original_value="25.5"),
    }
    return PlayerProjectionMarket(**{**values, **overrides})


def _repeat_snapshot(markets):
    return ProviderSnapshot(
        provider="underdog",
        status="complete",
        markets=tuple(markets),
        coverage=CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            skipped_count=0,
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc),
    )


def test_a_snapshot_repeat_that_only_reorders_selections_is_one_market():
    higher = Selection(selection_id="s-1", label="Higher", direction="higher")
    lower = Selection(selection_id="s-2", label="Lower", direction="lower")
    forward = _repeat_snapshot(
        (
            _repeatable_market(selections=(higher, lower)),
            _repeatable_market(selections=(lower, higher)),
        )
    )
    backward = _repeat_snapshot(
        (
            _repeatable_market(selections=(lower, higher)),
            _repeatable_market(selections=(higher, lower)),
        )
    )

    assert len(forward.markets) == 1
    assert len(backward.markets) == 1
    # The snapshot retains the provider's own listing rather than rewriting it,
    # and the retained evidence of both readings says exactly the same thing.
    assert market_evidence_key(forward.markets[0]) == market_evidence_key(
        backward.markets[0]
    )
    assert set(forward.markets[0].selections) == {higher, lower}


def test_a_snapshot_repeat_written_at_another_scale_is_one_market():
    plain = _repeatable_market()
    padded = _repeatable_market(
        threshold=MarketThreshold("25.50", unit="points", original_value="25.50")
    )

    forward = _repeat_snapshot((plain, padded))
    backward = _repeat_snapshot((padded, plain))

    assert len(forward.markets) == len(backward.markets) == 1
    # The one retained spelling is chosen by content, never by arrival order.
    retained = forward.markets[0].threshold
    assert retained.original_value == backward.markets[0].threshold.original_value
    assert retained.value.as_tuple() == backward.markets[0].threshold.value.as_tuple()


def test_a_snapshot_repeat_that_changes_a_stated_fact_is_still_malformed():
    with pytest.raises(MalformedProviderResponseError):
        _repeat_snapshot(
            (
                _repeatable_market(),
                _repeatable_market(
                    threshold=MarketThreshold(
                        "26.5", unit="points", original_value="26.5"
                    )
                ),
            )
        )
    with pytest.raises(MalformedProviderResponseError):
        _repeat_snapshot(
            (
                _repeatable_market(),
                _repeatable_market(status=MarketStatus.SUSPENDED),
            )
        )
    with pytest.raises(MalformedProviderResponseError):
        _repeat_snapshot(
            (
                _repeatable_market(selections=(Selection(selection_id="s-1"),)),
                _repeatable_market(selections=(Selection(selection_id="s-2"),)),
            )
        )


def test_the_market_collector_reads_a_repeat_in_the_same_semantics():
    higher = Selection(selection_id="s-1", label="Higher", direction="higher")
    lower = Selection(selection_id="s-2", label="Lower", direction="lower")
    padded = _repeatable_market(
        threshold=MarketThreshold("25.50", unit="points", original_value="25.50"),
        selections=(lower, higher),
    )
    plain = _repeatable_market(selections=(higher, lower))

    def collected(markets):
        collector = _SnapshotMarketCollector()
        collector.extend(markets)
        return collector

    forward = collected((plain, padded))
    backward = collected((padded, plain))

    assert len(forward.markets) == len(backward.markets) == 1
    assert forward.warning_codes == (CoverageCode.DUPLICATE_SOURCE_IDENTITY,)
    assert (
        forward.markets[0].threshold.original_value
        == backward.markets[0].threshold.original_value
    )
    assert forward.markets[0].selections == backward.markets[0].selections


def test_the_market_collector_still_rejects_a_changed_repeat():
    collector = _SnapshotMarketCollector()
    collector.add(_repeatable_market())

    with pytest.raises(CoverageRecordMalformed) as error:
        collector.add(
            _repeatable_market(
                threshold=MarketThreshold("26.5", unit="points", original_value="26.5")
            )
        )

    assert error.value.code is CoverageCode.CONFLICTING_SOURCE_IDENTITY
    assert collector.markets == ()
    assert CoverageCode.CONFLICTING_SOURCE_IDENTITY in collector.warning_codes


class _CanonicalStatistic:
    """One reviewed canonical statistic, stated as the catalog contract reads it."""

    def __init__(self, comparable: bool) -> None:
        self.id = "points"
        self.components = ("points",)
        self.comparable = comparable


def _resolved(comparable: bool) -> StatisticMatch:
    """One canonical statistic resolution of a market's own evidence."""

    return StatisticMatch(
        state=MatchState.CANONICAL,
        evidence=StatisticEvidence(label="Points"),
        scoring_period=ScoringPeriod.FULL_GAME,
        canonical=_CanonicalStatistic(comparable),
        provider="underdog",
    )


def _unresolved() -> StatisticMatch:
    return StatisticMatch(
        state=MatchState.UNMAPPED,
        evidence=StatisticEvidence(label="Points"),
        scoring_period=ScoringPeriod.FULL_GAME,
        provider="underdog",
        reason=MatchReason.UNKNOWN_PROVIDER_LABEL,
    )


def test_statistic_comparability_is_part_of_what_a_market_says():
    # Comparability decides whether a resolved market may enter a group at all,
    # so no two of these readings are the same offering, and a market that
    # states no resolution at all is distinct from every one of them.
    keys = {
        market_content_key(market)
        for market in (
            _repeatable_market(),
            _repeatable_market(statistic_match=_unresolved()),
            _repeatable_market(statistic_match=_resolved(True)),
            _repeatable_market(statistic_match=_resolved(False)),
        )
    }

    assert len(keys) == 4


def test_a_snapshot_repeat_that_flips_comparability_is_a_conflict():
    comparable = _repeatable_market(statistic_match=_resolved(True))
    incomparable = _repeatable_market(statistic_match=_resolved(False))

    for markets in ((comparable, incomparable), (incomparable, comparable)):
        with pytest.raises(MalformedProviderResponseError):
            _repeat_snapshot(markets)


def test_a_snapshot_repeat_restating_one_comparability_is_still_one_market():
    snapshot = _repeat_snapshot(
        (
            _repeatable_market(statistic_match=_resolved(True)),
            _repeatable_market(statistic_match=_resolved(True)),
        )
    )

    assert len(snapshot.markets) == 1


def test_the_market_collector_fails_closed_on_flipped_comparability():
    for first, second in ((True, False), (False, True)):
        collector = _SnapshotMarketCollector()
        collector.add(_repeatable_market(statistic_match=_resolved(first)))

        with pytest.raises(CoverageRecordMalformed) as error:
            collector.add(_repeatable_market(statistic_match=_resolved(second)))

        assert error.value.code is CoverageCode.CONFLICTING_SOURCE_IDENTITY
        # Neither reading survives, whichever of them arrived first.
        assert collector.markets == ()


def test_partial_snapshot_requires_usable_market_and_known_incomplete_work():
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="line-1",
        athlete=AthleteEvidence(name="LeBron James"),
        statistic=StatisticEvidence(label="Points"),
        threshold=MarketThreshold("25.5", unit="points"),
    )
    partial = ProviderSnapshot(
        provider="dabble",
        status="partial",
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            skipped_count=0,
            pagination_complete=False,
        ),
        retrieved_at="2026-08-09T16:30:00Z",
    )
    assert partial.status is SnapshotStatus.PARTIAL
    assert not hasattr(partial, "is_partial")

    with pytest.raises(ValueError, match="at least one market"):
        ProviderSnapshot(
            provider="dabble",
            status="partial",
            markets=(),
            coverage=CoverageEvidence(pagination_complete=False),
            retrieved_at="2026-08-09T16:30:00Z",
        )


def test_market_query_is_semantic_nba_scope_and_context_has_absolute_deadline():
    query = NBAMarketQuery(
        sport="nba",
        league="NBA",
        market_statuses=("available", "suspended"),
    )
    assert query.sport == "NBA"
    assert query.market_statuses == (MarketStatus.AVAILABLE, MarketStatus.SUSPENDED)

    context = RetrievalContext(
        deadline="2026-08-09T16:30:10-05:00",
        request_id="request-1",
    )
    assert context.deadline == datetime(2026, 8, 9, 21, 30, 10, tzinfo=timezone.utc)
    assert not hasattr(context, "deadline_at")
    assert context.remaining_seconds(
        now=datetime(2026, 8, 9, 16, 30, 5, tzinfo=timezone(timedelta(hours=-5)))
    ) == 5
    assert not context.is_expired(
        now=datetime(2026, 8, 9, 16, 30, tzinfo=timezone(timedelta(hours=-5)))
    )

    with pytest.raises(ValueError, match="NBA"):
        NBAMarketQuery(sport="NFL")

    with pytest.raises(ValueError, match="pregame"):
        NBAMarketQuery(pregame_only=False)

    for unsafe_request_id in ("contains spaces", "x" * 129, "line\nbreak"):
        with pytest.raises(ValueError, match="request_id"):
            RetrievalContext(
                deadline="2026-08-09T16:30:10Z",
                request_id=unsafe_request_id,
            )


@pytest.mark.parametrize(
    "season", ["2024-99", "2024-24", "2024-26", "2024-2025", "24-25", "not-a-season"]
)
def test_market_query_rejects_a_noncanonical_season_before_any_provider_call(season):
    with pytest.raises(ValueError, match="season"):
        NBAMarketQuery(season=season)


@pytest.mark.parametrize("season", ["2024-25", " 2024-25 ", "2099-00"])
def test_market_query_accepts_canonical_consecutive_seasons(season):
    assert NBAMarketQuery(season=season).season == season.strip()


def test_provider_snapshot_protocol_is_the_single_adapter_seam():
    class FakeProvider:
        def get_snapshot(self, query, context):
            del query, context
            return "snapshot"

    assert isinstance(FakeProvider(), ProviderSnapshotProvider)


def test_coverage_codes_are_closed_and_diagnostic_details_are_separate():
    coverage = CoverageEvidence(
        warning_codes=("duplicate_source_identity",),
        skipped_reasons=(CoverageCode.MALFORMED_RECORD,),
        diagnostic_details=("line_score had an invalid decimal",),
    )

    assert coverage.warning_codes == (CoverageCode.DUPLICATE_SOURCE_IDENTITY,)
    assert coverage.skipped_reasons == (CoverageCode.MALFORMED_RECORD,)
    assert coverage.diagnostic_details == ("line_score had an invalid decimal",)
    assert CoverageCode.DUPLICATE_SOURCE_IDENTITY == "duplicate_source_identity"

    with pytest.raises(ValueError, match="known CoverageCode"):
        CoverageEvidence(warning_codes=("duplicate_source_identitiy",))
    with pytest.raises(ValueError, match="known CoverageCode"):
        CoverageEvidence(skipped_reasons=("provider-specific detail",))


def test_record_coverage_exceptions_require_typed_codes_and_keep_detail_separate():
    with pytest.raises(TypeError, match="CoverageCode"):
        CoverageRecordExcluded("non_player_market")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="CoverageCode"):
        CoverageRecordMalformed(
            "provider-specific detail",
            code="malformed_record",  # type: ignore[arg-type]
        )

    malformed = CoverageRecordMalformed("missing_match_type")
    assert malformed.code is CoverageCode.MALFORMED_RECORD
    assert malformed.detail == "missing_match_type"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("active", MarketStatus.AVAILABLE),
        ("pre_game", MarketStatus.AVAILABLE),
        ("pregame", MarketStatus.AVAILABLE),
        ("paused", MarketStatus.SUSPENDED),
    ],
)
def test_market_status_normalization_is_the_single_authority(
    label: str,
    expected: MarketStatus,
):
    normalized = normalize_market_status(label)

    assert normalized.value is expected
    assert normalized.original_label == label


def test_unknown_market_status_is_not_guessed():
    with pytest.raises(ValueError, match="eligible"):
        normalize_market_status("provider-specific-status")


def test_dfs_module_exports_only_contract_symbols():
    import app.providers.dfs as dfs

    assert set(dfs.__all__) == {
        "AthleteEvidence",
        "AppearanceEvidence",
        "CompetitionEvidence",
        "CoverageCode",
        "CoverageEvidence",
        "DeadlineExceededError",
        "EventEvidence",
        "LeagueEvidence",
        "MalformedProviderResponseError",
        "MarketStatus",
        "MarketThreshold",
        "MarketVariant",
        "NBAMarketQuery",
        "NormalizedLabel",
        "PlayerProjectionMarket",
        "ProviderSnapshot",
        "ProviderSnapshotProvider",
        "RetrievalContext",
        "ScoringPeriod",
        "Selection",
        "SelectionDirection",
        "SelectionModifier",
        "SnapshotStatus",
        "SportEvidence",
        "StatisticEvidence",
        "TeamEvidence",
        "normalize_coverage_code",
        "normalize_market_status",
        "normalize_market_variant",
        "normalize_scoring_period",
        "normalize_selection_direction",
        "normalize_timestamp",
    }
