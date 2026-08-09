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
)


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
