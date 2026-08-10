"""Immutable, factual comparison values for the DFS board.

A Comparison Group is a statement of fact about markets that name the same
canonical event, canonical athlete, canonical statistic, and scoring period.
Nothing in this module models an opinion: there is no probability, expected
value, recommendation, average, preferred market, entry payout, or
cross-provider fantasy assumption, and every numeric value is an exact
``Decimal`` carried from the provider's own threshold evidence.

The module owns the closed vocabularies a comparison is built from, the
versioned deterministic reference scheme, and the immutable value types.  It
knows nothing about retrieval, catalogs, or persistence; the board service owns
those seams.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from collections.abc import Iterable, Sequence

from app.domain.statistics import ScoringPeriod
from app.providers.dfs import MarketStatus, MarketVariant, PlayerProjectionMarket, Selection


#: Bumped whenever the facts a reference is derived from change.  A reference
#: is stable exactly while its defining source or canonical identity is
#: unchanged, so a consumer can cache one and detect a redefinition.
REFERENCE_VERSION = "1"

_FIELD_SEPARATOR = "\x1f"
_KIND_SEPARATOR = "\x1e"


def _reference(kind: str, parts: Sequence[object]) -> str:
    """One versioned deterministic reference over the parts that define it."""

    payload = _FIELD_SEPARATOR.join(
        "" if part is None else str(part) for part in parts
    )
    digest = hashlib.sha256(
        f"{kind}{_KIND_SEPARATOR}{REFERENCE_VERSION}{_KIND_SEPARATOR}{payload}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    return f"{kind}_{REFERENCE_VERSION}_{digest}"


def market_reference(market: PlayerProjectionMarket) -> str:
    """The versioned reference for one normalized market.

    A provider market ID is the market's own source identity, so it defines the
    reference by itself.  A provider that publishes no market ID names the
    market by the facts it did report -- the athlete, event, statistic,
    threshold, variant, and scoring period -- and those define the reference
    instead.  Availability is deliberately absent from both: a market that is
    suspended and then available again is the same market.
    """

    if market.market_id is not None:
        return _reference("mkt", (market.provider, market.market_id))
    athlete = market.athlete
    event = market.event
    statistic = market.statistic
    threshold = market.threshold
    return _reference(
        "mkt",
        (
            market.provider,
            None if athlete is None else athlete.provider_id,
            None if athlete is None else athlete.name,
            None if event is None else event.provider_id,
            None if statistic is None else statistic.provider_id,
            None if statistic is None else statistic.label,
            None if threshold is None else threshold.value,
            None if threshold is None else threshold.unit,
            market.variant,
            market.scoring_period,
        ),
    )


def selection_reference(market_ref: str, selection: Selection) -> str:
    """The versioned reference for one selection of a referenced market."""

    return _reference(
        "sel",
        (market_ref, selection.selection_id, selection.direction, selection.label),
    )


class MarketFreshness(str, Enum):
    """How contemporaneous one market's provider observation is."""

    FRESH = "fresh"
    STALE = "stale"


class ComparisonFreshness(str, Enum):
    """Freshness of one Comparison Group's member observations.

    ``MIXED`` is the explicit Mixed-Freshness Comparison: the group's members
    come from observations that are not contemporaneous, so a reader must not
    read the spread as a single instant.
    """

    FRESH = "fresh"
    STALE = "stale"
    MIXED = "mixed"


class ComparisonExclusion(str, Enum):
    """Closed vocabulary for a market that is visible but not comparable.

    Every excluded market stays on the board as an :class:`UnresolvedMarket`;
    exclusion never removes evidence, it only keeps an unsound group from being
    built.
    """

    MISSING_ATHLETE_EVIDENCE = "missing_athlete_evidence"
    UNRESOLVED_ATHLETE = "unresolved_athlete"
    MISSING_EVENT_EVIDENCE = "missing_event_evidence"
    UNRESOLVED_EVENT = "unresolved_event"
    UNMAPPED_STATISTIC = "unmapped_statistic"
    NON_COMPARABLE_STATISTIC = "non_comparable_statistic"
    MISSING_THRESHOLD = "missing_threshold"
    STALE_SNAPSHOT = "stale_snapshot"
    COMPARISON_UNAVAILABLE = "comparison_unavailable"


class CatalogAvailabilityReason(str, Enum):
    """Why a canonical catalog cannot support comparison identity."""

    MISSING = "catalog_missing"
    STALE = "catalog_stale"
    NOT_CONFIGURED = "catalog_not_configured"


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def exact_seconds(value: object) -> Decimal:
    """Convert a timedelta-like duration to exact decimal seconds."""

    days = getattr(value, "days", None)
    seconds = getattr(value, "seconds", None)
    microseconds = getattr(value, "microseconds", None)
    if days is None or seconds is None or microseconds is None:
        raise ValueError("an exact duration must be a timedelta")
    return Decimal(days * 86400 + seconds) + (
        Decimal(microseconds) / Decimal(1_000_000)
    )


@dataclass(frozen=True, slots=True)
class CatalogAvailability:
    """Identity, age, and usability of one canonical catalog."""

    catalog: str
    season: str | None
    available: bool
    reason: CatalogAvailabilityReason | None = None
    last_success_at: datetime | None = None
    age_seconds: Decimal | None = None
    max_age_seconds: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, str) or not self.catalog.strip():
            raise ValueError("catalog availability requires a catalog name")
        if self.season is not None and (
            not isinstance(self.season, str) or not self.season.strip()
        ):
            raise ValueError("catalog availability requires a season or None")
        if self.available and self.reason is not None:
            raise ValueError("an available catalog cannot carry a reason")
        if not self.available and self.reason is None:
            raise ValueError("an unavailable catalog requires a reason")
        if self.last_success_at is not None:
            object.__setattr__(
                self,
                "last_success_at",
                _aware_utc(self.last_success_at, field="catalog last_success_at"),
            )
        for name in ("age_seconds", "max_age_seconds"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                raise ValueError(f"catalog {name} must be an exact Decimal or None")


@dataclass(frozen=True, slots=True)
class ComparisonAvailability:
    """Whether the board may state comparisons at all, and on what evidence."""

    available: bool
    catalogs: tuple[CatalogAvailability, ...] = ()

    def __post_init__(self) -> None:
        catalogs = tuple(self.catalogs)
        if any(not isinstance(entry, CatalogAvailability) for entry in catalogs):
            raise ValueError("comparison availability requires CatalogAvailability values")
        ordered = tuple(sorted(catalogs, key=lambda entry: entry.catalog))
        if self.available and any(not entry.available for entry in ordered):
            raise ValueError("comparisons are unavailable while a catalog is unusable")
        object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "catalogs", ordered)

    @property
    def unavailable_catalogs(self) -> tuple[CatalogAvailability, ...]:
        return tuple(entry for entry in self.catalogs if not entry.available)


@dataclass(frozen=True, slots=True)
class ComparisonKey:
    """The four facts every member of a Comparison Group shares."""

    canonical_event_id: str
    canonical_athlete_id: int
    canonical_statistic_id: str
    scoring_period: ScoringPeriod

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_event_id, str) or not self.canonical_event_id.strip():
            raise ValueError("a comparison key requires a canonical event id")
        if isinstance(self.canonical_athlete_id, bool) or not isinstance(
            self.canonical_athlete_id, int
        ):
            raise ValueError("a comparison key requires a canonical athlete id")
        if not isinstance(self.canonical_statistic_id, str) or not self.canonical_statistic_id.strip():
            raise ValueError("a comparison key requires a canonical statistic id")
        if not isinstance(self.scoring_period, ScoringPeriod):
            raise ValueError("a comparison key requires a ScoringPeriod")
        if self.scoring_period is ScoringPeriod.UNKNOWN:
            raise ValueError("a comparison key requires an explicit scoring period")

    @property
    def reference(self) -> str:
        """The versioned reference defined by this canonical identity."""

        return _reference(
            "cmp",
            (
                self.canonical_event_id,
                self.canonical_athlete_id,
                self.canonical_statistic_id,
                self.scoring_period.value,
            ),
        )

    @property
    def order(self) -> tuple[str, int, str, str]:
        return (
            self.canonical_event_id,
            self.canonical_athlete_id,
            self.canonical_statistic_id,
            self.scoring_period.value,
        )


@dataclass(frozen=True, slots=True)
class ComparisonMember:
    """One market offered against a Comparison Group's canonical identity."""

    market_reference: str
    provider: str
    threshold: Decimal
    threshold_unit: str
    variant: MarketVariant
    status: MarketStatus
    retrieved_at: datetime
    freshness: MarketFreshness
    selection_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.threshold, Decimal):
            raise ValueError("a comparison member threshold must be an exact Decimal")
        if not isinstance(self.variant, MarketVariant):
            raise ValueError("a comparison member requires a MarketVariant")
        if not isinstance(self.status, MarketStatus):
            raise ValueError("a comparison member requires a MarketStatus")
        if not isinstance(self.freshness, MarketFreshness):
            raise ValueError("a comparison member requires a MarketFreshness")
        object.__setattr__(
            self,
            "retrieved_at",
            _aware_utc(self.retrieved_at, field="comparison member retrieved_at"),
        )
        object.__setattr__(
            self, "selection_references", tuple(sorted(self.selection_references))
        )

    @property
    def order(self) -> tuple[str, Decimal, str, str, str]:
        return (
            self.provider,
            self.threshold,
            self.variant.value,
            self.status.value,
            self.market_reference,
        )


@dataclass(frozen=True, slots=True)
class ComparisonSummary:
    """Exactly the facts a Comparison Group's members already state.

    Minimum, maximum, and Threshold Spread are exact decimal arithmetic over
    the thresholds the providers published.  Nothing is averaged, ranked,
    priced, or recommended.
    """

    minimum_threshold: Decimal
    maximum_threshold: Decimal
    threshold_spread: Decimal
    provider_count: int
    market_count: int
    freshness: ComparisonFreshness
    market_references: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("minimum_threshold", "maximum_threshold", "threshold_spread"):
            if not isinstance(getattr(self, name), Decimal):
                raise ValueError(f"comparison summary {name} must be an exact Decimal")
        if self.maximum_threshold < self.minimum_threshold:
            raise ValueError("comparison summary maximum cannot precede its minimum")
        if self.threshold_spread != self.maximum_threshold - self.minimum_threshold:
            raise ValueError("comparison summary spread must be the exact difference")
        if not isinstance(self.freshness, ComparisonFreshness):
            raise ValueError("comparison summary requires a ComparisonFreshness")
        references = tuple(self.market_references)
        if tuple(sorted(references)) != references:
            raise ValueError("comparison summary market references must be sorted")
        if len(references) != self.market_count:
            raise ValueError("comparison summary market count must match its references")

    @classmethod
    def of(cls, members: Sequence[ComparisonMember]) -> "ComparisonSummary":
        """Summarize members with exact decimal arithmetic only."""

        if not members:
            raise ValueError("a comparison summary requires at least one member")
        thresholds = [member.threshold for member in members]
        minimum = min(thresholds)
        maximum = max(thresholds)
        retrieved = {member.retrieved_at for member in members}
        freshness_values = {member.freshness for member in members}
        if len(retrieved) > 1 or len(freshness_values) > 1:
            freshness = ComparisonFreshness.MIXED
        elif freshness_values == {MarketFreshness.STALE}:
            freshness = ComparisonFreshness.STALE
        else:
            freshness = ComparisonFreshness.FRESH
        return cls(
            minimum_threshold=minimum,
            maximum_threshold=maximum,
            threshold_spread=maximum - minimum,
            provider_count=len({member.provider for member in members}),
            market_count=len(members),
            freshness=freshness,
            market_references=tuple(
                sorted(member.market_reference for member in members)
            ),
        )


@dataclass(frozen=True, slots=True)
class ComparisonGroup:
    """One canonical identity and every market that offers it."""

    key: ComparisonKey
    members: tuple[ComparisonMember, ...]
    summary: ComparisonSummary

    def __post_init__(self) -> None:
        if not isinstance(self.key, ComparisonKey):
            raise ValueError("a comparison group requires a ComparisonKey")
        members = tuple(self.members)
        if not members:
            raise ValueError("a comparison group requires at least one member")
        if any(not isinstance(member, ComparisonMember) for member in members):
            raise ValueError("comparison members must be ComparisonMember values")
        if tuple(sorted(members, key=lambda member: member.order)) != members:
            raise ValueError("comparison members must be deterministically ordered")

    @property
    def reference(self) -> str:
        return self.key.reference

    @property
    def is_mixed_freshness(self) -> bool:
        """Whether this is an explicit Mixed-Freshness Comparison."""

        return self.summary.freshness is ComparisonFreshness.MIXED


@dataclass(frozen=True, slots=True)
class UnresolvedMarket:
    """One visible market that no group may contain, and exactly why."""

    market_reference: str
    provider: str
    reason: ComparisonExclusion
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ComparisonExclusion):
            raise ValueError("an unresolved market requires a ComparisonExclusion")
        detail = self.detail
        if detail is not None and (not isinstance(detail, str) or not detail.strip()):
            raise ValueError("an unresolved market detail must be a non-empty string or None")

    @property
    def order(self) -> tuple[str, str, str]:
        return (self.reason.value, self.provider, self.market_reference)


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """One provider's contribution to the board, with its own warnings."""

    provider: str
    status: str
    reason: str | None = None
    retrieved_at: datetime | None = None
    age_seconds: Decimal | None = None
    freshness: MarketFreshness | None = None
    market_count: int = 0
    warning_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.retrieved_at is not None:
            object.__setattr__(
                self,
                "retrieved_at",
                _aware_utc(self.retrieved_at, field="provider report retrieved_at"),
            )
        if self.age_seconds is not None and not isinstance(self.age_seconds, Decimal):
            raise ValueError("provider report age_seconds must be an exact Decimal")
        object.__setattr__(self, "warning_codes", tuple(sorted(self.warning_codes)))


@dataclass(frozen=True, slots=True)
class ComparisonFilters:
    """Optional central narrowing of one comparison board read.

    Every filter names an exact identity.  There is deliberately no fuzzy or
    partial name filter: a caller narrows by the canonical identities the board
    itself established, never by a label that could match something else.
    """

    providers: tuple[str, ...] = ()
    canonical_athlete_ids: tuple[int, ...] = ()
    canonical_event_ids: tuple[str, ...] = ()
    canonical_statistic_ids: tuple[str, ...] = ()
    market_statuses: tuple[MarketStatus, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "providers", _unique_sorted(self.providers, _provider_filter_name)
        )
        object.__setattr__(
            self,
            "canonical_athlete_ids",
            _unique_sorted(self.canonical_athlete_ids, _athlete_filter_id),
        )
        object.__setattr__(
            self,
            "canonical_event_ids",
            _unique_sorted(self.canonical_event_ids, _text_filter_value),
        )
        object.__setattr__(
            self,
            "canonical_statistic_ids",
            _unique_sorted(self.canonical_statistic_ids, _text_filter_value),
        )
        statuses = tuple(
            value if isinstance(value, MarketStatus) else MarketStatus(value)
            for value in self.market_statuses
        )
        object.__setattr__(
            self,
            "market_statuses",
            tuple(sorted(dict.fromkeys(statuses), key=lambda status: status.value)),
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.providers
            or self.canonical_athlete_ids
            or self.canonical_event_ids
            or self.canonical_statistic_ids
            or self.market_statuses
        )

    @property
    def supported_filters(self) -> tuple[str, ...]:
        """The narrowing filters a caller may add, for a too-large board."""

        return SUPPORTED_NARROWING_FILTERS


#: The exact narrowing filters the board supports, reported verbatim when a
#: read is refused for size so a caller knows what would make it smaller.
SUPPORTED_NARROWING_FILTERS = (
    "canonical_athlete_ids",
    "canonical_event_ids",
    "canonical_statistic_ids",
    "market_statuses",
    "providers",
)


def _provider_filter_name(value: object) -> str:
    name = value.strip().casefold() if isinstance(value, str) else ""
    if not name:
        raise ValueError("a provider filter must be a non-empty name")
    return name


def _text_filter_value(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError("a canonical identity filter must be a non-empty string")
    return text


def _athlete_filter_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("a canonical athlete filter must be an integer id")
    return value


def _unique_sorted(values: Iterable[object], normalize) -> tuple:
    return tuple(sorted(dict.fromkeys(normalize(value) for value in values)))


@dataclass(frozen=True, slots=True)
class ComparisonBoard:
    """One deterministic, factual comparison board read."""

    season: str | None
    generated_at: datetime
    availability: ComparisonAvailability
    groups: tuple[ComparisonGroup, ...] = ()
    unresolved: tuple[UnresolvedMarket, ...] = ()
    provider_reports: tuple[ProviderReport, ...] = ()
    disabled_providers: tuple[str, ...] = ()
    filters: ComparisonFilters = ComparisonFilters()
    market_count: int = 0
    contract_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _aware_utc(self.generated_at, field="comparison board generated_at"),
        )
        if not isinstance(self.availability, ComparisonAvailability):
            raise ValueError("a comparison board requires a ComparisonAvailability")
        groups = tuple(self.groups)
        if tuple(sorted(groups, key=lambda group: group.key.order)) != groups:
            raise ValueError("comparison groups must be deterministically ordered")
        unresolved = tuple(self.unresolved)
        if tuple(sorted(unresolved, key=lambda entry: entry.order)) != unresolved:
            raise ValueError("unresolved markets must be deterministically ordered")
        reports = tuple(self.provider_reports)
        if tuple(sorted(reports, key=lambda report: report.provider)) != reports:
            raise ValueError("provider reports must be deterministically ordered")
        if not self.availability.available and groups:
            raise ValueError("an unavailable comparison board cannot carry groups")
        disabled = tuple(sorted(self.disabled_providers))
        if disabled != tuple(self.disabled_providers):
            raise ValueError("disabled providers must be deterministically ordered")
        if not isinstance(self.filters, ComparisonFilters):
            raise ValueError("a comparison board requires ComparisonFilters")

    @property
    def is_empty(self) -> bool:
        """Whether a complete read found nothing to compare and nothing left over."""

        return not self.groups and not self.unresolved

    @property
    def mixed_freshness_groups(self) -> tuple[ComparisonGroup, ...]:
        return tuple(group for group in self.groups if group.is_mixed_freshness)


__all__ = [
    "REFERENCE_VERSION",
    "SUPPORTED_NARROWING_FILTERS",
    "CatalogAvailability",
    "CatalogAvailabilityReason",
    "ComparisonAvailability",
    "ComparisonBoard",
    "ComparisonExclusion",
    "ComparisonFilters",
    "ComparisonFreshness",
    "ComparisonGroup",
    "ComparisonKey",
    "ComparisonMember",
    "ComparisonSummary",
    "MarketFreshness",
    "ProviderReport",
    "UnresolvedMarket",
    "exact_seconds",
    "market_reference",
    "selection_reference",
]
