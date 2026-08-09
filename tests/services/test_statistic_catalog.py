"""Public behavior tests for the reviewed DFS Statistic Catalog."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from app.providers.dfs import (
    CoverageEvidence,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    ScoringPeriod,
    SnapshotStatus,
    StatisticEvidence,
)
from app.services.dfs_board import DFSBoardService
from app.services.statistic_catalog import (
    CanonicalStatistic,
    MatchState,
    StatisticCatalog,
    StatisticCatalogError,
    StatisticResolver,
)


def test_default_catalog_is_immutable_and_contains_initial_full_game_statistics() -> None:
    catalog = StatisticCatalog.load_default()

    assert isinstance(catalog.by_id, MappingProxyType)
    assert tuple(catalog.by_id) == (
        "points",
        "rebounds",
        "assists",
        "three_pointers_made",
        "steals",
        "blocks",
        "turnovers",
        "pra",
        "pa",
        "pr",
        "ra",
    )
    assert all(
        statistic.scoring_periods == (ScoringPeriod.FULL_GAME,)
        for statistic in catalog.statistics
    )
    assert catalog.by_id["pra"].components == ("points", "rebounds", "assists")
    assert catalog.by_id["pa"].components == ("points", "assists")
    assert catalog.by_id["pr"].components == ("points", "rebounds")
    assert catalog.by_id["ra"].components == ("rebounds", "assists")

    with pytest.raises(TypeError):
        catalog.by_id["points"] = catalog.by_id["rebounds"]


def test_default_catalog_resolves_an_explicit_provider_label() -> None:
    match = StatisticResolver(StatisticCatalog.load_default()).resolve(
        "prizepicks",
        "Points",
        scoring_period=ScoringPeriod.FULL_GAME,
        unit="count",
    )

    assert match.state is MatchState.CANONICAL
    assert isinstance(match.canonical, CanonicalStatistic)
    assert match.canonical.id == "points"
    assert match.evidence.label == "Points"


@pytest.mark.parametrize(
    ("provider", "label"),
    [
        ("dabble", "three-pointers-made"),
        ("prizepicks", "3-Pointers Made"),
        ("underdog", "3PM"),
    ],
)
def test_three_pointers_made_has_explicit_provider_labels(provider: str, label: str) -> None:
    match = StatisticCatalog.load_default().resolve(
        provider,
        label,
        scoring_period=ScoringPeriod.FULL_GAME,
        unit="count",
        components=("three-pointers-made",),
    )

    assert match.state is MatchState.CANONICAL
    assert match.canonical_id == "three_pointers_made"


def test_catalog_retains_provider_evidence_and_reorders_composite_components() -> None:
    resolver = StatisticResolver(StatisticCatalog.load_default())
    evidence = StatisticEvidence(
        provider_id="dabble-stat-1",
        label="assists+points+rebounds",
        components=("assists", "points", "rebounds"),
    )

    match = resolver.resolve_evidence(
        "dabble", evidence, scoring_period=ScoringPeriod.FULL_GAME, unit="count"
    )

    assert match.state is MatchState.CANONICAL
    assert match.canonical_id == "pra"
    assert match.canonical is not None
    assert match.canonical.components == ("points", "rebounds", "assists")
    assert match.provider_evidence is evidence
    assert match.provider_evidence.provider_id == "dabble-stat-1"
    assert match.provider_evidence.label == "assists+points+rebounds"


def test_aliases_are_explicit_and_unknown_labels_are_not_guessed() -> None:
    definition = {
        "schema_version": 1,
        "component_order": ["points"],
        "statistics": [
            {
                "id": "points",
                "label": "Points",
                "unit": "count",
                "scoring_periods": ["full_game"],
                "components": ["points"],
                "provider_mappings": {"dabble": [{"label": "PTS"}]},
            }
        ],
    }
    resolver = StatisticCatalog.from_mapping(definition).resolver

    assert resolver.resolve("dabble", "pts").state is MatchState.CANONICAL
    assert resolver.resolve("dabble", "Points").state is MatchState.UNMAPPED
    assert resolver.resolve("dabble", "PTS+REB").state is MatchState.UNMAPPED


def test_unknown_period_specific_and_provider_fantasy_labels_are_unmapped() -> None:
    resolver = StatisticCatalog.load_default().resolver

    for provider, label in (
        ("dabble", "first-half-points"),
        ("prizepicks", "Fantasy Score"),
        ("underdog", "Fantasy Points"),
        ("prizepicks", "made-up-stat"),
    ):
        match = resolver.resolve(
            provider,
            label,
            scoring_period=ScoringPeriod.FULL_GAME,
            unit="count",
        )
        assert match.state is MatchState.UNMAPPED
        assert match.canonical is None
        assert match.provider_evidence.label == label

    assert (
        resolver.resolve(
            "dabble",
            "points",
            scoring_period=ScoringPeriod.FIRST_HALF,
            unit="count",
        ).state
        is MatchState.UNMAPPED
    )
    assert (
        resolver.resolve("new-provider", "Points").state is MatchState.UNMAPPED
    )


def test_catalog_rejects_duplicate_conflicting_and_invalid_definitions() -> None:
    base = {
        "schema_version": 1,
        "component_order": ["points"],
        "statistics": [
            {
                "id": "points",
                "label": "Points",
                "unit": "count",
                "scoring_periods": ["full_game"],
                "components": ["points"],
                "provider_mappings": {"dabble": ["points"]},
            }
        ],
    }

    duplicate = deepcopy(base)
    duplicate["statistics"].append(deepcopy(base["statistics"][0]))
    with pytest.raises(StatisticCatalogError, match="duplicate.*id"):
        StatisticCatalog.from_mapping(duplicate)

    conflict = deepcopy(base)
    conflict["component_order"].append("rebounds")
    conflict["statistics"].append(
        {
            "id": "rebounds",
            "label": "Rebounds",
            "unit": "count",
            "scoring_periods": ["full_game"],
            "components": ["rebounds"],
            "provider_mappings": {"dabble": ["points"]},
        }
    )
    with pytest.raises(StatisticCatalogError, match="unknown.*component|conflicting"):
        StatisticCatalog.from_mapping(conflict)

    invalid_period = deepcopy(base)
    invalid_period["statistics"][0]["scoring_periods"] = ["overtime"]
    with pytest.raises(StatisticCatalogError, match="period"):
        StatisticCatalog.from_mapping(invalid_period)

    invalid_unit = deepcopy(base)
    invalid_unit["statistics"][0]["unit"] = "fantasy_points"
    with pytest.raises(StatisticCatalogError, match="unit"):
        StatisticCatalog.from_mapping(invalid_unit)


def test_catalog_rejects_inconsistent_ordered_components() -> None:
    definition = {
        "schema_version": 1,
        "component_order": ["points", "rebounds", "assists"],
        "statistics": [
            {
                "id": "points",
                "label": "Points",
                "unit": "count",
                "scoring_periods": ["full_game"],
                "components": ["points"],
            },
            {
                "id": "rebounds",
                "label": "Rebounds",
                "unit": "count",
                "scoring_periods": ["full_game"],
                "components": ["rebounds"],
            },
            {
                "id": "assists",
                "label": "Assists",
                "unit": "count",
                "scoring_periods": ["full_game"],
                "components": ["assists"],
            },
            {
                "id": "pra",
                "label": "PRA",
                "unit": "count",
                "scoring_periods": ["full_game"],
                "components": ["assists", "points", "rebounds"],
            },
        ],
    }
    with pytest.raises(StatisticCatalogError, match="ordered components"):
        StatisticCatalog.from_mapping(definition)


def test_catalog_file_load_failure_is_explicit(tmp_path) -> None:
    definition_path = tmp_path / "invalid-statistics.yaml"
    definition_path.write_text("schema_version: [", encoding="utf-8")

    with pytest.raises(StatisticCatalogError, match="could not be loaded"):
        StatisticCatalog.load(definition_path)


def test_board_resolves_canonical_and_unmapped_statistics_for_all_providers() -> None:
    retrieved_at = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)

    def market(provider: str, label: str, components: tuple[str, ...] = ()) -> PlayerProjectionMarket:
        return PlayerProjectionMarket(
            provider=provider,
            market_id=f"{provider}-market",
            statistic=StatisticEvidence(
                provider_id=f"{provider}-stat",
                label=label,
                components=components,
            ),
            threshold=MarketThreshold("10", unit="count"),
            scoring_period=ScoringPeriod.FULL_GAME,
        )

    snapshots = {
        "dabble": ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(market("dabble", "assists+points+rebounds", ("assists", "points", "rebounds")),),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                pagination_complete=True,
                fanout_complete=True,
            ),
            retrieved_at=retrieved_at,
        ),
        "prizepicks": ProviderSnapshot(
            provider="prizepicks",
            status=SnapshotStatus.COMPLETE,
            markets=(market("prizepicks", "Points"),),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                pagination_complete=True,
                fanout_complete=True,
            ),
            retrieved_at=retrieved_at,
        ),
        "underdog": ProviderSnapshot(
            provider="underdog",
            status=SnapshotStatus.COMPLETE,
            markets=(market("underdog", "Fantasy Points"),),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                pagination_complete=True,
                fanout_complete=True,
            ),
            retrieved_at=retrieved_at,
        ),
    }

    class Provider:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def get_snapshot(self, query: NBAMarketQuery, context: RetrievalContext) -> ProviderSnapshot:
            return snapshots[self.provider]

    service = DFSBoardService(
        provider_registry={name: Provider(name) for name in snapshots},
        deadline_seconds=5,
    )
    board = service.get_board(
        NBAMarketQuery(),
        RetrievalContext(
            deadline=datetime(2026, 8, 9, 20, 0, 5, tzinfo=timezone.utc),
            request_id="statistic-board",
        ),
    )

    assert {market.provider for market in board.canonical_markets} == {
        "dabble",
        "prizepicks",
    }
    assert [market.statistic.canonical_id for market in board.canonical_markets] == [
        "pra",
        "points",
    ]
    assert [market.provider for market in board.unmapped_markets] == ["underdog"]
    unmapped = board.unmapped_markets[0]
    assert unmapped.statistic.label == "Fantasy Points"
    assert unmapped.statistic_match.state is MatchState.UNMAPPED
    assert unmapped.statistic_match.provider_evidence.provider_id == "underdog-stat"
