"""Reviewed, immutable statistic definitions for the DFS board.

Provider adapters retain source statistic labels as evidence. This module is
the only place that assigns a cross-provider statistic identity: a label is
canonical only when it has an explicit entry in the version-controlled
catalog and its period, unit, and component evidence agree with that entry.
Unknown values are returned as typed ``UNMAPPED`` matches instead of being
interpreted by a spelling heuristic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

import yaml

from app.providers.dfs import (
    MarketThreshold,
    PlayerProjectionMarket,
    ScoringPeriod,
    StatisticEvidence,
    normalize_scoring_period,
)


SUPPORTED_STATISTIC_PROVIDERS: tuple[str, ...] = (
    "dabble",
    "prizepicks",
    "underdog",
)


class StatisticCatalogError(ValueError):
    """A statistic definition cannot safely be loaded or resolved."""


class StatisticCatalogSchemaError(StatisticCatalogError):
    """The version-controlled statistic definition has an invalid schema."""


class MatchState(str, Enum):
    """The deliberately small statistic resolution state vocabulary."""

    CANONICAL = "canonical"
    UNMAPPED = "unmapped"

    # Descriptive aliases used by callers that refer to a resolved value as
    # "matched" rather than "canonical".
    MATCHED = "canonical"
    MAPPED = "canonical"


class StatisticUnit(str, Enum):
    """Units reviewed for catalog definitions."""

    COUNT = "count"
    DECIMAL = "decimal"
    MINUTES = "minutes"
    PERCENTAGE = "percentage"
    POINTS = "points"


_UNIT_VALUES = frozenset(unit.value for unit in StatisticUnit)
_IDENTIFIER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def _identifier(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise StatisticCatalogSchemaError(f"{field_name} must be a string")
    normalized = value.strip().casefold()
    if not normalized or normalized[0] not in "abcdefghijklmnopqrstuvwxyz":
        raise StatisticCatalogSchemaError(
            f"{field_name} must start with a lowercase alphabetic character"
        )
    if any(character not in _IDENTIFIER_CHARS for character in normalized):
        raise StatisticCatalogSchemaError(
            f"{field_name} must contain only lowercase letters, numbers, and underscores"
        )
    return normalized


def _label(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StatisticCatalogSchemaError(f"{field_name} must be a non-empty string")
    return value.strip()


def _label_key(value: str) -> str:
    """Normalize presentation case only; never infer statistic meaning."""

    return value.strip().casefold()


def _component_key(value: Any) -> str:
    """Normalize component spelling without assigning semantic meaning."""

    if not isinstance(value, str):
        return ""
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _provider(value: Any, *, field_name: str = "provider") -> str:
    if not isinstance(value, str) or not value.strip():
        raise StatisticCatalogSchemaError(f"{field_name} must be a non-empty string")
    normalized = value.strip().casefold()
    if normalized not in SUPPORTED_STATISTIC_PROVIDERS:
        raise StatisticCatalogSchemaError(
            f"{field_name} {value!r} is not one of the supported DFS providers"
        )
    return normalized


def _period(value: Any, *, field_name: str) -> ScoringPeriod:
    if isinstance(value, ScoringPeriod):
        normalized = value
    elif isinstance(value, str):
        try:
            normalized = ScoringPeriod(value.strip().casefold())
        except ValueError:
            normalized = normalize_scoring_period(value).value
    else:
        normalized = ScoringPeriod.UNKNOWN
    if normalized is ScoringPeriod.UNKNOWN:
        raise StatisticCatalogSchemaError(
            f"{field_name} contains an invalid scoring period {value!r}"
        )
    return normalized


def _unit(value: Any, *, field_name: str) -> StatisticUnit:
    if isinstance(value, StatisticUnit):
        normalized = value
    elif isinstance(value, str):
        normalized_value = value.strip().casefold().replace(" ", "_")
        try:
            normalized = StatisticUnit(normalized_value)
        except ValueError as error:
            raise StatisticCatalogSchemaError(
                f"{field_name} contains an invalid statistic unit {value!r}"
            ) from error
    else:
        raise StatisticCatalogSchemaError(f"{field_name} must be a string unit")
    if normalized.value not in _UNIT_VALUES:  # pragma: no cover - enum guard
        raise StatisticCatalogSchemaError(
            f"{field_name} contains an invalid statistic unit {value!r}"
        )
    return normalized


def _as_nonempty_list(value: Any, *, field_name: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise StatisticCatalogSchemaError(f"{field_name} must be a non-empty list")
    return tuple(value)


def _mapping_labels(value: Any, *, field_name: str) -> tuple[str, ...]:
    """Read one provider mapping in compact or explicit object form."""

    if isinstance(value, str):
        return (_label(value, field_name=field_name),)
    if isinstance(value, Mapping):
        allowed = {"label", "labels", "alias", "aliases"}
        unknown = set(value) - allowed
        if unknown:
            raise StatisticCatalogSchemaError(
                f"{field_name} contains unsupported mapping fields: {sorted(unknown)}"
            )
        values: list[Any] = []
        for key in ("label", "labels", "alias", "aliases"):
            if key not in value:
                continue
            candidate = value[key]
            if key in {"label", "alias"}:
                values.append(candidate)
            else:
                values.extend(
                    _as_nonempty_list(candidate, field_name=f"{field_name}.{key}")
                )
        if not values:
            raise StatisticCatalogSchemaError(f"{field_name} must contain a label")
        return tuple(_label(candidate, field_name=field_name) for candidate in values)
    if isinstance(value, (list, tuple)):
        if not value:
            raise StatisticCatalogSchemaError(f"{field_name} must contain labels")
        labels: list[str] = []
        for index, item in enumerate(value):
            labels.extend(_mapping_labels(item, field_name=f"{field_name}[{index}]"))
        return tuple(labels)
    raise StatisticCatalogSchemaError(
        f"{field_name} must be a label, list of labels, or explicit mapping"
    )


def _provider_mappings(value: Any, *, field_name: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if isinstance(value, (list, tuple)):
        mappings: dict[str, tuple[str, ...]] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise StatisticCatalogSchemaError(
                    f"{field_name}[{index}] must be an object"
                )
            allowed = {"provider", "label", "labels", "alias", "aliases"}
            unknown = set(item) - allowed
            if unknown or "provider" not in item:
                raise StatisticCatalogSchemaError(
                    f"{field_name}[{index}] requires provider and labels"
                )
            provider = _provider(
                item["provider"], field_name=f"{field_name}[{index}].provider"
            )
            if provider in mappings:
                raise StatisticCatalogSchemaError(
                    f"duplicate provider mapping identity for {provider!r}"
                )
            labels = _mapping_labels(
                {
                    key: item[key]
                    for key in ("label", "labels", "alias", "aliases")
                    if key in item
                },
                field_name=f"{field_name}[{index}]",
            )
            mappings[provider] = labels
        return mappings
    if not isinstance(value, Mapping):
        raise StatisticCatalogSchemaError(f"{field_name} must be an object by provider")
    mappings: dict[str, tuple[str, ...]] = {}
    for raw_provider, raw_labels in value.items():
        provider = _provider(raw_provider, field_name=f"{field_name} provider")
        if provider in mappings:
            raise StatisticCatalogSchemaError(
                f"duplicate provider mapping identity for {provider!r}"
            )
        mappings[provider] = _mapping_labels(
            raw_labels, field_name=f"{field_name}.{provider}"
        )
    return mappings


@dataclass(frozen=True, slots=True)
class CanonicalStatistic:
    """One reviewed canonical statistic definition."""

    id: str
    label: str
    unit: StatisticUnit | str
    scoring_periods: tuple[ScoringPeriod | str, ...]
    components: tuple[str, ...]
    provider_mappings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    comparable: bool = True

    def __post_init__(self) -> None:
        statistic_id = _identifier(self.id, field_name="statistic id")
        display_label = _label(self.label, field_name=f"statistic {statistic_id} label")
        unit = _unit(self.unit, field_name=f"statistic {statistic_id} unit")
        periods = tuple(
            _period(value, field_name=f"statistic {statistic_id} scoring_periods")
            for value in self.scoring_periods
        )
        if not periods:
            raise StatisticCatalogSchemaError(
                f"statistic {statistic_id} scoring_periods must not be empty"
            )
        if len(set(periods)) != len(periods):
            raise StatisticCatalogSchemaError(
                f"statistic {statistic_id} has duplicate scoring periods"
            )
        components = tuple(
            _identifier(value, field_name=f"statistic {statistic_id} component")
            for value in self.components
        )
        if not components:
            raise StatisticCatalogSchemaError(
                f"statistic {statistic_id} components must not be empty"
            )
        if len(set(components)) != len(components):
            raise StatisticCatalogSchemaError(
                f"statistic {statistic_id} has duplicate ordered components"
            )
        if not isinstance(self.comparable, bool):
            raise StatisticCatalogSchemaError(
                f"statistic {statistic_id} comparable must be boolean"
            )
        mappings: dict[str, tuple[str, ...]] = {}
        for raw_provider, raw_labels in self.provider_mappings.items():
            provider = _provider(
                raw_provider,
                field_name=f"statistic {statistic_id} provider mapping provider",
            )
            if provider in mappings:
                raise StatisticCatalogSchemaError(
                    f"duplicate provider mapping identity for {provider!r}"
                )
            labels = _mapping_labels(
                raw_labels,
                field_name=f"statistic {statistic_id} provider mapping {provider}",
            )
            if len({_label_key(value) for value in labels}) != len(labels):
                raise StatisticCatalogSchemaError(
                    f"duplicate provider mapping labels for {provider!r}"
                )
            mappings[provider] = labels
        object.__setattr__(self, "id", statistic_id)
        object.__setattr__(self, "label", display_label)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "scoring_periods", periods)
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "provider_mappings", MappingProxyType(mappings))

    @property
    def canonical_id(self) -> str:
        return self.id

    @property
    def name(self) -> str:
        return self.label

    @property
    def ordered_components(self) -> tuple[str, ...]:
        return self.components

    @property
    def period(self) -> ScoringPeriod | None:
        return self.scoring_periods[0] if len(self.scoring_periods) == 1 else None


@dataclass(frozen=True, slots=True)
class StatisticMapping:
    """One explicit provider-label-to-canonical identity mapping."""

    provider: str
    label: str
    canonical_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider(self.provider))
        object.__setattr__(self, "label", _label(self.label, field_name="mapping label"))
        object.__setattr__(
            self,
            "canonical_id",
            _identifier(self.canonical_id, field_name="mapping canonical_id"),
        )


@dataclass(frozen=True, slots=True)
class StatisticMatch:
    """A canonical or explicitly unmapped statistic resolution."""

    state: MatchState | str
    evidence: StatisticEvidence
    canonical: CanonicalStatistic | None = None
    provider: str | None = None
    scoring_period: ScoringPeriod | str = ScoringPeriod.UNKNOWN
    unit: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        try:
            state = self.state if isinstance(self.state, MatchState) else MatchState(self.state)
        except (TypeError, ValueError) as error:
            raise ValueError("statistic match state must be canonical or unmapped") from error
        if not isinstance(self.evidence, StatisticEvidence):
            raise TypeError("statistic match evidence must be StatisticEvidence")
        if state is MatchState.CANONICAL and not isinstance(self.canonical, CanonicalStatistic):
            raise ValueError("canonical statistic matches require a canonical statistic")
        if state is MatchState.UNMAPPED and self.canonical is not None:
            raise ValueError("unmapped statistic matches cannot carry a canonical statistic")
        provider = None
        if self.provider is not None:
            if not isinstance(self.provider, str) or not self.provider.strip():
                raise ValueError("statistic match provider must be a non-empty string or None")
            # Resolution keeps unfamiliar provider evidence visible as
            # UNMAPPED. Catalog definitions themselves still accept only the
            # three reviewed provider names.
            provider = self.provider.strip().casefold()
        period = _runtime_period(self.scoring_period)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "scoring_period", period)

    @property
    def match_state(self) -> MatchState:
        return self.state

    @property
    def canonical_id(self) -> str | None:
        return self.canonical.id if self.canonical is not None else None

    @property
    def canonical_statistic(self) -> CanonicalStatistic | None:
        return self.canonical

    @property
    def statistic(self) -> CanonicalStatistic | None:
        return self.canonical

    @property
    def status(self) -> MatchState:
        return self.state

    @property
    def provider_evidence(self) -> StatisticEvidence:
        return self.evidence

    @property
    def provider_label(self) -> str | None:
        return self.evidence.label

    @property
    def original_label(self) -> str | None:
        return self.evidence.label

    @property
    def is_comparable(self) -> bool:
        return self.state is MatchState.CANONICAL and bool(
            self.canonical is not None and self.canonical.comparable
        )


StatisticResolution = StatisticMatch


@dataclass(frozen=True, slots=True)
class StatisticCatalog:
    """Immutable catalog built from one validated definition document."""

    statistics: tuple[CanonicalStatistic, ...]
    version: int = 1
    by_id: Mapping[str, CanonicalStatistic] = field(init=False)
    mappings: tuple[StatisticMapping, ...] = field(init=False)
    _label_index: Mapping[tuple[str, str], CanonicalStatistic] = field(
        init=False, repr=False, compare=False
    )

    DEFAULT_PATH: ClassVar[Path] = (
        Path(__file__).resolve().parents[1] / "config" / "statistic_catalog.yaml"
    )

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise StatisticCatalogSchemaError(
                "statistic catalog version must be a positive integer"
            )
        statistics = tuple(self.statistics)
        if not statistics:
            raise StatisticCatalogSchemaError("statistic catalog statistics must not be empty")
        if any(not isinstance(statistic, CanonicalStatistic) for statistic in statistics):
            raise StatisticCatalogSchemaError(
                "statistic catalog entries must be CanonicalStatistic values"
            )
        by_id: dict[str, CanonicalStatistic] = {}
        for statistic in statistics:
            if statistic.id in by_id:
                raise StatisticCatalogSchemaError(
                    f"duplicate canonical statistic id {statistic.id!r}"
                )
            by_id[statistic.id] = statistic

        component_order = _component_order(statistics)
        component_positions = {value: index for index, value in enumerate(component_order)}
        for statistic in statistics:
            missing = [component for component in statistic.components if component not in by_id]
            if missing:
                raise StatisticCatalogSchemaError(
                    f"statistic {statistic.id!r} references unknown ordered components {missing!r}"
                )
            if any(by_id[component].components != (component,) for component in statistic.components):
                raise StatisticCatalogSchemaError(
                    f"statistic {statistic.id!r} components must reference atomic statistics"
                )
            positions = [component_positions[component] for component in statistic.components]
            if positions != sorted(positions):
                raise StatisticCatalogSchemaError(
                    f"statistic {statistic.id!r} has inconsistent ordered components"
                )

        label_index: dict[tuple[str, str], CanonicalStatistic] = {}
        mappings: list[StatisticMapping] = []
        for statistic in statistics:
            for provider, labels in statistic.provider_mappings.items():
                for source_label in labels:
                    key = (provider, _label_key(source_label))
                    previous = label_index.get(key)
                    if previous is not None:
                        if previous.id == statistic.id:
                            raise StatisticCatalogSchemaError(
                                f"duplicate provider mapping identity {provider!r}:{source_label!r}"
                            )
                        raise StatisticCatalogSchemaError(
                            "conflicting provider mappings for "
                            f"{provider!r}:{source_label!r} ({previous.id!r} vs {statistic.id!r})"
                        )
                    label_index[key] = statistic
                    mappings.append(
                        StatisticMapping(
                            provider=provider,
                            label=source_label,
                            canonical_id=statistic.id,
                        )
                    )
        object.__setattr__(self, "statistics", statistics)
        object.__setattr__(self, "by_id", MappingProxyType(by_id))
        object.__setattr__(self, "mappings", tuple(mappings))
        object.__setattr__(self, "_label_index", MappingProxyType(label_index))

    @classmethod
    def load_default(cls) -> "StatisticCatalog":
        return cls.load(cls.DEFAULT_PATH)

    @classmethod
    def default(cls) -> "StatisticCatalog":
        return cls.load_default()

    @classmethod
    def load(cls, path: str | Path) -> "StatisticCatalog":
        """Load and validate one YAML definition document."""

        definition_path = Path(path)
        try:
            with definition_path.open("r", encoding="utf-8") as stream:
                definition = yaml.load(stream, Loader=_UniqueKeyLoader)
        except FileNotFoundError as error:
            raise StatisticCatalogError(
                f"statistic catalog definition was not found: {definition_path}"
            ) from error
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise StatisticCatalogError(
                f"statistic catalog definition could not be loaded: {definition_path}"
            ) from error
        return cls.from_mapping(definition, source=definition_path)

    @classmethod
    def from_file(cls, path: str | Path) -> "StatisticCatalog":
        return cls.load(path)

    @classmethod
    def load_from_file(cls, path: str | Path) -> "StatisticCatalog":
        return cls.load(path)

    @classmethod
    def from_definition(
        cls,
        definition: Mapping[str, Any],
        *,
        source: str | Path | None = None,
    ) -> "StatisticCatalog":
        return cls.from_mapping(definition, source=source)

    @classmethod
    def from_mapping(
        cls,
        definition: Mapping[str, Any],
        *,
        source: str | Path | None = None,
    ) -> "StatisticCatalog":
        """Validate a decoded definition, useful for isolated contract tests."""

        location = f" in {source}" if source is not None else ""
        try:
            parsed = _parse_definition(definition)
            return cls(**parsed)
        except StatisticCatalogError as error:
            if source is None:
                raise
            raise StatisticCatalogSchemaError(
                f"invalid statistic catalog schema{location}: {error}"
            ) from error
        except (TypeError, ValueError, KeyError) as error:
            raise StatisticCatalogSchemaError(
                f"invalid statistic catalog schema{location}: {error}"
            ) from error

    @property
    def resolver(self) -> "StatisticResolver":
        return StatisticResolver(self)

    def resolve(self, *args: Any, **kwargs: Any) -> StatisticMatch:
        return self.resolver.resolve(*args, **kwargs)

    def resolve_statistic(self, *args: Any, **kwargs: Any) -> StatisticMatch:
        return self.resolve(*args, **kwargs)


class StatisticResolver:
    """Resolve explicit provider evidence without semantic heuristics."""

    def __init__(self, catalog: StatisticCatalog) -> None:
        if not isinstance(catalog, StatisticCatalog):
            raise TypeError("statistic resolver requires a StatisticCatalog")
        self.catalog = catalog

    def resolve(
        self,
        provider: str | PlayerProjectionMarket,
        label: str | StatisticEvidence | None = None,
        *,
        scoring_period: ScoringPeriod | str | None = ScoringPeriod.FULL_GAME,
        period: ScoringPeriod | str | None = None,
        unit: str | None = None,
        components: Sequence[str] | None = None,
        evidence: StatisticEvidence | None = None,
    ) -> StatisticMatch:
        """Resolve one label, retaining the exact provider evidence."""

        if isinstance(provider, PlayerProjectionMarket) and label is None and evidence is None:
            return self.resolve_market(provider)
        if isinstance(label, StatisticEvidence):
            if evidence is not None:
                raise TypeError("statistic evidence was supplied twice")
            evidence = label
            label = evidence.label
        if period is not None:
            if scoring_period is not None and scoring_period is not ScoringPeriod.FULL_GAME:
                raise TypeError("use scoring_period or period, not both")
            scoring_period = period

        normalized_provider = provider.strip().casefold() if isinstance(provider, str) else ""
        if evidence is None:
            source_label = label
            source_components = tuple(components or ())
            evidence = StatisticEvidence(label=source_label, components=source_components)
        else:
            if not isinstance(evidence, StatisticEvidence):
                raise TypeError("statistic resolver evidence must be StatisticEvidence")
            source_label = evidence.label if label is None else label
            source_components = tuple(components) if components is not None else evidence.components
            if source_label != evidence.label or source_components != evidence.components:
                evidence = StatisticEvidence(
                    provider_id=evidence.provider_id,
                    canonical_id=evidence.canonical_id,
                    label=source_label,
                    components=source_components,
                    match_state=getattr(evidence, "match_state", None),
                )
        period = _runtime_period(scoring_period)
        if not source_label or period is ScoringPeriod.UNKNOWN:
            return self._unmapped(
                evidence,
                provider=normalized_provider,
                scoring_period=period,
                unit=unit,
                reason="unknown_statistic_or_scoring_period",
            )
        canonical = self.catalog._label_index.get(
            (normalized_provider, _label_key(source_label))
        )
        if canonical is None:
            return self._unmapped(
                evidence,
                provider=normalized_provider,
                scoring_period=period,
                unit=unit,
                reason="unknown_provider_label",
            )
        if period not in canonical.scoring_periods:
            return self._unmapped(
                evidence,
                provider=normalized_provider,
                scoring_period=period,
                unit=unit,
                reason="unsupported_scoring_period",
            )
        if unit is not None and _runtime_unit(unit) != canonical.unit:
            return self._unmapped(
                evidence,
                provider=normalized_provider,
                scoring_period=period,
                unit=unit,
                reason="unit_mismatch",
            )
        if source_components:
            try:
                normalized_components = tuple(
                    _identifier(
                        _component_key(component),
                        field_name="provider statistic component",
                    )
                    for component in source_components
                )
            except StatisticCatalogError:
                normalized_components = ()
            if (
                len(normalized_components) != len(set(normalized_components))
                or set(normalized_components) != set(canonical.components)
            ):
                return self._unmapped(
                    evidence,
                    provider=normalized_provider,
                    scoring_period=period,
                    unit=unit,
                    reason="component_mismatch",
                )
        return StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            canonical=canonical,
            provider=normalized_provider,
            scoring_period=period,
            unit=canonical.unit.value,
        )

    def resolve_evidence(
        self,
        provider: str,
        evidence: StatisticEvidence,
        *,
        scoring_period: ScoringPeriod | str | None = ScoringPeriod.FULL_GAME,
        period: ScoringPeriod | str | None = None,
        unit: str | None = None,
    ) -> StatisticMatch:
        return self.resolve(
            provider,
            evidence.label,
            scoring_period=scoring_period,
            period=period,
            unit=unit,
            components=evidence.components,
            evidence=evidence,
        )

    def resolve_statistic(
        self,
        provider: str,
        evidence: StatisticEvidence,
        *,
        scoring_period: ScoringPeriod | str | None = ScoringPeriod.FULL_GAME,
        period: ScoringPeriod | str | None = None,
        unit: str | None = None,
    ) -> StatisticMatch:
        return self.resolve_evidence(
            provider,
            evidence,
            scoring_period=scoring_period,
            period=period,
            unit=unit,
        )

    def resolve_market(self, market: PlayerProjectionMarket) -> StatisticMatch:
        if not isinstance(market, PlayerProjectionMarket):
            raise TypeError("statistic resolver market must be PlayerProjectionMarket")
        evidence = market.statistic or StatisticEvidence()
        threshold = market.threshold
        unit = threshold.unit if isinstance(threshold, MarketThreshold) else None
        return self.resolve_evidence(
            market.provider,
            evidence,
            scoring_period=market.scoring_period,
            unit=unit,
        )

    def _unmapped(
        self,
        evidence: StatisticEvidence,
        *,
        provider: str,
        scoring_period: ScoringPeriod,
        unit: str | None,
        reason: str,
    ) -> StatisticMatch:
        return StatisticMatch(
            state=MatchState.UNMAPPED,
            evidence=evidence,
            provider=provider or None,
            scoring_period=scoring_period,
            unit=unit,
            reason=reason,
        )


def _runtime_period(value: ScoringPeriod | str | None) -> ScoringPeriod:
    if value is None:
        return ScoringPeriod.UNKNOWN
    if isinstance(value, ScoringPeriod):
        return value
    if not isinstance(value, str):
        return ScoringPeriod.UNKNOWN
    return normalize_scoring_period(value).value


def _runtime_unit(value: str) -> StatisticUnit | None:
    try:
        return _unit(value, field_name="provider statistic unit")
    except StatisticCatalogError:
        return None


def _component_order(statistics: Sequence[CanonicalStatistic]) -> tuple[str, ...]:
    order: list[str] = []
    for statistic in statistics:
        for component in statistic.components:
            if component not in order:
                order.append(component)
    return tuple(order)


def _parse_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the small, intentionally explicit YAML schema."""

    if not isinstance(definition, Mapping):
        raise StatisticCatalogSchemaError("definition must be an object")
    allowed = {
        "schema_version",
        "version",
        "component_order",
        "statistics",
        "canonical_statistics",
    }
    unknown = set(definition) - allowed
    if unknown:
        raise StatisticCatalogSchemaError(
            f"unsupported top-level fields: {sorted(unknown)}"
        )
    version = definition.get("schema_version", definition.get("version"))
    if version is None:
        raise StatisticCatalogSchemaError("schema_version is required")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise StatisticCatalogSchemaError("schema_version must be a positive integer")
    if (
        "schema_version" in definition
        and "version" in definition
        and definition["schema_version"] != definition["version"]
    ):
        raise StatisticCatalogSchemaError("schema_version and version conflict")
    raw_statistics = definition.get(
        "statistics", definition.get("canonical_statistics")
    )
    raw_statistics = _as_nonempty_list(raw_statistics, field_name="statistics")

    parsed: list[CanonicalStatistic] = []
    for index, raw_statistic in enumerate(raw_statistics):
        if not isinstance(raw_statistic, Mapping):
            raise StatisticCatalogSchemaError(f"statistics[{index}] must be an object")
        allowed_statistic = {
            "id",
            "canonical_id",
            "label",
            "display_name",
            "name",
            "unit",
            "periods",
            "scoring_periods",
            "period",
            "components",
            "ordered_components",
            "provider_mappings",
            "mappings",
            "provider_labels",
            "comparable",
            "comparison_allowed",
        }
        unknown_statistic = set(raw_statistic) - allowed_statistic
        if unknown_statistic:
            raise StatisticCatalogSchemaError(
                f"statistics[{index}] has unsupported fields: {sorted(unknown_statistic)}"
            )
        statistic_id = raw_statistic.get(
            "id", raw_statistic.get("canonical_id")
        )
        display_label = raw_statistic.get(
            "label", raw_statistic.get("display_name", raw_statistic.get("name"))
        )
        periods = raw_statistic.get(
            "scoring_periods",
            raw_statistic.get("periods", raw_statistic.get("period")),
        )
        components = raw_statistic.get(
            "components", raw_statistic.get("ordered_components")
        )
        mappings = raw_statistic.get(
            "provider_mappings",
            raw_statistic.get(
                "mappings", raw_statistic.get("provider_labels")
            ),
        )
        if (
            statistic_id is None
            or display_label is None
            or periods is None
            or components is None
        ):
            raise StatisticCatalogSchemaError(
                f"statistics[{index}] requires id, label, scoring_periods, and components"
            )
        raw_components = _as_nonempty_list(
            components, field_name=f"statistics[{index}].components"
        )
        raw_periods = (periods,) if isinstance(periods, str) else periods
        parsed.append(
            CanonicalStatistic(
                id=_identifier(statistic_id, field_name=f"statistics[{index}].id"),
                label=_label(display_label, field_name=f"statistics[{index}].label"),
                unit=_unit(
                    raw_statistic.get("unit"),
                    field_name=f"statistics[{index}].unit",
                ),
                scoring_periods=tuple(
                    _period(
                        value,
                        field_name=f"statistics[{index}].scoring_periods",
                    )
                    for value in _as_nonempty_list(
                        raw_periods,
                        field_name=f"statistics[{index}].scoring_periods",
                    )
                ),
                components=tuple(
                    _identifier(value, field_name=f"statistics[{index}].components")
                    for value in raw_components
                ),
                provider_mappings=_provider_mappings(
                    mappings, field_name=f"statistics[{index}].provider_mappings"
                ),
                comparable=raw_statistic.get(
                    "comparable", raw_statistic.get("comparison_allowed", True)
                ),
            )
        )

    explicit_order = definition.get("component_order")
    if explicit_order is not None:
        order = tuple(
            _identifier(value, field_name="component_order")
            for value in _as_nonempty_list(
                explicit_order, field_name="component_order"
            )
        )
        if len(set(order)) != len(order):
            raise StatisticCatalogSchemaError(
                "component_order contains duplicate identities"
            )
        actual_order = _component_order(parsed)
        if set(order) != set(actual_order):
            raise StatisticCatalogSchemaError(
                "component_order must contain every referenced ordered component exactly once"
            )
        positions = {component: index for index, component in enumerate(order)}
        for statistic in parsed:
            if [positions[value] for value in statistic.components] != sorted(
                positions[value] for value in statistic.components
            ):
                raise StatisticCatalogSchemaError(
                    f"statistic {statistic.id!r} has inconsistent ordered components"
                )
    return {"statistics": tuple(parsed), "version": version}


class _UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML loader that does not discard duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.Node, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate definition key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


__all__ = [
    "CanonicalStatistic",
    "MatchState",
    "StatisticMatchState",
    "StatisticCatalog",
    "StatisticCatalogError",
    "StatisticCatalogSchemaError",
    "StatisticMapping",
    "StatisticMatch",
    "StatisticResolution",
    "StatisticResolver",
    "StatisticUnit",
    "SUPPORTED_STATISTIC_PROVIDERS",
    "load_statistic_catalog",
]


StatisticMatchState = MatchState


def load_statistic_catalog(path: str | Path | None = None) -> StatisticCatalog:
    """Load the application catalog or an explicitly supplied definition."""

    return StatisticCatalog.load_default() if path is None else StatisticCatalog.load(path)
