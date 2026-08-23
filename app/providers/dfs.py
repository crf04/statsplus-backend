"""Shared, provider-neutral contract for NBA daily-fantasy markets.

Provider adapters own discovery and wire-format details.  This module owns
the small semantic vocabulary exchanged with the rest of the application and
the immutable snapshot models returned by adapters.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from app.domain.market_content import (
    NumericDomainError,
    market_content_key,
    market_evidence_key,
    normalized_decimal,
)
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch

from app.utils.request_id import is_valid_request_id


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


class PriceKind(str, Enum):
    """The one canonical form a selection's published price is stated in."""

    AMERICAN = "american"
    DECIMAL = "decimal"
    MULTIPLIER = "multiplier"
    UNPRICED = "unpriced"


class PriceScope(str, Enum):
    """What a selection's published price is a price for.

    ``SELECTION`` is odds for this leg alone; ``ENTRY`` is the slip-level
    payout a leg takes part in, which depends on the entry's leg count.
    """

    SELECTION = "selection"
    ENTRY = "entry"


class ModifierKind(str, Enum):
    """The closed vocabulary of provider-defined selection adjustments."""

    PAYOUT_MULTIPLIER = "payout_multiplier"
    LINE_ADJUSTMENT = "line_adjustment"
    PROMO = "promo"


_MODIFIER_KIND_LABELS = {
    "payout_multiplier": ModifierKind.PAYOUT_MULTIPLIER,
    "payout multiplier": ModifierKind.PAYOUT_MULTIPLIER,
    "multiplier": ModifierKind.PAYOUT_MULTIPLIER,
    "payout": ModifierKind.PAYOUT_MULTIPLIER,
    "line_adjustment": ModifierKind.LINE_ADJUSTMENT,
    "line adjustment": ModifierKind.LINE_ADJUSTMENT,
    "line": ModifierKind.LINE_ADJUSTMENT,
    "promo": ModifierKind.PROMO,
    "promotion": ModifierKind.PROMO,
    "promotional": ModifierKind.PROMO,
}


def normalize_modifier_kind(label: str | ModifierKind | None) -> ModifierKind:
    """Normalize a provider modifier label into the closed kind vocabulary.

    A modifier the application cannot name is a malformed source record, not a
    pass-through string: an unrecognized adjustment silently retained would be
    read by no consumer and would still change what a selection offers.
    """

    if isinstance(label, ModifierKind):
        return label
    normalized = label.strip().casefold() if isinstance(label, str) else ""
    kind = _MODIFIER_KIND_LABELS.get(normalized)
    if kind is None:
        raise CoverageRecordMalformed(
            "selection modifier kind is not a reviewed payout_multiplier, "
            "line_adjustment, or promo value"
        )
    return kind


class MarketStatus(str, Enum):
    """The eligible market availability states in a provider snapshot."""

    AVAILABLE = "available"
    SUSPENDED = "suspended"


EnumValue = TypeVar("EnumValue", bound=Enum)


@dataclass(frozen=True, slots=True)
class NormalizedLabel(Generic[EnumValue]):
    """A closed normalized value together with its provider label."""

    value: EnumValue
    original_label: str | None


def normalize_market_variant(label: str | MarketVariant | None) -> NormalizedLabel[MarketVariant]:
    """Normalize a provider variant label without guessing unknown values.

    The map below is the closed variant vocabulary.  PrizePicks names its two
    adjusted-line products ``demon`` and ``goblin``; both are alternate lines
    on the same offering, so they normalize to :attr:`MarketVariant.ALTERNATE`
    with the provider's own word retained as the variant label.  Underdog calls
    its two-sided line ``balanced``, which is that provider's word for its
    standard offering.

    A label outside this map stays :attr:`MarketVariant.UNKNOWN`, and the rule
    for an UNKNOWN variant is a single one, stated in one place: it is retained
    and archived with its label, and it is never targetable -- neither the
    database-first Player Pool nor a Latest Player Projection row admits a
    market whose variant the application cannot name.  Naming a new provider
    word is therefore a change to this map, reviewed once, rather than a
    silent drop at a downstream filter.
    """

    if isinstance(label, MarketVariant):
        original_label = None if label is MarketVariant.UNKNOWN else label.value
        return NormalizedLabel(label, original_label)

    normalized = label.strip().casefold() if isinstance(label, str) else ""
    values = {
        "standard": MarketVariant.STANDARD,
        "default": MarketVariant.STANDARD,
        "main": MarketVariant.STANDARD,
        "balanced": MarketVariant.STANDARD,
        "alternate": MarketVariant.ALTERNATE,
        "alt": MarketVariant.ALTERNATE,
        "demon": MarketVariant.ALTERNATE,
        "goblin": MarketVariant.ALTERNATE,
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
        # ``UNKNOWN`` is this application's word for "the provider named no
        # direction", so it is never a provider label to retain.
        original_label = None if label is SelectionDirection.UNKNOWN else label.value
        return NormalizedLabel(label, original_label)

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
        "pre_game": MarketStatus.AVAILABLE,
        "pre-game": MarketStatus.AVAILABLE,
        "pregame": MarketStatus.AVAILABLE,
        "suspended": MarketStatus.SUSPENDED,
        "paused": MarketStatus.SUSPENDED,
    }
    status = values.get(normalized)
    if status is None:
        raise ValueError(
            "market status is not an eligible available or suspended value"
        )
    return NormalizedLabel(status, label)


_SCORING_PERIOD_VALUES = {period.value: period for period in ScoringPeriod}


def normalize_scoring_period(
    label: str | ScoringPeriod | None,
) -> NormalizedLabel[ScoringPeriod]:
    """Normalize reviewed scoring periods without guessing unknown labels.

    Canonical closed-vocabulary values such as ``full_game`` resolve exactly;
    everything else falls back to the reviewed label map and stays UNKNOWN.
    """

    if isinstance(label, ScoringPeriod):
        original_label = None if label is ScoringPeriod.UNKNOWN else label.value
        return NormalizedLabel(label, original_label)
    cleaned = label.strip().casefold() if isinstance(label, str) else ""
    canonical = _SCORING_PERIOD_VALUES.get(cleaned)
    if canonical is not None:
        return NormalizedLabel(canonical, label)
    normalized = cleaned.replace("-", " ")
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
    """Convert an upstream numeric value without introducing float arithmetic.

    This is the one boundary at which a provider number enters the
    application, so it is where the normalized numeric domain is enforced: a
    value outside it is rejected here, as one malformed provider field, rather
    than accepted and then found uncomparable when a board states its spread.
    """

    if isinstance(value, bool):
        raise NumericDomainError(f"{field} must be a finite decimal")
    if isinstance(value, float) and not isfinite(value):
        raise NumericDomainError(f"{field} must be a finite decimal")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise NumericDomainError(f"{field} must be a finite decimal") from error
    return normalized_decimal(decimal, field=field)


def _integral_value(value: Decimal | int | str | float, *, field: str) -> int:
    """One exactly integral provider number, or one typed malformed field.

    An American price is a whole number of odds units, so the accepted forms
    are the ones that already name such a number exactly: an integer, or a
    numeric value or numeric string whose value is integral.  A fractional
    value is refused rather than truncated into a price the provider never
    quoted, and a boolean, a nonfinite value, or a magnitude outside the
    normalized numeric domain is refused too.

    Every refusal is a :class:`NumericDomainError`, so it reaches each adapter
    as the ``ValueError`` it already converts into one typed malformed record.
    ``int()`` alone does not: it truncates ``-112.5`` silently and raises
    ``OverflowError`` on an infinity, which no adapter's coverage translation
    catches.
    """

    decimal = _decimal_value(value, field=field)
    _sign, digits, exponent = decimal.as_tuple()
    if exponent < 0 and any(digits[exponent:]):
        raise NumericDomainError(f"{field} must be a whole number")
    return int(decimal)


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

@dataclass(frozen=True, slots=True)
class SelectionModifier:
    """Provider-defined selection adjustment, never an entry payout."""

    value: Decimal | int | str | float
    kind: str
    scope: str
    label: str | None = None

    def __post_init__(self) -> None:
        decimal = _decimal_value(self.value, field="selection modifier value")
        kind = normalize_modifier_kind(self.kind).value
        scope = self.scope.strip() if isinstance(self.scope, str) else ""
        if not scope:
            raise ValueError("selection modifier scope must be a non-empty string")
        if self.label is None:
            label = None
        elif not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("selection modifier label must be a non-empty string or None")
        else:
            label = self.label
        object.__setattr__(self, "value", decimal)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "label", label)

def _price_enum(
    value: object,
    enum: type[EnumValue],
    *,
    field: str,
) -> EnumValue:
    if isinstance(value, enum):
        return value
    try:
        return enum(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"selection {field} is not a reviewed value") from error


def _canonical_price(
    *,
    price_kind: object,
    price_value: object,
    price_scope: object,
    american_price: int | None,
    decimal_price: Decimal | None,
    modifiers: tuple["SelectionModifier", ...],
) -> tuple["PriceKind", Decimal | None, "PriceScope"]:
    """State one selection's price in the single canonical form, exactly once.

    A stated kind is honoured and checked against the fields it claims; an
    unstated one is derived from the retained fields in a fixed order so that
    every provider -- and every provider yet to be written -- answers the same
    question the same way.
    """

    payouts = tuple(
        modifier
        for modifier in modifiers
        if modifier.kind == ModifierKind.PAYOUT_MULTIPLIER.value
    )
    if price_kind is None:
        if price_value is not None or price_scope is not None:
            raise ValueError(
                "selection price_value and price_scope require a price_kind"
            )
        if american_price is not None:
            return PriceKind.AMERICAN, Decimal(american_price), PriceScope.SELECTION
        if decimal_price is not None:
            return PriceKind.DECIMAL, decimal_price, PriceScope.SELECTION
        if len(payouts) > 1:
            raise CoverageRecordMalformed(
                "selection states more than one payout multiplier"
            )
        if payouts:
            return PriceKind.MULTIPLIER, payouts[0].value, PriceScope.ENTRY
        return PriceKind.UNPRICED, None, PriceScope.SELECTION

    kind = _price_enum(price_kind, PriceKind, field="price_kind")
    if price_scope is None:
        scope = (
            PriceScope.ENTRY
            if kind is PriceKind.MULTIPLIER
            else PriceScope.SELECTION
        )
    else:
        scope = _price_enum(price_scope, PriceScope, field="price_scope")
    if kind is PriceKind.UNPRICED:
        if price_value is not None:
            raise ValueError("an unpriced selection must not state a price_value")
        return kind, None, scope
    if price_value is None:
        raise ValueError("a priced selection must state a price_value")
    if kind is PriceKind.AMERICAN:
        value = Decimal(_integral_value(price_value, field="selection price_value"))
        if american_price is not None and value != Decimal(american_price):
            raise ValueError("selection price_value contradicts american_price")
        return kind, value, scope
    value = _decimal_value(price_value, field="selection price_value")
    if kind is PriceKind.DECIMAL:
        if decimal_price is not None and value != decimal_price:
            raise ValueError("selection price_value contradicts decimal_price")
        return kind, value, scope
    if value <= 0:
        raise ValueError("a multiplier price_value must be positive")
    return kind, value, scope


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
    """One selectable side of a provider projection market.

    Every selection states its price in exactly one canonical form, so two
    providers that publish prices in different shapes can still be compared as
    prices rather than as adapter-specific fields.  The triple is
    ``price_kind``/``price_value``/``price_scope``: what form the number is in,
    the exact number as published, and whether it prices this leg alone or the
    slip the leg takes part in.

    An adapter states the triple only when the provider prices a selection
    outside its own payload -- an entry payout declared in the provider
    registry, for instance.  Otherwise it is derived here, once, from the
    fields the adapter already normalized, in this order: an American price,
    then a decimal price, then a single payout-multiplier modifier, and
    otherwise ``unpriced``.  Deriving it at this seam is what makes the rule
    the same rule for every provider, including ones not yet written.

    ``american_price`` and ``decimal_price`` stay exactly as they were; the
    canonical triple is additive and never contradicts them.
    """

    selection_id: str | None = None
    label: str | None = None
    direction: SelectionDirection | str | None = None
    direction_label: str | None = None
    status: str | None = None
    modifiers: tuple[SelectionModifier, ...] = ()
    american_price: Decimal | int | str | float | None = None
    decimal_price: Decimal | int | str | float | None = None
    price_kind: PriceKind | str | None = None
    price_value: Decimal | int | str | float | None = None
    price_scope: PriceScope | str | None = None

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
        american_price = (
            None
            if self.american_price is None
            else _integral_value(self.american_price, field="selection american_price")
        )
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
        kind, value, scope = _canonical_price(
            price_kind=self.price_kind,
            price_value=self.price_value,
            price_scope=self.price_scope,
            american_price=american_price,
            decimal_price=decimal_price,
            modifiers=modifiers,
        )
        object.__setattr__(self, "price_kind", kind)
        object.__setattr__(self, "price_value", value)
        object.__setattr__(self, "price_scope", scope)

    @property
    def is_priced(self) -> bool:
        """Whether this selection states a price at all."""

        return self.price_kind is not PriceKind.UNPRICED

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
    scoring_period: ScoringPeriod | str | None = ScoringPeriod.UNKNOWN
    scoring_period_label: str | None = None
    starts_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    selections: tuple[Selection, ...] = ()
    appearance: AppearanceEvidence | None = None
    statistic_match: StatisticMatch | None = None

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
        for evidence, name, expected_type in (
            (self.league, "league", LeagueEvidence),
            (self.competition, "competition", CompetitionEvidence),
            (self.sport, "sport", SportEvidence),
        ):
            if evidence is not None and not isinstance(evidence, expected_type):
                raise ValueError(
                    f"market {name} must be {expected_type.__name__} or None"
                )
        if self.statistic is not None and not isinstance(
            self.statistic, StatisticEvidence
        ):
            raise ValueError("market statistic must be StatisticEvidence or None")
        if self.statistic_match is not None and not isinstance(
            self.statistic_match, StatisticMatch
        ):
            raise ValueError("market statistic_match must be StatisticMatch or None")
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
        # One market states one price form.  Two sides of one offering priced
        # in different forms could not be compared with each other, let alone
        # with another provider's, so a provider that publishes them that way
        # is publishing a market this application cannot represent.
        priced = tuple(selection for selection in selections if selection.is_priced)
        if len({(selection.price_kind, selection.price_scope) for selection in priced}) > 1:
            raise MalformedProviderResponseError(
                "market selections must state one price kind and scope"
            )
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
    def price_kind(self) -> PriceKind:
        """The one canonical form this market's selections are priced in."""

        for selection in self.selections:
            if selection.is_priced:
                return selection.price_kind
        return PriceKind.UNPRICED

    @property
    def price_scope(self) -> PriceScope:
        """Whether this market's price is for one leg or for the entry."""

        for selection in self.selections:
            if selection.is_priced:
                return selection.price_scope
        return PriceScope.SELECTION

    @property
    def price_value(self) -> Decimal | None:
        """The market's price when every priced side states the same number.

        An entry payout is one number for the whole slip, so it is a fact about
        the market.  Two-sided odds are not: each side states its own number,
        and those stay on the selections in the archived source document rather
        than being averaged or arbitrarily chosen here.
        """

        values = {
            selection.price_value
            for selection in self.selections
            if selection.is_priced
        }
        if len(values) != 1:
            return None
        return values.pop()

    @property
    def is_priced(self) -> bool:
        """Whether this market states a price for any side it offers."""

        return any(selection.is_priced for selection in self.selections)

    @property
    def statistic_match_state(self) -> MatchState | None:
        """Return the board resolver state while retaining source evidence."""

        if self.statistic_match is not None:
            return self.statistic_match.state
        return None

    @property
    def source_identity(self) -> tuple[str, str] | None:
        """Exact upstream identity used for snapshot deduplication."""

        if self.market_id is None:
            return None
        return self.provider, self.market_id

class SnapshotStatus(str, Enum):
    """Whether a provider retrieval completed all expected upstream work."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class CoverageCode(str, Enum):
    """Closed warning and exclusion vocabulary for provider coverage."""

    CONFLICTING_SOURCE_IDENTITY = "conflicting_source_identity"
    DUPLICATE_SOURCE_IDENTITY = "duplicate_source_identity"
    FIXTURE_FAILED = "fixture_failed"
    FIXTURE_LIST_FAILED = "fixture_list_failed"
    FIXTURE_MALFORMED = "fixture_malformed"
    INELIGIBLE_EVENT_STATUS = "ineligible_event_status"
    INELIGIBLE_STATUS = "ineligible_status"
    MALFORMED_RECORD = "malformed_record"
    MISSING_COMPETITION_ID = "missing_competition_id"
    MISSING_EVENT_RELATIONSHIP = "missing_event_relationship"
    MISSING_MATCH_RELATIONSHIP = "missing_match_relationship"
    MISSING_MATCH_TYPE = "missing_match_type"
    NON_GAME_MARKET = "non_game_market"
    NON_NBA_COMPETITION = "non_nba_competition"
    NON_NBA_MARKET = "non_nba_market"
    NON_NBA_SPORT = "non_nba_sport"
    NON_PLAYER_MARKET = "non_player_market"
    NON_PROJECTION_MARKET = "non_projection_market"
    PAGE_FETCH_FAILED = "page_fetch_failed"
    PAGE_MALFORMED = "page_malformed"
    PAGE_METADATA_MISMATCH = "page_metadata_mismatch"
    PAGINATION_EXPECTED_TOTAL_CHANGED = "pagination_expected_total_changed"
    PAGINATION_METADATA_MALFORMED = "pagination_metadata_malformed"
    PAGINATION_PAGE_MISMATCH = "pagination_page_mismatch"
    PAGINATION_TOTAL_PAGES_CHANGED = "pagination_total_pages_changed"
    STATUS_FILTER = "status_filter"


def normalize_coverage_code(
    value: CoverageCode | str,
    *,
    default: CoverageCode | None = None,
) -> CoverageCode:
    """Normalize a reviewed coverage code, optionally falling back for detail."""

    if isinstance(value, CoverageCode):
        return value
    try:
        return CoverageCode(value)
    except (TypeError, ValueError) as error:
        if default is not None:
            return default
        raise ValueError("coverage code must be a known CoverageCode value") from error


def _normalize_coverage_codes(
    values: tuple[CoverageCode | str, ...],
    *,
    field: str,
) -> tuple[CoverageCode, ...]:
    normalized: list[CoverageCode] = []
    for value in tuple(values):
        try:
            code = normalize_coverage_code(value)
        except ValueError as error:
            raise ValueError(
                f"coverage {field} must contain known CoverageCode values"
            ) from error
        normalized.append(code)
    return tuple(normalized)


class MalformedProviderResponseError(ValueError):
    """A provider payload cannot be represented without losing facts."""


class CoverageRecordExcluded(Exception):
    """A valid source record is outside the shared adapter scope."""

    def __init__(self, code: CoverageCode) -> None:
        if not isinstance(code, CoverageCode):
            raise TypeError("coverage exclusion code must be a CoverageCode")
        self.code = code
        super().__init__(self.code.value)


class CoverageRecordMalformed(MalformedProviderResponseError):
    """A source record is malformed, with typed coverage and diagnostic detail."""

    def __init__(
        self,
        detail: str,
        *,
        code: CoverageCode = CoverageCode.MALFORMED_RECORD,
    ) -> None:
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError("malformed record detail must be a non-empty string")
        if not isinstance(code, CoverageCode):
            raise TypeError("malformed record code must be a CoverageCode")
        self.code = code
        self.detail = detail
        super().__init__(detail)


class _RecordCoverageAccumulator:
    """Account for normalized records consistently across provider adapters."""

    def __init__(self) -> None:
        self.markets: list[Any] = []
        self.fetched_count = 0
        self.eligible_count = 0
        self.normalized_count = 0
        self.skipped_count = 0
        self.malformed_count = 0
        self.warning_codes: list[CoverageCode] = []
        self.skipped_reasons: list[CoverageCode] = []
        self.diagnostic_details: list[str] = []

    def add(
        self,
        record: Any,
        normalize: Callable[[Any], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        """Normalize one record and retain typed coverage evidence."""

        self.fetched_count += 1
        try:
            normalized = normalize(record)
        except CoverageRecordExcluded as error:
            self._exclude(error)
            return
        except CoverageRecordMalformed as error:
            self._malformed(error)
            return

        try:
            if on_success is None:
                self.markets.append(normalized)
            else:
                on_success(normalized)
        except CoverageRecordMalformed as error:
            self._malformed(error)
            return

        # Identity-aware consumers (such as the shared market collector) may
        # reject a normalized value.  Commit source coverage only after that
        # transaction accepts the value; a rejected value is one malformed
        # source row, accounted for by _malformed above.
        self.eligible_count += 1
        self.normalized_count += 1

    def accept_normalized(
        self,
        normalized: Any,
        *,
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        """Commit one already-normalized row after identity acceptance.

        Parsers may need to normalize inside a bounded transport worker.  A
        caller can then feed the immutable normalized batch through the same
        identity transaction without counting the row twice as fetched.
        """

        try:
            if on_success is None:
                self.markets.append(normalized)
            else:
                on_success(normalized)
        except CoverageRecordMalformed as error:
            self._malformed(error)
            return
        self.eligible_count += 1
        self.normalized_count += 1

    def absorb(
        self,
        batch: "_NormalizedBatch",
        *,
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        """Merge a parser batch while retaining one identity/accounting owner."""

        self.fetched_count += batch.fetched_count
        self.skipped_count += batch.skipped_count
        self.malformed_count += batch.malformed_count
        for code in batch.warning_codes:
            if code not in self.warning_codes:
                self.warning_codes.append(code)
        for code in batch.skipped_reasons:
            if code not in self.skipped_reasons:
                self.skipped_reasons.append(code)
        for detail in batch.diagnostic_details:
            if detail not in self.diagnostic_details:
                self.diagnostic_details.append(detail)
        for normalized in batch.markets:
            self.accept_normalized(normalized, on_success=on_success)

    def extend(
        self,
        records: Iterable[Any],
        normalize: Callable[[Any], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        """Account for records in source order using the shared row policy."""

        for record in records:
            self.add(record, normalize, on_success=on_success)

    def _exclude(self, error: CoverageRecordExcluded) -> None:
        self.skipped_count += 1
        if error.code not in self.skipped_reasons:
            self.skipped_reasons.append(error.code)

    def _malformed(
        self,
        error: CoverageRecordMalformed,
    ) -> None:
        self.skipped_count += 1
        self.malformed_count += 1
        if CoverageCode.MALFORMED_RECORD not in self.warning_codes:
            self.warning_codes.append(CoverageCode.MALFORMED_RECORD)
        if error.code not in self.skipped_reasons:
            self.skipped_reasons.append(error.code)
        if error.detail not in self.diagnostic_details:
            self.diagnostic_details.append(error.detail)

    def warning_values(self) -> tuple[CoverageCode, ...]:
        return tuple(self.warning_codes)

    def skipped_values(self) -> tuple[CoverageCode, ...]:
        return tuple(self.skipped_reasons)

    def diagnostic_values(self) -> tuple[str, ...]:
        return tuple(self.diagnostic_details)


@dataclass(frozen=True, slots=True)
class _NormalizedBatch:
    """Immutable normalized records plus their shared coverage accounting."""

    markets: tuple[PlayerProjectionMarket, ...]
    fetched_count: int
    eligible_count: int
    normalized_count: int
    skipped_count: int
    warning_codes: tuple[CoverageCode, ...]
    skipped_reasons: tuple[CoverageCode, ...]
    diagnostic_details: tuple[str, ...]
    malformed_count: int

    def __post_init__(self) -> None:
        if self.fetched_count != self.eligible_count + self.skipped_count:
            raise ValueError(
                "normalized batch coverage must partition fetched rows"
            )
        if self.normalized_count != self.eligible_count:
            raise ValueError(
                "normalized batch normalized and eligible counts must agree"
            )
        if self.malformed_count > self.skipped_count:
            raise ValueError(
                "normalized batch malformed rows cannot exceed skipped rows"
            )
        if len(self.markets) > self.normalized_count:
            raise ValueError(
                "normalized batch retained markets cannot exceed normalized rows"
            )

    @classmethod
    def from_accumulator(
        cls,
        accumulator: _RecordCoverageAccumulator,
        *,
        markets: Iterable[PlayerProjectionMarket] | None = None,
        warning_codes: Iterable[CoverageCode] = (),
    ) -> "_NormalizedBatch":
        """Finalize one normalized-record accumulator consistently."""

        return cls(
            markets=tuple(accumulator.markets if markets is None else markets),
            fetched_count=accumulator.fetched_count,
            eligible_count=accumulator.eligible_count,
            normalized_count=accumulator.normalized_count,
            skipped_count=accumulator.skipped_count,
            warning_codes=tuple(
                dict.fromkeys((*accumulator.warning_values(), *warning_codes))
            ),
            skipped_reasons=accumulator.skipped_values(),
            diagnostic_details=accumulator.diagnostic_values(),
            malformed_count=accumulator.malformed_count,
        )


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
    warning_codes: tuple[CoverageCode | str, ...] = ()
    skipped_reasons: tuple[CoverageCode | str, ...] = ()
    diagnostic_details: tuple[str, ...] = ()

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
            values = _normalize_coverage_codes(
                getattr(self, name),
                field=name,
            )
            object.__setattr__(self, name, values)
        details = tuple(self.diagnostic_details)
        if any(not isinstance(value, str) or not value.strip() for value in details):
            raise ValueError("coverage diagnostic_details must contain non-empty strings")
        object.__setattr__(self, "diagnostic_details", details)

    @property
    def is_complete(self) -> bool:
        """Whether the evidence says no expected upstream work is missing."""

        if self.pagination_complete is False or self.fanout_complete is False:
            return False
        return self.expected_total is None or self.fetched_count >= self.expected_total

class _RetainedRepeat:
    """The market one source identity is currently retained by, and where.

    Both seams that collapse a repeated identity need the same three things:
    what the retained market says, how its complete retained evidence orders
    against a repeat, and which position in the retained sequence it holds.
    Keeping them together computes each key once and keeps the choice between
    two semantic repeats a function of their content alone.
    """

    __slots__ = ("market", "position", "content_key", "_evidence_key")

    def __init__(self, market: PlayerProjectionMarket, position: int) -> None:
        self.market = market
        self.position = position
        self.content_key = market_content_key(market)
        self._evidence_key = market_evidence_key(market)

    def supersede(self, market: PlayerProjectionMarket) -> bool:
        """Whether this semantic repeat is the one to retain, and retain it."""

        evidence_key = market_evidence_key(market)
        if evidence_key >= self._evidence_key:
            return False
        self.market = market
        self._evidence_key = evidence_key
        return True


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
        if status is SnapshotStatus.COMPLETE and not self.coverage.is_complete:
            raise ValueError("complete snapshots require complete coverage evidence")
        if status is SnapshotStatus.PARTIAL:
            if not markets:
                raise ValueError("partial snapshots require at least one market")
            if self.coverage.is_complete:
                raise ValueError("partial snapshots require incomplete coverage evidence")
        if not isinstance(self.contract_version, str) or not self.contract_version.strip():
            raise ValueError("snapshot contract_version must be a non-empty string")

        # A repeated source identity is read in the shared canonical semantics
        # (see app.domain.market_content), so a market merely relisted with its
        # selections the other way round, or with one exact number written at
        # another scale, is one market rather than a contradiction.  Where two
        # such repeats differ only in retained audit spelling, the one kept is
        # the least by complete evidence content, never the first to arrive.
        by_identity: dict[tuple[str, str], _RetainedRepeat] = {}
        deduplicated: list[PlayerProjectionMarket] = []
        for market in markets:
            identity = market.source_identity
            if identity is None:
                deduplicated.append(market)
                continue
            previous = by_identity.get(identity)
            if previous is None:
                by_identity[identity] = _RetainedRepeat(market, len(deduplicated))
                deduplicated.append(market)
                continue
            if previous.content_key != market_content_key(market):
                raise MalformedProviderResponseError(
                    "repeated provider market identity has conflicting normalized content"
                )
            if previous.supersede(market):
                deduplicated[previous.position] = market

        retrieved_at = normalize_timestamp(self.retrieved_at)
        if retrieved_at is None:
            raise ValueError("snapshot retrieved_at is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "markets", tuple(deduplicated))
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "contract_version", self.contract_version.strip())

class _SnapshotMarketCollector:
    """Collect normalized markets with exact source-identity semantics.

    A repeated identity is judged in the same shared canonical semantics
    :class:`ProviderSnapshot` validates in, so an adapter never rejects a
    market the snapshot it is building would have accepted as one offering.
    """

    def __init__(self) -> None:
        self._markets: list[PlayerProjectionMarket] = []
        self._seen: dict[tuple[str, str], _RetainedRepeat] = {}
        self._conflicting: set[tuple[str, str]] = set()
        self._warning_codes: list[CoverageCode] = []

    def add(self, market: PlayerProjectionMarket) -> None:
        """Add one normalized market, recording semantic repeats or conflicts."""

        identity = market.source_identity
        if identity is None:
            self._markets.append(market)
            return
        previous = self._seen.get(identity)
        if previous is None:
            self._seen[identity] = _RetainedRepeat(market, len(self._markets))
            self._markets.append(market)
            return
        if previous.content_key == market_content_key(market):
            self._warning_codes.append(CoverageCode.DUPLICATE_SOURCE_IDENTITY)
            if previous.supersede(market):
                self._markets[previous.position] = market
            return
        self._conflicting.add(identity)
        self._warning_codes.append(CoverageCode.CONFLICTING_SOURCE_IDENTITY)
        raise CoverageRecordMalformed(
            "repeated provider market identity has conflicting normalized content",
            code=CoverageCode.CONFLICTING_SOURCE_IDENTITY,
        )

    def extend(self, markets: Iterable[PlayerProjectionMarket]) -> None:
        """Add normalized markets in source order."""

        for market in markets:
            self.add(market)

    @property
    def markets(self) -> tuple[PlayerProjectionMarket, ...]:
        """Return usable markets, excluding every conflicting source identity."""

        if not self._conflicting:
            return tuple(self._markets)
        return tuple(
            market
            for market in self._markets
            if market.source_identity not in self._conflicting
        )

    @property
    def warning_codes(self) -> tuple[CoverageCode, ...]:
        return tuple(dict.fromkeys(self._warning_codes))


def _build_snapshot(
    *,
    provider: str,
    markets: Iterable[PlayerProjectionMarket],
    retrieved_at: datetime | str,
    fetched_count: int = 0,
    eligible_count: int = 0,
    normalized_count: int = 0,
    skipped_count: int = 0,
    pagination_complete: bool | None = None,
    fanout_complete: bool | None = None,
    expected_total: int | None = None,
    warning_codes: Iterable[CoverageCode | str] = (),
    skipped_reasons: Iterable[CoverageCode | str] = (),
    diagnostic_details: Iterable[str] = (),
) -> ProviderSnapshot:
    """Build the immutable snapshot and derive status from coverage evidence."""

    coverage = CoverageEvidence(
        fetched_count=fetched_count,
        eligible_count=eligible_count,
        normalized_count=normalized_count,
        skipped_count=skipped_count,
        pagination_complete=pagination_complete,
        fanout_complete=fanout_complete,
        expected_total=expected_total,
        warning_codes=tuple(dict.fromkeys(warning_codes)),
        skipped_reasons=tuple(dict.fromkeys(skipped_reasons)),
        diagnostic_details=tuple(dict.fromkeys(diagnostic_details)),
    )
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.PARTIAL if not coverage.is_complete else SnapshotStatus.COMPLETE,
        markets=tuple(markets),
        coverage=coverage,
        retrieved_at=retrieved_at,
    )


class DeadlineExceededError(TimeoutError):
    """The shared retrieval deadline has elapsed."""


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Internal retrieval context shared by all provider adapters."""

    deadline: datetime | str
    request_id: str | None = None
    cache_status: str = "disabled"

    def __post_init__(self) -> None:
        deadline = normalize_timestamp(self.deadline)
        if deadline is None:
            raise ValueError("retrieval deadline is required")
        request_id = self.request_id
        if request_id is not None:
            if not is_valid_request_id(request_id):
                raise ValueError("retrieval request_id is invalid")
        object.__setattr__(self, "deadline", deadline)
        object.__setattr__(self, "request_id", request_id)
        if self.cache_status not in {"hit", "miss", "disabled", "stale", "error"}:
            raise ValueError("retrieval cache_status is invalid")

    def with_cache_status(self, status: str) -> "RetrievalContext":
        """Return this request context with its cache state for provider telemetry."""

        return RetrievalContext(self.deadline, self.request_id, status)

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
    market_statuses: tuple[MarketStatus | str, ...] = (
        MarketStatus.AVAILABLE,
        MarketStatus.SUSPENDED,
    )
    pregame_only: bool = True
    season: str | None = None

    def __post_init__(self) -> None:
        sport = self.sport.strip().upper() if isinstance(self.sport, str) else ""
        league = self.league.strip().upper() if isinstance(self.league, str) else ""
        if sport != "NBA" or league != "NBA":
            raise ValueError("the shared provider query supports NBA only")
        # A repeated status names the same semantic query, so it is deduplicated
        # here rather than becoming a second cache key and a second refresh.
        statuses = tuple(
            dict.fromkeys(
                normalize_market_status(status).value for status in self.market_statuses
            )
        )
        if not statuses:
            raise ValueError("query market_statuses must not be empty")
        if self.pregame_only is not True:
            raise ValueError("the shared provider query supports pregame markets only")
        season = self.season
        if season is not None:
            season = self._canonical_season(season)
        object.__setattr__(self, "sport", sport)
        object.__setattr__(self, "league", league)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "market_statuses", statuses)

    @staticmethod
    def _canonical_season(season: Any) -> str:
        """Require one canonical NBA season before any provider is called.

        The two-digit end year must be the calendar year after the four-digit
        start year, so a shape-valid but impossible label such as ``2024-99``
        is rejected here rather than reaching a provider.  This mirrors
        ``app.providers.nba_stats.validate_canonical_season``, which this
        contract module cannot import without pulling ``nba_api`` into every
        DFS consumer.
        """

        if not isinstance(season, str):
            raise ValueError("query season must be in YYYY-YY form")
        value = season.strip()
        match = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if match is None:
            raise ValueError("query season must be in YYYY-YY form")
        if int(match.group(2)) != (int(match.group(1)) + 1) % 100:
            raise ValueError("query season must span consecutive calendar years")
        return value

@runtime_checkable
class ProviderSnapshotProvider(Protocol):
    """Single public adapter seam for normalized provider snapshots."""

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Retrieve one complete or partial snapshot for the semantic query."""


__all__ = [
    "AthleteEvidence",
    "AppearanceEvidence",
    "CompetitionEvidence",
    "EventEvidence",
    "LeagueEvidence",
    "MarketStatus",
    "MarketVariant",
    "MarketThreshold",
    "ModifierKind",
    "PriceKind",
    "PriceScope",
    "CoverageEvidence",
    "CoverageCode",
    "DeadlineExceededError",
    "MalformedProviderResponseError",
    "NormalizedLabel",
    "PlayerProjectionMarket",
    "NBAMarketQuery",
    "ProviderSnapshot",
    "ProviderSnapshotProvider",
    "RetrievalContext",
    "Selection",
    "SelectionModifier",
    "SelectionDirection",
    "ScoringPeriod",
    "SnapshotStatus",
    "SportEvidence",
    "StatisticEvidence",
    "TeamEvidence",
    "normalize_market_variant",
    "normalize_market_status",
    "normalize_modifier_kind",
    "normalize_coverage_code",
    "normalize_selection_direction",
    "normalize_scoring_period",
    "normalize_timestamp",
]
