"""Public behavior tests for the reviewed DFS Statistic Catalog."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
from app.domain.statistics import MatchReason, StatisticMatch, StatisticUnit
from app.services.dfs_board import DFSBoardService
from app.services.statistic_catalog import (
    CanonicalStatistic,
    MatchState,
    StatisticCatalog,
    StatisticCatalogError,
    StatisticResolver,
)
from app.services.statistic_catalog_schema import StatisticDefinitionError, load_definition


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
        "stks",
        "field_goals_attempted",
        "three_pointers_attempted",
        "two_pointers_attempted",
    )
    assert all(
        statistic.scoring_periods == (ScoringPeriod.FULL_GAME,)
        for statistic in catalog.statistics
    )
    assert catalog.by_id["pra"].components == ("points", "rebounds", "assists")
    assert catalog.by_id["pa"].components == ("points", "assists")
    assert catalog.by_id["pr"].components == ("points", "rebounds")
    assert catalog.by_id["ra"].components == ("rebounds", "assists")
    assert catalog.by_id["stks"].components == ("steals", "blocks")
    assert {statistic.market_category for statistic in catalog.statistics} == {
        "PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV", "PRA", "PA",
        "PR", "RA", "STKS", "FGA", "FG3A", "FG2A",
    }

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
    assert match.evidence is evidence
    assert match.evidence.provider_id == "dabble-stat-1"
    assert match.evidence.label == "assists+points+rebounds"


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

    full_game = ScoringPeriod.FULL_GAME
    assert resolver.resolve("dabble", "pts", scoring_period=full_game).state is MatchState.CANONICAL
    assert resolver.resolve("dabble", "Points", scoring_period=full_game).state is MatchState.UNMAPPED
    assert resolver.resolve("dabble", "PTS+REB", scoring_period=full_game).state is MatchState.UNMAPPED


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
        assert match.evidence.label == label

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
        resolver.resolve(
            "new-provider", "Points", scoring_period=ScoringPeriod.FULL_GAME
        ).state
        is MatchState.UNMAPPED
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


def test_loader_reports_unhashable_mapping_keys_as_definition_errors(tmp_path) -> None:
    definition_path = tmp_path / "unhashable-key-statistics.yaml"
    definition_path.write_text("schema_version: 1\n? [points, assists]\n: value\n", encoding="utf-8")

    with pytest.raises(StatisticDefinitionError, match="could not be loaded"):
        load_definition(definition_path)

    with pytest.raises(StatisticCatalogError, match="could not be loaded"):
        StatisticCatalog.load(definition_path)


def test_loader_still_rejects_duplicate_definition_keys(tmp_path) -> None:
    definition_path = tmp_path / "duplicate-key-statistics.yaml"
    definition_path.write_text("schema_version: 1\nstatistics: []\nstatistics: []\n", encoding="utf-8")

    with pytest.raises(StatisticDefinitionError, match="could not be loaded"):
        load_definition(definition_path)


@pytest.mark.parametrize("schema_version", [2, 999, 0])
def test_catalog_rejects_unimplemented_schema_versions(schema_version: int) -> None:
    definition = {
        "schema_version": schema_version,
        "statistics": [{
            "id": "points", "label": "Points", "unit": "count",
            "scoring_periods": ["full_game"], "components": ["points"],
        }],
    }

    with pytest.raises(StatisticCatalogError, match="schema_version.*(implemented|supported|1)"):
        StatisticCatalog.from_mapping(definition)


def _schema_v1_definition() -> dict:
    return {
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


def test_schema_version_is_required_and_has_no_version_alias() -> None:
    without_version = _schema_v1_definition()
    del without_version["schema_version"]
    with pytest.raises(StatisticCatalogError, match="schema_version"):
        StatisticCatalog.from_mapping(without_version)

    aliased = _schema_v1_definition()
    del aliased["schema_version"]
    aliased["version"] = 1
    with pytest.raises(StatisticCatalogError, match="schema_version|unsupported top-level"):
        StatisticCatalog.from_mapping(aliased)

    duplicated = _schema_v1_definition()
    duplicated["version"] = 1
    with pytest.raises(StatisticCatalogError, match="unsupported top-level"):
        StatisticCatalog.from_mapping(duplicated)


@pytest.mark.parametrize(
    "rename",
    [
        ("statistics", "canonical_statistics"),
        ("component_order", "components_order"),
    ],
)
def test_definition_rejects_undocumented_top_level_aliases(rename: tuple[str, str]) -> None:
    definition = _schema_v1_definition()
    definition[rename[1]] = definition.pop(rename[0])

    with pytest.raises(StatisticCatalogError, match="unsupported top-level|statistics"):
        StatisticCatalog.from_mapping(definition)


@pytest.mark.parametrize(
    "rename",
    [
        ("id", "canonical_id"),
        ("label", "display_name"),
        ("label", "name"),
        ("scoring_periods", "period"),
        ("scoring_periods", "periods"),
        ("components", "ordered_components"),
        ("provider_mappings", "provider_labels"),
        ("provider_mappings", "mappings"),
    ],
)
def test_definition_rejects_undocumented_statistic_field_aliases(
    rename: tuple[str, str],
) -> None:
    definition = _schema_v1_definition()
    statistic = definition["statistics"][0]
    statistic[rename[1]] = statistic.pop(rename[0])

    with pytest.raises(StatisticCatalogError, match="unsupported fields"):
        StatisticCatalog.from_mapping(definition)


def test_definition_rejects_undocumented_comparable_alias_and_mapping_alias() -> None:
    comparison_alias = _schema_v1_definition()
    comparison_alias["statistics"][0]["comparison_allowed"] = False
    with pytest.raises(StatisticCatalogError, match="unsupported fields"):
        StatisticCatalog.from_mapping(comparison_alias)

    mapping_alias = _schema_v1_definition()
    mapping_alias["statistics"][0]["provider_mappings"] = {
        "dabble": {"aliases": ["points"]}
    }
    with pytest.raises(StatisticCatalogError, match="unsupported mapping fields"):
        StatisticCatalog.from_mapping(mapping_alias)

    conflicting_mapping = _schema_v1_definition()
    conflicting_mapping["statistics"][0]["provider_mappings"] = {
        "dabble": {"label": "points", "labels": ["pts"]}
    }
    with pytest.raises(StatisticCatalogError, match="label or labels"):
        StatisticCatalog.from_mapping(conflicting_mapping)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda definition: definition["statistics"][0].__setitem__("id", " points "),
        lambda definition: definition["statistics"][0].__setitem__("id", "Points"),
        lambda definition: definition["statistics"][0].__setitem__("label", " Points "),
        lambda definition: definition["statistics"][0].__setitem__("components", [" points "]),
        lambda definition: definition["statistics"][0].__setitem__("components", ["Points"]),
        lambda definition: definition["statistics"][0].__setitem__("unit", " count "),
        lambda definition: definition["statistics"][0].__setitem__("unit", "Count"),
        lambda definition: definition["statistics"][0].__setitem__(
            "scoring_periods", [" full_game "]
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "scoring_periods", ["Full_Game"]
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {" dabble ": ["points"]}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"Dabble": ["points"]}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": [" POINTS "]}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": [{"label": " points "}]}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": {"labels": ["points "]}}
        ),
        lambda definition: definition.__setitem__("component_order", [" points "]),
        lambda definition: definition.__setitem__("component_order", ["Points"]),
    ],
)
def test_definition_rejects_noncanonical_whitespace_and_casing(mutate) -> None:
    definition = _schema_v1_definition()
    mutate(definition)

    with pytest.raises(StatisticCatalogError):
        StatisticCatalog.from_mapping(definition)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda definition: definition["statistics"][0].__setitem__(
            "scoring_periods", "full_game"
        ),
        lambda definition: definition["statistics"][0].__setitem__("components", "points"),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", ["points"]
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": "points"}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": [["points"]]}
        ),
        lambda definition: definition.__setitem__("component_order", "points"),
        lambda definition: definition.__setitem__(
            "statistics", definition["statistics"][0]
        ),
    ],
)
def test_definition_rejects_wrong_container_shapes(mutate) -> None:
    definition = _schema_v1_definition()
    mutate(definition)

    with pytest.raises(StatisticCatalogError):
        StatisticCatalog.from_mapping(definition)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda definition: definition.__setitem__(
            "statistics", (definition["statistics"][0],)
        ),
        lambda definition: definition.__setitem__("component_order", ("points",)),
        lambda definition: definition.__setitem__("component_order", {"points"}),
        lambda definition: definition["statistics"][0].__setitem__(
            "scoring_periods", ("full_game",)
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "components", ("points",)
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "components", {"points"}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": ("points",)}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": {"labels": ("points",)}}
        ),
        lambda definition: definition["statistics"][0].__setitem__(
            "provider_mappings", {"dabble": [{"labels": ("points",)}]}
        ),
    ],
)
def test_definition_rejects_tuples_and_other_non_lists_in_list_fields(mutate) -> None:
    definition = _schema_v1_definition()
    mutate(definition)

    with pytest.raises(StatisticCatalogError, match="non-empty list"):
        StatisticCatalog.from_mapping(definition)


def test_constructed_statistics_keep_immutable_tuple_provider_mappings() -> None:
    statistic = CanonicalStatistic(
        id="points",
        label="Points",
        unit="count",
        scoring_periods=(ScoringPeriod.FULL_GAME,),
        components=("points",),
        provider_mappings={"dabble": ("POINTS",), "underdog": {"labels": ("Pts",)}},
    )

    assert statistic.provider_mappings["dabble"] == ("POINTS",)
    assert statistic.provider_mappings["underdog"] == ("Pts",)
    assert (
        StatisticCatalog(statistics=(statistic,))
        .resolve("dabble", "points", scoring_period=ScoringPeriod.FULL_GAME)
        .state
        is MatchState.CANONICAL
    )


def test_definition_keeps_exact_provider_label_presentation_casing() -> None:
    definition = _schema_v1_definition()
    definition["statistics"][0]["provider_mappings"] = {
        "dabble": ["POINTS", {"labels": ["Pts"]}]
    }

    catalog = StatisticCatalog.from_mapping(definition)

    assert catalog.by_id["points"].provider_mappings["dabble"] == ("POINTS", "Pts")
    assert (
        catalog.resolve("dabble", "points", scoring_period=ScoringPeriod.FULL_GAME).state
        is MatchState.CANONICAL
    )


def test_provider_evidence_stays_tolerant_while_definitions_stay_exact() -> None:
    resolver = StatisticCatalog.load_default().resolver

    match = resolver.resolve(
        " PrizePicks ",
        " points ",
        scoring_period="full game",
        unit=" Count ",
        components=(" Points ",),
    )

    assert match.state is MatchState.CANONICAL
    assert match.canonical is not None
    assert match.canonical.id == "points"


def test_typed_catalog_construction_requires_schema_version_one() -> None:
    statistic = CanonicalStatistic(
        id="points",
        label="Points",
        unit="count",
        scoring_periods=(ScoringPeriod.FULL_GAME,),
        components=("points",),
    )

    assert StatisticCatalog(statistics=(statistic,)).version == 1
    for version in (2, 0, -1, True, "1"):
        with pytest.raises(StatisticCatalogError, match="version"):
            StatisticCatalog(statistics=(statistic,), version=version)  # type: ignore[arg-type]


def test_resolver_defaults_omitted_period_evidence_to_unknown() -> None:
    resolver = StatisticCatalog.load_default().resolver

    match = resolver.resolve("prizepicks", "Points", unit="count")

    assert match.state is MatchState.UNMAPPED
    assert match.scoring_period is ScoringPeriod.UNKNOWN
    assert match.reason is MatchReason.UNKNOWN_SCORING_PERIOD
    assert match.canonical is None
    assert match.evidence.label == "Points"


def test_resolver_requires_explicit_full_game_evidence_for_canonical_matches() -> None:
    resolver = StatisticCatalog.load_default().resolver

    for period in (None, ScoringPeriod.UNKNOWN, "unknown", "made-up-period"):
        assert (
            resolver.resolve("prizepicks", "Points", scoring_period=period).state
            is MatchState.UNMAPPED
        )
    assert (
        resolver.resolve(
            "prizepicks", "Points", scoring_period=ScoringPeriod.FULL_GAME
        ).state
        is MatchState.CANONICAL
    )
    assert (
        resolver.resolve("prizepicks", "Points", scoring_period="full game").state
        is MatchState.CANONICAL
    )
    assert (
        resolver.resolve("prizepicks", "Points", scoring_period="full_game").state
        is MatchState.CANONICAL
    )


def test_resolver_reports_closed_reasons_for_each_unmapped_outcome() -> None:
    resolver = StatisticCatalog.load_default().resolver
    full_game = ScoringPeriod.FULL_GAME

    assert (
        resolver.resolve("prizepicks", None, scoring_period=full_game).reason
        is MatchReason.MISSING_STATISTIC_LABEL
    )
    assert (
        resolver.resolve("prizepicks", "made-up", scoring_period=full_game).reason
        is MatchReason.UNKNOWN_PROVIDER_LABEL
    )
    assert (
        resolver.resolve(
            "prizepicks", "Points", scoring_period=ScoringPeriod.FIRST_HALF
        ).reason
        is MatchReason.UNSUPPORTED_SCORING_PERIOD
    )
    assert (
        resolver.resolve(
            "prizepicks", "Points", scoring_period=full_game, unit="minutes"
        ).reason
        is MatchReason.UNIT_MISMATCH
    )
    assert (
        resolver.resolve(
            "prizepicks",
            "Points",
            scoring_period=full_game,
            unit="count",
            components=("rebounds",),
        ).reason
        is MatchReason.COMPONENT_MISMATCH
    )


def test_statistic_match_is_typed_immutable_and_validates_state_coherence() -> None:
    evidence = StatisticEvidence(label="Points")
    with pytest.raises(TypeError):
        StatisticMatch(
            state="unmapped",  # type: ignore[arg-type]
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            reason=MatchReason.UNKNOWN_PROVIDER_LABEL,
        )
    unmapped = StatisticMatch(
        state=MatchState.UNMAPPED,
        evidence=evidence,
        scoring_period=ScoringPeriod.FULL_GAME,
        reason=MatchReason.UNKNOWN_PROVIDER_LABEL,
    )
    with pytest.raises(AttributeError):
        unmapped.state = MatchState.CANONICAL
    with pytest.raises(ValueError):
        StatisticMatch(
            state=MatchState.UNMAPPED,
            evidence=evidence,
            canonical=StatisticCatalog.load_default().by_id["points"],
            scoring_period=ScoringPeriod.FULL_GAME,
            reason=MatchReason.UNKNOWN_PROVIDER_LABEL,
        )


def test_statistic_match_rejects_attribute_deletion_and_addition() -> None:
    match = StatisticMatch(
        state=MatchState.UNMAPPED,
        evidence=StatisticEvidence(label="Points"),
        scoring_period=ScoringPeriod.FULL_GAME,
        reason=MatchReason.UNKNOWN_PROVIDER_LABEL,
    )

    with pytest.raises(AttributeError):
        del match.state
    with pytest.raises(AttributeError):
        del match.reason
    with pytest.raises(AttributeError):
        match.reason = MatchReason.UNIT_MISMATCH
    # A frozen slotted value refuses an undeclared attribute too; CPython 3.11
    # reports that one as a TypeError rather than an AttributeError.
    with pytest.raises((AttributeError, TypeError)):
        match.canonical_id = "points"

    assert match.state is MatchState.UNMAPPED
    assert match.reason is MatchReason.UNKNOWN_PROVIDER_LABEL


def test_statistic_values_expose_one_name_for_each_reviewed_fact() -> None:
    catalog = StatisticCatalog.load_default()
    match = catalog.resolve(
        "prizepicks", "Points", scoring_period=ScoringPeriod.FULL_GAME
    )

    for duplicate in ("canonical_id", "name", "ordered_components", "period"):
        assert not hasattr(catalog.by_id["points"], duplicate)
    for duplicate in (
        "match_state",
        "status",
        "canonical_statistic",
        "statistic",
        "provider_evidence",
        "provider_label",
        "original_label",
    ):
        assert not hasattr(match, duplicate)

    assert match.canonical_id == "points"
    assert match.is_comparable


def test_statistic_match_closes_period_unit_and_reason_vocabularies() -> None:
    evidence = StatisticEvidence(label="Points")

    for invalid in (
        {"scoring_period": "full_game"},
        {"unit": "count"},
        {"reason": "unknown_provider_label"},
    ):
        arguments = {
            "scoring_period": ScoringPeriod.FULL_GAME,
            "reason": MatchReason.UNKNOWN_PROVIDER_LABEL,
            **invalid,
        }
        with pytest.raises(TypeError):
            StatisticMatch(state=MatchState.UNMAPPED, evidence=evidence, **arguments)

    with pytest.raises(ValueError):
        StatisticMatch(
            state=MatchState.UNMAPPED,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
        )
    with pytest.raises(ValueError):
        StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            canonical=StatisticCatalog.load_default().by_id["points"],
            scoring_period=ScoringPeriod.UNKNOWN,
        )

    match = StatisticMatch(
        state=MatchState.UNMAPPED,
        evidence=evidence,
        scoring_period=ScoringPeriod.UNKNOWN,
        unit=StatisticUnit.COUNT,
        reason=MatchReason.UNKNOWN_SCORING_PERIOD,
    )
    assert match.unit is StatisticUnit.COUNT
    assert match.reason is MatchReason.UNKNOWN_SCORING_PERIOD


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
            deadline=datetime.now(timezone.utc) + timedelta(seconds=5),
            request_id="statistic-board",
        ),
    )

    assert {market.provider for market in board.canonical_markets} == {
        "dabble",
        "prizepicks",
    }
    assert [
        market.statistic_match.canonical_id for market in board.canonical_markets
    ] == ["pra", "points"]
    assert [market.provider for market in board.unmapped_markets] == ["underdog"]
    unmapped = board.unmapped_markets[0]
    assert unmapped.statistic.label == "Fantasy Points"
    assert unmapped.statistic_match.state is MatchState.UNMAPPED
    assert unmapped.statistic_match.evidence.provider_id == "underdog-stat"


def test_board_marks_market_without_statistic_evidence_as_unmapped() -> None:
    retrieved_at = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    snapshot = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(PlayerProjectionMarket(provider="dabble", market_id="missing-stat"),),
        coverage=CoverageEvidence(
            fetched_count=1, eligible_count=1, normalized_count=1,
            pagination_complete=True, fanout_complete=True,
        ),
        retrieved_at=retrieved_at,
    )

    class Provider:
        def get_snapshot(self, query: NBAMarketQuery, context: RetrievalContext) -> ProviderSnapshot:
            return snapshot

    board = DFSBoardService(
        provider_registry={"dabble": Provider()}, deadline_seconds=5
    ).get_board(
        NBAMarketQuery(),
        RetrievalContext(deadline=datetime.now(timezone.utc) + timedelta(seconds=5)),
    )
    market = board.unmapped_markets[0]
    assert market.statistic is None
    assert market.statistic_match.state is MatchState.UNMAPPED
    assert market.statistic_match.reason is MatchReason.MISSING_STATISTIC_EVIDENCE
    assert board.resolved_markets[0].market_id == "missing-stat"

    # The unmapped match is attached during snapshot resolution, so it is
    # visible on the board's snapshots and not only on ``resolved_markets``.
    snapshot_market = board.snapshots[0].markets[0]
    assert snapshot_market.market_id == "missing-stat"
    assert snapshot_market.statistic is None
    assert snapshot_market.statistic_match is not None
    assert snapshot_market.statistic_match.state is MatchState.UNMAPPED
    assert snapshot_market.statistic_match.reason is MatchReason.MISSING_STATISTIC_EVIDENCE
    assert snapshot_market.statistic_match.provider == "dabble"
    with pytest.raises(AttributeError):
        snapshot_market.statistic_match.state = MatchState.CANONICAL


def _board_for(market: PlayerProjectionMarket):
    snapshot = ProviderSnapshot(
        provider=market.provider,
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1, eligible_count=1, normalized_count=1,
            pagination_complete=True, fanout_complete=True,
        ),
        retrieved_at=datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
    )

    class Provider:
        def get_snapshot(
            self, query: NBAMarketQuery, context: RetrievalContext
        ) -> ProviderSnapshot:
            return snapshot

    return DFSBoardService(
        provider_registry={market.provider: Provider()}, deadline_seconds=5
    ).get_board(
        NBAMarketQuery(),
        RetrievalContext(deadline=datetime.now(timezone.utc) + timedelta(seconds=5)),
    )


def test_board_leaves_mapped_label_without_period_evidence_unmapped() -> None:
    board = _board_for(
        PlayerProjectionMarket(
            provider="prizepicks",
            market_id="no-period",
            statistic=StatisticEvidence(provider_id="pp-stat", label="Points"),
            threshold=MarketThreshold("25.5", unit="count"),
        )
    )

    assert board.canonical_markets == ()
    market = board.unmapped_markets[0]
    assert market.scoring_period is ScoringPeriod.UNKNOWN
    assert market.statistic_match.state is MatchState.UNMAPPED
    assert market.statistic_match.reason is MatchReason.UNKNOWN_SCORING_PERIOD
    assert market.statistic_match.canonical is None


def test_board_resolution_leaves_provider_statistic_evidence_untouched() -> None:
    evidence = StatisticEvidence(
        provider_id="dabble-stat",
        label="assists+points+rebounds",
        components=("assists", "points", "rebounds"),
    )
    board = _board_for(
        PlayerProjectionMarket(
            provider="dabble",
            market_id="identity",
            statistic=evidence,
            threshold=MarketThreshold("30.5", unit="count"),
            scoring_period=ScoringPeriod.FULL_GAME,
        )
    )

    market = board.canonical_markets[0]
    assert market.statistic is evidence
    assert market.statistic == evidence
    assert market.statistic.canonical_id is None
    assert market.statistic.components == ("assists", "points", "rebounds")
    assert market.statistic_match.evidence is evidence
    assert market.statistic_match.canonical_id == "pra"
    assert market.statistic_match.canonical.components == (
        "points",
        "rebounds",
        "assists",
    )
