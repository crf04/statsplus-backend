"""Shared, provider-neutral contract for NBA daily-fantasy markets.

Provider adapters own discovery and wire-format details.  This module owns
the small semantic vocabulary exchanged with the rest of the application and
the immutable snapshot models returned by adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal, InvalidOperation
from math import isfinite
from types import MappingProxyType
from typing import Generic, Protocol, TypeVar, runtime_checkable


class MarketVariant(str, Enum):
    """The reviewed set of market variants understood by the board."""

    STANDARD = "standard"
    ALTERNATE = "alternate"
    PROMOTIONAL = "promotional"
    UNKNOWN = "unknown"


class SelectionDirection(str, Enum):
    """The reviewed semantic direction of a player projection selection."""

    HIGHER = "higher"
    LOWER = "lower"
    UNKNOWN = "unknown"


class MarketStatus(str, Enum):
    """The eligible market availability states in a provider snapshot."""

    AVAILABLE = "available"
    SUSPENDED = "suspended"


class ScoringPeriod(str, Enum):
    """Reviewed scoring periods; unknown provider labels remain evidence."""

    FULL_GAME = "full_game"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    FIRST_QUARTER = "first_quarter"
    SECOND_QUARTER = "second_quarter"
    UNKNOWN = "unknown"


EnumValue = TypeVar("EnumValue", bound=Enum)


@dataclass(frozen=True, slots=True)
class NormalizedLabel(Generic[EnumValue]):
    """A closed normalized value together with its provider label."""

    value: EnumValue
    original_label: str | None


def normalize_market_variant(label: str | MarketVariant | None) -> NormalizedLabel[MarketVariant]:
    """Normalize a provider variant label without guessing unknown values."""

    if isinstance(label, MarketVariant):
        return NormalizedLabel(label, label.value)

    normalized = label.strip().casefold() if isinstance(label, str) else ""
    values = {
        "standard": MarketVariant.STANDARD,
        "default": MarketVariant.STANDARD,
        "main": MarketVariant.STANDARD,
        "alternate": MarketVariant.ALTERNATE,
        "alt": MarketVariant.ALTERNATE,
        "promotional": MarketVariant.PROMOTIONAL,
        "promotion": MarketVariant.PROMOTIONAL,
        "promo": MarketVariant.PROMOTIONAL,
    }
    return NormalizedLabel(values.get(normalized, MarketVariant.UNKNOWN), label)


def normalize_selection_direction(
    label: str | SelectionDirection | None,
) -> NormalizedLabel[SelectionDirection]:
    """Normalize higher/lower labels and retain unfamiliar labels verbatim."""

    if isinstance(label, SelectionDirection):
        return NormalizedLabel(label, label.value)

    normalized = label.strip().casefold() if isinstance(label, str) else ""
    values = {
        "higher": SelectionDirection.HIGHER,
        "high": SelectionDirection.HIGHER,
        "over": SelectionDirection.HIGHER,
        "more": SelectionDirection.HIGHER,
        "more than": SelectionDirection.HIGHER,
        "greater": SelectionDirection.HIGHER,
        "greater than": SelectionDirection.HIGHER,
        "above": SelectionDirection.HIGHER,
        "lower": SelectionDirection.LOWER,
        "low": SelectionDirection.LOWER,
        "under": SelectionDirection.LOWER,
        "less": SelectionDirection.LOWER,
        "less than": SelectionDirection.LOWER,
        "below": SelectionDirection.LOWER,
    }
    return NormalizedLabel(values.get(normalized, SelectionDirection.UNKNOWN), label)


def normalize_market_status(label: str | MarketStatus | None) -> NormalizedLabel[MarketStatus]:
    """Normalize only eligible market statuses.

    Closed, settled, and live statuses are intentionally not mapped into this
    vocabulary.  Adapters must exclude them with coverage evidence before
    constructing a normalized market.
    """

    if isinstance(label, MarketStatus):
        return NormalizedLabel(label, label.value)
    normalized = label.strip().casefold() if isinstance(label, str) else ""
    values = {
        "available": MarketStatus.AVAILABLE,
        "active": MarketStatus.AVAILABLE,
        "open": MarketStatus.AVAILABLE,
        "suspended": MarketStatus.SUSPENDED,
        "paused": MarketStatus.SUSPENDED,
    }
    status = values.get(normalized)
    if status is None:
        raise ValueError(
            "market status is not an eligible available or suspended value"
        )
    return NormalizedLabel(status, label)


def normalize_scoring_period(
    label: str | ScoringPeriod | None,
) -> NormalizedLabel[ScoringPeriod]:
    """Normalize reviewed scoring periods without guessing unknown labels."""

    if isinstance(label, ScoringPeriod):
        original_label = None if label is ScoringPeriod.UNKNOWN else label.value
        return NormalizedLabel(label, original_label)
    normalized = label.strip().casefold().replace("-", " ") if isinstance(label, str) else ""
    values = {
        "full game": ScoringPeriod.FULL_GAME,
        "game": ScoringPeriod.FULL_GAME,
        "full": ScoringPeriod.FULL_GAME,
        "first half": ScoringPeriod.FIRST_HALF,
        "second half": ScoringPeriod.SECOND_HALF,
        "first quarter": ScoringPeriod.FIRST_QUARTER,
        "second quarter": ScoringPeriod.SECOND_QUARTER,
    }
    return NormalizedLabel(values.get(normalized, ScoringPeriod.UNKNOWN), label)


def _decimal_value(value: Decimal | int | str | float, *, field: str) -> Decimal:
    """Convert an upstream numeric value without introducing float arithmetic."""

    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not decimal.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return decimal


@dataclass(frozen=True, slots=True)
class MarketThreshold:
    """Exact threshold evidence for one player projection market."""

    value: Decimal | int | str | float
    unit: str
    original_value: str | None = None

    def __post_init__(self) -> None:
        decimal = _decimal_value(self.value, field="threshold value")
        unit = self.unit.strip() if isinstance(self.unit, str) else ""
        if not unit:
            raise ValueError("threshold unit must be a non-empty string")
        original = self.original_value
        if original is not None and not isinstance(original, str):
            raise ValueError("threshold original_value must be a string or None")
        object.__setattr__(self, "value", decimal)
        object.__setattr__(self, "unit", unit)

    @property
    def displayed_value(self) -> str | None:
        """Alias for the provider's original displayed threshold value."""

        return self.original_value


@dataclass(frozen=True, slots=True)
class SelectionModifier:
    """Provider-defined selection adjustment, never an entry payout."""

    value: Decimal | int | str | float
    kind: str
    scope: str
    label: str

    def __post_init__(self) -> None:
        decimal = _decimal_value(self.value, field="selection modifier value")
        kind = self.kind.strip() if isinstance(self.kind, str) else ""
        scope = self.scope.strip() if isinstance(self.scope, str) else ""
        label = self.label if isinstance(self.label, str) else ""
        if not kind:
            raise ValueError("selection modifier kind must be a non-empty string")
        if not scope:
            raise ValueError("selection modifier scope must be a non-empty string")
        if not label.strip():
            raise ValueError("selection modifier label must be a non-empty string")
        object.__setattr__(self, "value", decimal)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scope", scope)

    @property
    def original_label(self) -> str:
        """Alias used by normalized-label consumers."""

        return self.label


def normalize_timestamp(value: datetime | str | None) -> datetime | None:
    """Return an aware timestamp in UTC; never assume a timezone."""

    if value is None:
        return None
    timestamp = value
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("timestamp must be a valid ISO-8601 value") from error
    if not isinstance(timestamp, datetime):
        raise ValueError("timestamp must be a datetime, ISO-8601 string, or None")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(timezone.utc)


def _optional_identifier(value: str | int | None, *, field: str) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-empty identifier")
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{field} must be a non-empty identifier")
        return value
    if isinstance(value, int):
        return value
    raise ValueError(f"{field} must be a string, integer, or None")


@dataclass(frozen=True, slots=True)
class TeamEvidence:
    """Provider and canonical evidence for one NBA team."""

    provider_id: str | None = None
    canonical_id: int | None = None
    name: str | None = None
    abbreviation: str | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(self.provider_id, field="team provider_id")
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("team provider_id must be a string or None")
        if self.canonical_id is not None and (
            isinstance(self.canonical_id, bool) or not isinstance(self.canonical_id, int)
        ):
            raise ValueError("team canonical_id must be an integer or None")
        name = self.name.strip() if isinstance(self.name, str) else None
        abbreviation = (
            self.abbreviation.strip().upper()
            if isinstance(self.abbreviation, str)
            else None
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "name", name or None)
        object.__setattr__(self, "abbreviation", abbreviation or None)

    @property
    def label(self) -> str | None:
        """The original provider team label, when supplied."""

        return self.name


@dataclass(frozen=True, slots=True)
class AthleteEvidence:
    """Provider and canonical evidence for one NBA athlete."""

    provider_id: str | None = None
    canonical_id: int | None = None
    name: str | None = None
    team: TeamEvidence | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(
            self.provider_id, field="athlete provider_id"
        )
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("athlete provider_id must be a string or None")
        if self.canonical_id is not None and (
            isinstance(self.canonical_id, bool) or not isinstance(self.canonical_id, int)
        ):
            raise ValueError("athlete canonical_id must be an integer or None")
        name = self.name.strip() if isinstance(self.name, str) else None
        if self.team is not None and not isinstance(self.team, TeamEvidence):
            raise ValueError("athlete team must be TeamEvidence or None")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "name", name or None)

    @property
    def label(self) -> str | None:
        """The original provider athlete label, when supplied."""

        return self.name


@dataclass(frozen=True, slots=True)
class AppearanceEvidence:
    """Provider evidence for the appearance that owns a market line."""

    provider_id: str | None = None
    appearance_type: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(
            self.provider_id, field="appearance provider_id"
        )
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("appearance provider_id must be a string or None")
        appearance_type = (
            self.appearance_type.strip()
            if isinstance(self.appearance_type, str)
            else None
        )
        label = self.label.strip() if isinstance(self.label, str) else None
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "appearance_type", appearance_type or None)
        object.__setattr__(self, "label", label or None)

    @property
    def appearance_id(self) -> str | None:
        """Alias clarifying that ``provider_id`` is the upstream ID."""

        return self.provider_id

    @property
    def type(self) -> str | None:
        """The provider's appearance type label."""

        return self.appearance_type


@dataclass(frozen=True, slots=True)
class EventEvidence:
    """Provider and canonical evidence for one NBA game/event."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None
    starts_at: datetime | str | None = None
    ends_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    home_team: TeamEvidence | None = None
    away_team: TeamEvidence | None = None
    status_label: str | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(self.provider_id, field="event provider_id")
        canonical_id = _optional_identifier(self.canonical_id, field="event canonical_id")
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("event provider_id must be a string or None")
        if canonical_id is not None and not isinstance(canonical_id, str):
            raise ValueError("event canonical_id must be a string or None")
        label = self.label.strip() if isinstance(self.label, str) else None
        status_label = (
            self.status_label.strip()
            if isinstance(self.status_label, str)
            else None
        )
        for team in (self.home_team, self.away_team):
            if team is not None and not isinstance(team, TeamEvidence):
                raise ValueError("event teams must be TeamEvidence or None")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "label", label or None)
        object.__setattr__(self, "status_label", status_label or None)
        object.__setattr__(self, "starts_at", normalize_timestamp(self.starts_at))
        object.__setattr__(self, "ends_at", normalize_timestamp(self.ends_at))
        object.__setattr__(self, "updated_at", normalize_timestamp(self.updated_at))


@dataclass(frozen=True, slots=True)
class StatisticEvidence:
    """Provider and canonical evidence for one requested statistic."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None
    components: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(
            self.provider_id, field="statistic provider_id"
        )
        canonical_id = _optional_identifier(
            self.canonical_id, field="statistic canonical_id"
        )
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("statistic provider_id must be a string or None")
        if canonical_id is not None and not isinstance(canonical_id, str):
            raise ValueError("statistic canonical_id must be a string or None")
        label = self.label.strip() if isinstance(self.label, str) else None
        components = tuple(
            component.strip()
            for component in self.components
            if isinstance(component, str) and component.strip()
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "label", label or None)
        object.__setattr__(self, "components", components)


@dataclass(frozen=True, slots=True)
class LeagueEvidence:
    """Provider league evidence retained alongside a semantic NBA query."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(self.provider_id, field="league provider_id")
        canonical_id = _optional_identifier(self.canonical_id, field="league canonical_id")
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("league provider_id must be a string or None")
        if canonical_id is not None and not isinstance(canonical_id, str):
            raise ValueError("league canonical_id must be a string or None")
        label = self.label.strip() if isinstance(self.label, str) else None
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "label", label or None)


@dataclass(frozen=True, slots=True)
class SportEvidence:
    """Provider sport evidence retained without mapping labels by guesswork."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(self.provider_id, field="sport provider_id")
        canonical_id = _optional_identifier(self.canonical_id, field="sport canonical_id")
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("sport provider_id must be a string or None")
        if canonical_id is not None and not isinstance(canonical_id, str):
            raise ValueError("sport canonical_id must be a string or None")
        label = self.label.strip() if isinstance(self.label, str) else None
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "label", label or None)


@dataclass(frozen=True, slots=True)
class CompetitionEvidence:
    """Provider competition/fixture-group evidence retained for auditability."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None
    sport: SportEvidence | None = None

    def __post_init__(self) -> None:
        provider_id = _optional_identifier(
            self.provider_id, field="competition provider_id"
        )
        canonical_id = _optional_identifier(
            self.canonical_id, field="competition canonical_id"
        )
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("competition provider_id must be a string or None")
        if canonical_id is not None and not isinstance(canonical_id, str):
            raise ValueError("competition canonical_id must be a string or None")
        label = self.label.strip() if isinstance(self.label, str) else None
        if self.sport is not None and not isinstance(self.sport, SportEvidence):
            raise ValueError("competition sport must be SportEvidence or None")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "label", label or None)


@dataclass(frozen=True, slots=True)
class Selection:
    """One selectable side of a provider projection market."""

    selection_id: str | None = None
    label: str | None = None
    direction: SelectionDirection | str | None = None
    direction_label: str | None = None
    status: str | None = None
    modifiers: tuple[SelectionModifier, ...] = ()
    american_price: int | str | None = None
    decimal_price: Decimal | int | str | float | None = None

    def __post_init__(self) -> None:
        selection_id = _optional_identifier(
            self.selection_id, field="selection provider_id"
        )
        if selection_id is not None and not isinstance(selection_id, str):
            raise ValueError("selection selection_id must be a string or None")
        label = self.label.strip() if isinstance(self.label, str) else None
        raw_direction = self.direction_label
        if raw_direction is None:
            raw_direction = self.direction
        normalized = normalize_selection_direction(raw_direction)
        direction_label = (
            self.direction_label
            if self.direction_label is not None
            else normalized.original_label
        )
        if direction_label is not None and not isinstance(direction_label, str):
            raise ValueError("selection direction_label must be a string or None")
        status = self.status.strip() if isinstance(self.status, str) else None
        modifiers = tuple(self.modifiers)
        if any(not isinstance(modifier, SelectionModifier) for modifier in modifiers):
            raise ValueError("selection modifiers must be SelectionModifier values")
        american_price = self.american_price
        if american_price is not None:
            if isinstance(american_price, bool):
                raise ValueError("selection american_price must be an integer or None")
            try:
                american_price = int(american_price)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "selection american_price must be an integer or None"
                ) from error
        decimal_price = (
            None
            if self.decimal_price is None
            else _decimal_value(self.decimal_price, field="selection decimal_price")
        )
        object.__setattr__(self, "selection_id", selection_id)
        object.__setattr__(self, "label", label or None)
        object.__setattr__(self, "direction", normalized.value)
        object.__setattr__(self, "direction_label", direction_label)
        object.__setattr__(self, "status", status or None)
        object.__setattr__(self, "modifiers", modifiers)
        object.__setattr__(self, "american_price", american_price)
        object.__setattr__(self, "decimal_price", decimal_price)

    @property
    def provider_id(self) -> str | None:
        """Alias clarifying that ``selection_id`` is upstream evidence."""

        return self.selection_id

    @property
    def status_label(self) -> str | None:
        """The original provider selection status label."""

        return self.status


@dataclass(frozen=True, slots=True)
class PlayerProjectionMarket:
    """One normalized NBA player projection market."""

    provider: str
    market_id: str | None = None
    athlete: AthleteEvidence | None = None
    event: EventEvidence | None = None
    team: TeamEvidence | None = None
    opponent: TeamEvidence | None = None
    league: LeagueEvidence | None = None
    competition: CompetitionEvidence | None = None
    sport: SportEvidence | None = None
    statistic: StatisticEvidence | None = None
    threshold: MarketThreshold | None = None
    status: MarketStatus | str = MarketStatus.AVAILABLE
    status_label: str | None = None
    variant: MarketVariant | str = MarketVariant.STANDARD
    variant_label: str | None = None
    scoring_period: ScoringPeriod | str | None = ScoringPeriod.FULL_GAME
    scoring_period_label: str | None = None
    starts_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    selections: tuple[Selection, ...] = ()
    appearance: AppearanceEvidence | None = None

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold() if isinstance(self.provider, str) else ""
        if not provider:
            raise ValueError("market provider must be a non-empty string")
        market_id = _optional_identifier(self.market_id, field="market provider_id")
        if market_id is not None and not isinstance(market_id, str):
            raise ValueError("market market_id must be a string or None")
        if self.athlete is not None and not isinstance(self.athlete, AthleteEvidence):
            raise ValueError("market athlete must be AthleteEvidence or None")
        if self.appearance is not None and not isinstance(
            self.appearance, AppearanceEvidence
        ):
            raise ValueError("market appearance must be AppearanceEvidence or None")
        if self.event is not None and not isinstance(self.event, EventEvidence):
            raise ValueError("market event must be EventEvidence or None")
        for evidence, name in (
            (self.team, "team"),
            (self.opponent, "opponent"),
        ):
            if evidence is not None and not isinstance(evidence, TeamEvidence):
                raise ValueError(f"market {name} must be TeamEvidence or None")
        for evidence, name in (
            (self.league, "league"),
            (self.competition, "competition"),
            (self.sport, "sport"),
        ):
            if evidence is not None and not isinstance(
                evidence, (LeagueEvidence, CompetitionEvidence, SportEvidence)
            ):
                raise ValueError(f"market {name} evidence has an invalid type")
        if self.statistic is not None and not isinstance(
            self.statistic, StatisticEvidence
        ):
            raise ValueError("market statistic must be StatisticEvidence or None")
        if self.threshold is not None and not isinstance(self.threshold, MarketThreshold):
            raise ValueError("market threshold must be MarketThreshold or None")
        status = normalize_market_status(self.status)
        status_label = self.status_label if self.status_label is not None else status.original_label
        variant = normalize_market_variant(self.variant)
        variant_label = self.variant_label if self.variant_label is not None else variant.original_label
        period_input = self.scoring_period
        period = normalize_scoring_period(period_input)
        period_label = (
            self.scoring_period_label
            if self.scoring_period_label is not None
            else period.original_label
        )
        selections = tuple(self.selections)
        if any(not isinstance(selection, Selection) for selection in selections):
            raise ValueError("market selections must be Selection values")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "market_id", market_id)
        object.__setattr__(self, "status", status.value)
        object.__setattr__(self, "status_label", status_label)
        object.__setattr__(self, "variant", variant.value)
        object.__setattr__(self, "variant_label", variant_label)
        object.__setattr__(self, "scoring_period", period.value)
        object.__setattr__(self, "scoring_period_label", period_label)
        object.__setattr__(self, "starts_at", normalize_timestamp(self.starts_at))
        object.__setattr__(self, "updated_at", normalize_timestamp(self.updated_at))
        object.__setattr__(self, "selections", selections)

    @property
    def source_identity(self) -> tuple[str, str] | None:
        """Exact upstream identity used for snapshot deduplication."""

        if self.market_id is None:
            return None
        return self.provider, self.market_id

    @property
    def provider_market_id(self) -> str | None:
        """Alias clarifying that ``market_id`` is upstream evidence."""

        return self.market_id

    @property
    def period(self) -> ScoringPeriod:
        """Return the normalized scoring period enum."""

        return ScoringPeriod(self.scoring_period)


class SnapshotStatus(str, Enum):
    """Whether a provider retrieval completed all expected upstream work."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class MalformedProviderResponseError(ValueError):
    """A provider payload cannot be represented without losing facts."""


@dataclass(frozen=True, slots=True)
class CoverageEvidence:
    """Counts and completion evidence for one provider retrieval."""

    fetched_count: int = 0
    eligible_count: int = 0
    normalized_count: int = 0
    skipped_count: int = 0
    pagination_complete: bool | None = None
    fanout_complete: bool | None = None
    expected_total: int | None = None
    warning_codes: tuple[str, ...] = ()
    skipped_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "fetched_count",
            "eligible_count",
            "normalized_count",
            "skipped_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"coverage {name} must be a non-negative integer")
        if self.expected_total is not None and (
            isinstance(self.expected_total, bool)
            or not isinstance(self.expected_total, int)
            or self.expected_total < 0
        ):
            raise ValueError("coverage expected_total must be a non-negative integer")
        for name in ("pagination_complete", "fanout_complete"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"coverage {name} must be true, false, or None")
        for name in ("warning_codes", "skipped_reasons"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"coverage {name} must contain non-empty strings")
            object.__setattr__(self, name, values)

    @property
    def is_complete(self) -> bool:
        """Whether the evidence says no expected upstream work is missing."""

        if self.pagination_complete is False or self.fanout_complete is False:
            return False
        return self.expected_total is None or self.fetched_count >= self.expected_total

    @property
    def fetched(self) -> int:
        """Short alias for callers that use count nouns."""

        return self.fetched_count

    @property
    def eligible(self) -> int:
        return self.eligible_count

    @property
    def normalized(self) -> int:
        return self.normalized_count

    @property
    def skipped(self) -> int:
        return self.skipped_count


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    """Immutable, temporally coherent normalized provider observation."""

    provider: str
    status: SnapshotStatus | str
    markets: tuple[PlayerProjectionMarket, ...]
    coverage: CoverageEvidence
    retrieved_at: datetime | str
    contract_version: str = "1"

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold() if isinstance(self.provider, str) else ""
        if not provider:
            raise ValueError("snapshot provider must be a non-empty string")
        try:
            status = self.status if isinstance(self.status, SnapshotStatus) else SnapshotStatus(self.status)
        except ValueError as error:
            raise ValueError("snapshot status must be complete or partial") from error
        if not isinstance(self.coverage, CoverageEvidence):
            raise ValueError("snapshot coverage must be CoverageEvidence")
        markets = tuple(self.markets)
        if any(not isinstance(market, PlayerProjectionMarket) for market in markets):
            raise ValueError("snapshot markets must be PlayerProjectionMarket values")
        if any(market.provider != provider for market in markets):
            raise ValueError("snapshot market providers must match the snapshot provider")
        if status is SnapshotStatus.PARTIAL:
            if not markets:
                raise ValueError("partial snapshots require at least one market")
            if self.coverage.is_complete:
                raise ValueError("partial snapshots require incomplete coverage evidence")
        if not isinstance(self.contract_version, str) or not self.contract_version.strip():
            raise ValueError("snapshot contract_version must be a non-empty string")

        by_identity: dict[tuple[str, str], PlayerProjectionMarket] = {}
        deduplicated: list[PlayerProjectionMarket] = []
        for market in markets:
            identity = market.source_identity
            if identity is None:
                deduplicated.append(market)
                continue
            previous = by_identity.get(identity)
            if previous is None:
                by_identity[identity] = market
                deduplicated.append(market)
            elif previous != market:
                raise MalformedProviderResponseError(
                    "repeated provider market identity has conflicting normalized content"
                )

        retrieved_at = normalize_timestamp(self.retrieved_at)
        if retrieved_at is None:
            raise ValueError("snapshot retrieved_at is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "markets", tuple(deduplicated))
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "contract_version", self.contract_version.strip())

    @property
    def fetched_at(self) -> datetime:
        """Alias for clients that call the retrieval timestamp fetched_at."""

        return self.retrieved_at

    @property
    def is_partial(self) -> bool:
        return self.status is SnapshotStatus.PARTIAL


class DeadlineExceededError(TimeoutError):
    """The shared retrieval deadline has elapsed."""


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Internal retrieval context shared by all provider adapters."""

    deadline: datetime | str
    request_id: str | None = None
    telemetry: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        deadline = normalize_timestamp(self.deadline)
        if deadline is None:
            raise ValueError("retrieval deadline is required")
        request_id = self.request_id
        if request_id is not None:
            if not isinstance(request_id, str) or not request_id.strip():
                raise ValueError("retrieval request_id must be a non-empty string or None")
            request_id = request_id.strip()
        if not isinstance(self.telemetry, Mapping):
            raise ValueError("retrieval telemetry must be a mapping")
        telemetry = {
            str(key): str(value)
            for key, value in self.telemetry.items()
            if str(key).strip()
        }
        object.__setattr__(self, "deadline", deadline)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "telemetry", MappingProxyType(telemetry))

    @property
    def deadline_at(self) -> datetime:
        """Alias used by adapters that name the absolute timestamp explicitly."""

        return self.deadline

    def remaining_seconds(self, *, now: datetime | str | None = None) -> float:
        """Return non-negative seconds left in the absolute retrieval budget."""

        current = normalize_timestamp(now) if now is not None else datetime.now(timezone.utc)
        if current is None:  # pragma: no cover - guarded by normalize_timestamp
            return 0.0
        return max(0.0, (self.deadline - current).total_seconds())

    def is_expired(self, *, now: datetime | str | None = None) -> bool:
        """Return whether no provider work may begin under this context."""

        current = normalize_timestamp(now) if now is not None else datetime.now(timezone.utc)
        return current >= self.deadline

    def ensure_active(self, *, now: datetime | str | None = None) -> None:
        """Raise a typed timeout when adapter work would exceed the deadline."""

        if self.is_expired(now=now):
            raise DeadlineExceededError("provider retrieval deadline exceeded")


@dataclass(frozen=True, slots=True)
class NBAMarketQuery:
    """Small semantic query accepted by every NBA DFS provider adapter."""

    sport: str = "NBA"
    league: str = "NBA"
    athlete_id: int | None = None
    event_id: str | None = None
    statistic_id: str | None = None
    statistic: str | None = None
    scoring_period: ScoringPeriod | str | None = ScoringPeriod.FULL_GAME
    market_statuses: tuple[MarketStatus | str, ...] = (
        MarketStatus.AVAILABLE,
        MarketStatus.SUSPENDED,
    )
    pregame_only: bool = True

    def __post_init__(self) -> None:
        sport = self.sport.strip().upper() if isinstance(self.sport, str) else ""
        league = self.league.strip().upper() if isinstance(self.league, str) else ""
        if sport != "NBA" or league != "NBA":
            raise ValueError("the shared provider query supports NBA only")
        if self.athlete_id is not None and (
            isinstance(self.athlete_id, bool)
            or not isinstance(self.athlete_id, int)
            or self.athlete_id < 1
        ):
            raise ValueError("query athlete_id must be a positive integer or None")
        event_id = _optional_identifier(self.event_id, field="query event_id")
        statistic_id = _optional_identifier(
            self.statistic_id, field="query statistic_id"
        )
        statistic = self.statistic.strip() if isinstance(self.statistic, str) else None
        period = normalize_scoring_period(self.scoring_period)
        statuses = tuple(normalize_market_status(status).value for status in self.market_statuses)
        if not statuses:
            raise ValueError("query market_statuses must not be empty")
        if not isinstance(self.pregame_only, bool):
            raise ValueError("query pregame_only must be boolean")
        object.__setattr__(self, "sport", sport)
        object.__setattr__(self, "league", league)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "statistic_id", statistic_id)
        object.__setattr__(self, "statistic", statistic or None)
        object.__setattr__(self, "scoring_period", period.value)
        object.__setattr__(self, "market_statuses", statuses)

    @property
    def canonical_athlete_id(self) -> int | None:
        return self.athlete_id

    @property
    def canonical_event_id(self) -> str | None:
        return self.event_id

    @property
    def canonical_statistic_id(self) -> str | None:
        return self.statistic_id


@runtime_checkable
class ProviderSnapshotProvider(Protocol):
    """Single public adapter seam for normalized provider snapshots."""

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Retrieve one complete or partial snapshot for the semantic query."""


# Names used by different orchestration layers should point to one contract,
# not introduce compatibility protocols with subtly different methods.
MarketQuery = NBAMarketQuery
SemanticMarketQuery = NBAMarketQuery
DFSProvider = ProviderSnapshotProvider
ProviderAdapter = ProviderSnapshotProvider


__all__ = [
    "AthleteEvidence",
    "AppearanceEvidence",
    "CompetitionEvidence",
    "EventEvidence",
    "LeagueEvidence",
    "MarketStatus",
    "MarketVariant",
    "MarketThreshold",
    "CoverageEvidence",
    "DeadlineExceededError",
    "DFSProvider",
    "MalformedProviderResponseError",
    "MarketQuery",
    "NormalizedLabel",
    "PlayerProjectionMarket",
    "NBAMarketQuery",
    "ProviderAdapter",
    "ProviderSnapshot",
    "ProviderSnapshotProvider",
    "RetrievalContext",
    "Selection",
    "SelectionModifier",
    "SelectionDirection",
    "SemanticMarketQuery",
    "ScoringPeriod",
    "SnapshotStatus",
    "SportEvidence",
    "StatisticEvidence",
    "TeamEvidence",
    "normalize_market_variant",
    "normalize_market_status",
    "normalize_selection_direction",
    "normalize_scoring_period",
    "normalize_timestamp",
]
