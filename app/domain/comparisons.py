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
those seams.  What one market *says* -- the canonical semantics a reference is
derived in and the retained audit content that orders evidence -- belongs to
:mod:`app.domain.market_content`, which the provider contract shares, so a
repeat means the same thing at both seams.  When one observation *was* --
exact seconds, the window domain, and the freshness boundary itself -- belongs
to :mod:`app.domain.freshness`, which the snapshot cache and the configuration
boundary share for the same reason.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from collections.abc import Iterable, Sequence
from typing import Any

from app.domain.freshness import (
    exact_context as _exact_context,
    exact_scaled_seconds,
    exact_seconds,
)
from app.domain.market_content import (
    MAX_EXACT_DIFFERENCE_SPAN,
    NORMALIZED_DECIMAL_PLACE_LIMIT,
    NumericDomainError,
    aware_utc as _aware_utc,
    canonical_decimal,
    normalized_decimal,
    canonical_selections,
    encode_canonical as _encode,
    market_content_key,
    market_evidence_key,
    reported_evidence_facts as _reported_evidence_facts,
    selection_facts as _selection_facts,
)
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.providers.dfs import (
    AppearanceEvidence,
    AthleteEvidence,
    CompetitionEvidence,
    EventEvidence,
    LeagueEvidence,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    PlayerProjectionMarket,
    Selection,
    SelectionModifier,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
)


#: Bumped whenever the facts a reference is derived from change.  A reference
#: is stable exactly while its defining source or canonical identity is
#: unchanged, so a consumer can cache one and detect a redefinition.
#:
#: Version 2 replaced delimiter-joined text with the canonical injective
#: encoding below, so a value containing a separator can no longer be read as
#: two fields and an exact decimal no longer depends on its written scale.
REFERENCE_VERSION = "2"


def _reference(kind: str, payload: object) -> str:
    """One versioned deterministic reference over the facts that define it."""

    digest = hashlib.sha256(
        _encode((kind, REFERENCE_VERSION, payload))
    ).hexdigest()[:32]
    return f"{kind}_{REFERENCE_VERSION}_{digest}"


def market_reference(market: PlayerProjectionMarket) -> str:
    """The versioned reference for one normalized market.

    A provider market ID is the market's own source identity, so it defines the
    reference by itself, and a market that is suspended and then available
    again keeps that identity.

    A provider that publishes no market ID names the market by every fact it
    did report -- the athlete, the complete event evidence including its teams
    and times, the statistic, threshold, status, variant, scoring period,
    source labels, and the offered selections with their modifiers and prices
    -- and those define the reference instead.  Status takes part there because
    it is the only thing that distinguishes an offering the provider is
    currently taking from a suspended one it names identically, and both are
    legitimate distinct offerings the board must keep apart rather than read as
    one market contradicting itself.
    """

    if market.market_id is not None:
        return _reference("mkt", ("source_identity", market.provider, market.market_id))
    return _reference("mkt", ("reported_evidence", _reported_evidence_facts(market)))


def selection_reference(market_ref: str, selection: Selection) -> str:
    """The versioned reference for one selection of a referenced market.

    Every fact that defines the offering -- its identity, labels, direction,
    status, modifiers, and prices -- takes part, so two distinctly priced or
    distinctly modified selections are never the same reference.
    """

    return _reference("sel", (market_ref, _selection_facts(selection)))


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
    FUTURE_SNAPSHOT = "future_snapshot"
    CONFLICTING_MARKET_IDENTITY = "conflicting_market_identity"
    COMPARISON_UNAVAILABLE = "comparison_unavailable"


class CatalogAvailabilityReason(str, Enum):
    """Why a canonical catalog cannot support comparison identity."""

    MISSING = "catalog_missing"
    STALE = "catalog_stale"
    NOT_CONFIGURED = "catalog_not_configured"


def exact_difference(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    """The exact difference of two normalized decimals, ambient context aside.

    Decimal arithmetic reads the ambient context, so the same two provider
    thresholds subtracted inside ``localcontext(prec=5)`` and outside it are
    two different numbers, and a threshold carrying more digits than the
    ambient precision is rounded into a value that is not the difference of
    the numbers the board states.  Nor is precision the only inherited state:
    a narrow ``Emax``, a clamping context, or a caller's own traps would each
    change or refuse the result.

    So none of it is inherited.  The subtraction is performed by a fresh
    :class:`decimal.Context` that states every field it depends on --
    precision, exponent range, rounding, capitals, clamping, and a trap for
    every signal -- and it is performed by that context's own method, never by
    an operator, so no thread-local state takes part at all.

    Its precision is :data:`MAX_EXACT_DIFFERENCE_SPAN`, the widest exact
    difference the normalized numeric domain admits.  Both operands must be
    inside that domain, which is enforced where a provider number is first
    accepted, so every threshold the contract accepted subtracts exactly and
    no trap can fire.  A decimal from anywhere else is refused with one typed
    :class:`NumericDomainError` rather than allocating a difference whose
    digits are the distance between two exponents.
    """

    for name, value in (("minuend", minuend), ("subtrahend", subtrahend)):
        normalized_decimal(value, field=f"an exact difference {name}")
    return _exact_context(MAX_EXACT_DIFFERENCE_SPAN).subtract(minuend, subtrahend)


def _exact_count(value: object, field: str) -> None:
    """Require one public count to be an exact non-negative built-in integer.

    ``False == 0`` and ``Decimal("2") == 2`` in Python, so a count checked only
    by equality against what was retained accepts a boolean, a float, or a
    decimal that happens to agree -- and then states it, so a body whose
    contract says a market count is an integer can publish ``false`` or
    ``2.0``.  Every count is required to be an ``int`` that is not a ``bool``
    before any equality reads it, so a value that could never be a count is
    refused where it is constructed rather than downstream.
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be an exact count")


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

    The spread is exact independently of the decimal context a caller happens
    to be inside, and validation compares it against that same exact
    difference, so a summary can neither store nor accept a rounded spread.
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
        if self.threshold_spread != exact_difference(
            self.maximum_threshold, self.minimum_threshold
        ):
            raise ValueError("comparison summary spread must be the exact difference")
        if not isinstance(self.freshness, ComparisonFreshness):
            raise ValueError("comparison summary requires a ComparisonFreshness")
        _exact_count(self.provider_count, "a comparison summary provider count")
        _exact_count(self.market_count, "a comparison summary market count")
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
            threshold_spread=exact_difference(maximum, minimum),
            provider_count=len({member.provider for member in members}),
            market_count=len(members),
            freshness=freshness,
            market_references=tuple(
                sorted(member.market_reference for member in members)
            ),
        )


def _same_published_decimal(left: Decimal, right: Decimal) -> bool:
    """Whether two exact decimals are the same *published* number.

    ``Decimal("25.50") == Decimal("25.5")`` in Python, but a board writes a
    threshold in the scale the provider published, so the two reach a caller as
    different strings.  A summary is therefore compared against its members at
    the scale it would be written in, not merely at the value it compares equal
    to.
    """

    return left.as_tuple() == right.as_tuple()


def _states_exactly(summary: "ComparisonSummary", derived: "ComparisonSummary") -> bool:
    """Whether a summary is field-for-field the derivation of its members."""

    return (
        _same_published_decimal(summary.minimum_threshold, derived.minimum_threshold)
        and _same_published_decimal(summary.maximum_threshold, derived.maximum_threshold)
        and _same_published_decimal(summary.threshold_spread, derived.threshold_spread)
        and summary.provider_count == derived.provider_count
        and summary.market_count == derived.market_count
        and summary.freshness is derived.freshness
        and summary.market_references == derived.market_references
    )


@dataclass(frozen=True, slots=True)
class ComparisonGroup:
    """One canonical identity and every market that offers it.

    The summary is derived evidence, never independent evidence: a group may
    carry only the summary its own members produce.  A count, a bound, a
    spread, or a reference list that its members do not state is refused where
    the group is built, so nothing downstream -- the serializer least of all --
    can publish a comparison whose headline contradicts the markets printed
    beneath it.
    """

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
        if not isinstance(self.summary, ComparisonSummary):
            raise ValueError("a comparison group requires a ComparisonSummary")
        if not _states_exactly(self.summary, ComparisonSummary.of(members)):
            raise ValueError(
                "a comparison group summary must state exactly its members"
            )

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


# -- retained normalized evidence -----------------------------------------
#
# A board keeps every normalized market it read, not just the reduced facts a
# comparison needs.  These value types are the serialization-ready mirror of
# the provider vocabulary: closed enums, exact decimals, aware timestamps, and
# the provider's own original labels, with no provider object retained and no
# fact invented.  A resolved market links to its group and an unresolved one to
# its exclusion, so an audit reads the same market from either direction.


@dataclass(frozen=True, slots=True)
class BoardTeam:
    """Retained normalized team evidence."""

    provider_id: str | None = None
    canonical_id: int | None = None
    name: str | None = None
    abbreviation: str | None = None

    @classmethod
    def of(cls, evidence: TeamEvidence | None) -> "BoardTeam | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            canonical_id=evidence.canonical_id,
            name=evidence.name,
            abbreviation=evidence.abbreviation,
        )


@dataclass(frozen=True, slots=True)
class BoardAthlete:
    """Retained normalized athlete evidence."""

    provider_id: str | None = None
    canonical_id: int | None = None
    name: str | None = None
    team: BoardTeam | None = None

    @classmethod
    def of(cls, evidence: AthleteEvidence | None) -> "BoardAthlete | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            canonical_id=evidence.canonical_id,
            name=evidence.name,
            team=BoardTeam.of(evidence.team),
        )


@dataclass(frozen=True, slots=True)
class BoardEvent:
    """Retained normalized event evidence, including its canonical claim."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    updated_at: datetime | None = None
    status_label: str | None = None
    home_team: BoardTeam | None = None
    away_team: BoardTeam | None = None

    @classmethod
    def of(cls, evidence: EventEvidence | None) -> "BoardEvent | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            canonical_id=evidence.canonical_id,
            label=evidence.label,
            starts_at=evidence.starts_at,
            ends_at=evidence.ends_at,
            updated_at=evidence.updated_at,
            status_label=evidence.status_label,
            home_team=BoardTeam.of(evidence.home_team),
            away_team=BoardTeam.of(evidence.away_team),
        )


@dataclass(frozen=True, slots=True)
class BoardStatistic:
    """Retained normalized statistic evidence."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None
    components: tuple[str, ...] = ()

    @classmethod
    def of(cls, evidence: StatisticEvidence | None) -> "BoardStatistic | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            canonical_id=evidence.canonical_id,
            label=evidence.label,
            components=tuple(evidence.components),
        )


@dataclass(frozen=True, slots=True)
class BoardStatisticResolution:
    """What the Statistic Catalog said about one market's statistic."""

    state: MatchState
    scoring_period: ScoringPeriod
    canonical_id: str | None = None
    unit: str | None = None
    reason: str | None = None
    comparable: bool = False

    @classmethod
    def of(cls, match: StatisticMatch | None) -> "BoardStatisticResolution | None":
        if match is None:
            return None
        return cls(
            state=match.state,
            scoring_period=match.scoring_period,
            canonical_id=match.canonical_id,
            unit=None if match.unit is None else match.unit.value,
            reason=None if match.reason is None else match.reason.value,
            comparable=match.is_comparable,
        )


@dataclass(frozen=True, slots=True)
class BoardNamedEvidence:
    """Retained league or sport evidence."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None

    @classmethod
    def of(
        cls, evidence: LeagueEvidence | SportEvidence | None
    ) -> "BoardNamedEvidence | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            canonical_id=evidence.canonical_id,
            label=evidence.label,
        )


@dataclass(frozen=True, slots=True)
class BoardCompetition:
    """Retained competition evidence and the sport it belongs to."""

    provider_id: str | None = None
    canonical_id: str | None = None
    label: str | None = None
    sport: BoardNamedEvidence | None = None

    @classmethod
    def of(cls, evidence: CompetitionEvidence | None) -> "BoardCompetition | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            canonical_id=evidence.canonical_id,
            label=evidence.label,
            sport=BoardNamedEvidence.of(evidence.sport),
        )


@dataclass(frozen=True, slots=True)
class BoardAppearance:
    """Retained appearance evidence for the line's owning appearance."""

    provider_id: str | None = None
    appearance_type: str | None = None
    label: str | None = None

    @classmethod
    def of(cls, evidence: AppearanceEvidence | None) -> "BoardAppearance | None":
        if evidence is None:
            return None
        return cls(
            provider_id=evidence.provider_id,
            appearance_type=evidence.appearance_type,
            label=evidence.label,
        )


@dataclass(frozen=True, slots=True)
class BoardThreshold:
    """One market's exact threshold and the text the provider wrote."""

    value: Decimal
    unit: str
    original_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise ValueError("a board threshold must be an exact Decimal")

    @classmethod
    def of(cls, threshold: MarketThreshold | None) -> "BoardThreshold | None":
        if threshold is None:
            return None
        return cls(
            value=threshold.value,
            unit=threshold.unit,
            original_value=threshold.original_value,
        )


@dataclass(frozen=True, slots=True)
class BoardModifier:
    """One provider-defined selection adjustment, never an entry payout."""

    value: Decimal
    kind: str
    scope: str
    label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise ValueError("a board modifier value must be an exact Decimal")

    @classmethod
    def of(cls, modifier: SelectionModifier) -> "BoardModifier":
        return cls(
            value=modifier.value,
            kind=modifier.kind,
            scope=modifier.scope,
            label=modifier.label,
        )


@dataclass(frozen=True, slots=True)
class BoardSelection:
    """One retained selection, with its stable reference and every price."""

    selection_reference: str
    selection_id: str | None = None
    label: str | None = None
    direction: str | None = None
    direction_label: str | None = None
    status: str | None = None
    modifiers: tuple[BoardModifier, ...] = ()
    american_price: int | None = None
    decimal_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.decimal_price is not None and not isinstance(self.decimal_price, Decimal):
            raise ValueError("a board selection decimal_price must be a Decimal or None")
        object.__setattr__(self, "modifiers", tuple(self.modifiers))

    @classmethod
    def of(cls, reference: str, selection: Selection) -> "BoardSelection":
        return cls(
            selection_reference=reference,
            selection_id=selection.selection_id,
            label=selection.label,
            direction=selection.direction,
            direction_label=selection.direction_label,
            status=selection.status,
            modifiers=tuple(
                BoardModifier.of(modifier) for modifier in selection.modifiers
            ),
            american_price=selection.american_price,
            decimal_price=selection.decimal_price,
        )


@dataclass(frozen=True, slots=True)
class BoardObservation:
    """When one snapshot was retrieved, measured against one observation.

    ``age_seconds`` is never negative: an observation the provider timestamped
    ahead of the board's own clock is reported as ``is_future`` and enters no
    comparison, rather than being read as a negative age.
    """

    provider: str
    snapshot_status: str
    retrieved_at: datetime
    observed_at: datetime
    age_seconds: Decimal
    freshness: MarketFreshness | None = None
    is_future: bool = False

    def __post_init__(self) -> None:
        for name in ("retrieved_at", "observed_at"):
            object.__setattr__(
                self,
                name,
                _aware_utc(getattr(self, name), field=f"observation {name}"),
            )
        if not isinstance(self.age_seconds, Decimal):
            raise ValueError("an observation age must be an exact Decimal")
        if self.age_seconds < 0:
            raise ValueError("an observation age can never be negative")
        if self.is_future and self.freshness is not None:
            raise ValueError("a future observation has no freshness")


def observation_evidence_key(observation: BoardObservation) -> bytes:
    """A total, content-derived order over one retained snapshot observation."""

    return _encode(
        (
            observation.provider,
            observation.snapshot_status,
            observation.retrieved_at,
            observation.observed_at,
            observation.age_seconds,
            observation.freshness,
            observation.is_future,
        )
    )


@dataclass(frozen=True, slots=True)
class BoardMarket:
    """One retained normalized market, and where it went on this board.

    Exactly one of ``comparison_reference`` and ``exclusion`` is set, so a
    market is either part of a stated comparison or visibly, auditably not.

    When several disagreeing normalized observations claim one source identity,
    every one of them is retained as its own ``BoardMarket``.  Each states its
    position among the contradicting observations in ``conflict_ordinal`` and
    how many there were in ``conflict_count``, so an audit reads exactly what
    contradicted what rather than a single survivor chosen by arrival order.
    """

    market_reference: str
    provider: str
    observation: BoardObservation
    market_id: str | None = None
    athlete: BoardAthlete | None = None
    event: BoardEvent | None = None
    team: BoardTeam | None = None
    opponent: BoardTeam | None = None
    league: BoardNamedEvidence | None = None
    competition: BoardCompetition | None = None
    sport: BoardNamedEvidence | None = None
    statistic: BoardStatistic | None = None
    statistic_resolution: BoardStatisticResolution | None = None
    threshold: BoardThreshold | None = None
    status: MarketStatus = MarketStatus.AVAILABLE
    status_label: str | None = None
    variant: MarketVariant = MarketVariant.STANDARD
    variant_label: str | None = None
    scoring_period: ScoringPeriod = ScoringPeriod.UNKNOWN
    scoring_period_label: str | None = None
    starts_at: datetime | None = None
    updated_at: datetime | None = None
    appearance: BoardAppearance | None = None
    selections: tuple[BoardSelection, ...] = ()
    comparison_reference: str | None = None
    exclusion: ComparisonExclusion | None = None
    exclusion_detail: str | None = None
    conflict_ordinal: int | None = None
    conflict_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observation, BoardObservation):
            raise ValueError("a board market requires a BoardObservation")
        if not isinstance(self.status, MarketStatus):
            raise ValueError("a board market requires a MarketStatus")
        if not isinstance(self.variant, MarketVariant):
            raise ValueError("a board market requires a MarketVariant")
        if not isinstance(self.scoring_period, ScoringPeriod):
            raise ValueError("a board market requires a ScoringPeriod")
        if self.exclusion is not None and not isinstance(
            self.exclusion, ComparisonExclusion
        ):
            raise ValueError("a board market exclusion must be a ComparisonExclusion")
        if (self.comparison_reference is None) == (self.exclusion is None):
            raise ValueError(
                "a board market is either compared or excluded, never both or neither"
            )
        if (self.conflict_ordinal is None) != (self.conflict_count is None):
            raise ValueError(
                "contradiction evidence states both an ordinal and a count"
            )
        if self.conflict_count is not None:
            # Typed before compared: ``True < 2`` and ``Decimal("2") < 2`` both
            # answer, so an ordinal or a count is refused as a count before any
            # relational check reads it.
            _exact_count(self.conflict_ordinal, "a board market conflict ordinal")
            _exact_count(self.conflict_count, "a board market conflict count")
            if self.conflict_count < 2:
                raise ValueError("a contradiction requires at least two observations")
            if not 0 <= self.conflict_ordinal < self.conflict_count:
                raise ValueError("a contradiction ordinal must name one observation")
            if self.exclusion is not ComparisonExclusion.CONFLICTING_MARKET_IDENTITY:
                raise ValueError(
                    "a contradicted observation is excluded as a conflicting identity"
                )
        object.__setattr__(self, "selections", tuple(self.selections))

    @property
    def is_compared(self) -> bool:
        return self.comparison_reference is not None

    @property
    def order(self) -> tuple[str, str, int]:
        return (
            self.provider,
            self.market_reference,
            0 if self.conflict_ordinal is None else self.conflict_ordinal,
        )


@dataclass(frozen=True, slots=True)
class BoardCoverage:
    """One provider retrieval's counts and completion evidence.

    These are the collector's own bounded counts and closed coverage codes, not
    an analysis of them: nothing here is a rate, a ratio, or an inference about
    why a provider covered what it did.
    """

    fetched_count: int = 0
    eligible_count: int = 0
    normalized_count: int = 0
    skipped_count: int = 0
    pagination_complete: bool | None = None
    fanout_complete: bool | None = None
    expected_total: int | None = None
    is_complete: bool = False
    warning_codes: tuple[str, ...] = ()
    skipped_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "fetched_count",
            "eligible_count",
            "normalized_count",
            "skipped_count",
        ):
            _exact_count(getattr(self, name), f"a coverage {name.replace('_', ' ')}")
        # The one count a provider may leave unstated: nothing said how many
        # there were to fetch.  Stated, it is a count like any other.
        if self.expected_total is not None:
            _exact_count(self.expected_total, "a coverage expected total")
        for name in ("warning_codes", "skipped_reasons"):
            codes = tuple(
                sorted({getattr(code, "value", code) for code in getattr(self, name)})
            )
            object.__setattr__(self, name, codes)

    @classmethod
    def of(cls, coverage: Any | None) -> "BoardCoverage | None":
        if coverage is None:
            return None
        return cls(
            fetched_count=coverage.fetched_count,
            eligible_count=coverage.eligible_count,
            normalized_count=coverage.normalized_count,
            skipped_count=coverage.skipped_count,
            pagination_complete=coverage.pagination_complete,
            fanout_complete=coverage.fanout_complete,
            expected_total=coverage.expected_total,
            is_complete=coverage.is_complete,
            warning_codes=coverage.warning_codes,
            skipped_reasons=coverage.skipped_reasons,
        )


@dataclass(frozen=True, slots=True)
class BoardCacheState:
    """Where one provider's observation came from, and a failed refresh.

    The failure reason is the collector's own bounded classification, never
    provider exception text, so no credential or upstream detail can reach a
    reader through it.
    """

    status: str | None = None
    retrieved_at: datetime | None = None
    age_seconds: Decimal | None = None
    failure_reason: str | None = None
    failure_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("retrieved_at", "failure_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _aware_utc(value, field=f"provider cache {name}")
                )
        if self.age_seconds is not None:
            if not isinstance(self.age_seconds, Decimal) or not (
                self.age_seconds.is_finite()
            ):
                raise ValueError(
                    "provider cache age_seconds must be an exact finite Decimal"
                )
            if self.age_seconds < 0:
                raise ValueError("provider cache age_seconds can never be negative")

    @classmethod
    def of(cls, outcome: Any) -> "BoardCacheState | None":
        """One outcome's cache provenance, or None when it names none."""

        age = getattr(outcome, "cache_age_seconds", None)
        state = cls(
            status=getattr(outcome, "cache_status", None),
            retrieved_at=getattr(outcome, "cache_retrieved_at", None),
            # ``ProviderOutcome`` already normalized this to an exact finite
            # Decimal, so no reader re-derives it from a float here.
            age_seconds=age,
            failure_reason=getattr(outcome, "cache_failure_reason", None),
            failure_at=getattr(outcome, "cache_failure_at", None),
        )
        return None if state == cls() else state


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """One provider's contribution to the board, with its own warnings.

    It carries the provenance a reader needs to judge the contribution: the
    retrieval outcome and its reason, the observation's age and freshness, the
    coverage counts and completion evidence behind the market count, and the
    cache state the observation was served from.
    """

    provider: str
    status: str
    reason: str | None = None
    retrieved_at: datetime | None = None
    age_seconds: Decimal | None = None
    freshness: MarketFreshness | None = None
    market_count: int = 0
    warning_codes: tuple[str, ...] = ()
    snapshot_status: str | None = None
    future_observation: bool = False
    coverage: BoardCoverage | None = None
    cache: BoardCacheState | None = None

    def __post_init__(self) -> None:
        if self.retrieved_at is not None:
            object.__setattr__(
                self,
                "retrieved_at",
                _aware_utc(self.retrieved_at, field="provider report retrieved_at"),
            )
        if self.age_seconds is not None:
            if not isinstance(self.age_seconds, Decimal):
                raise ValueError("provider report age_seconds must be an exact Decimal")
            if self.age_seconds < 0:
                raise ValueError("provider report age_seconds can never be negative")
        _exact_count(self.market_count, "a provider report market count")
        if self.coverage is not None and not isinstance(self.coverage, BoardCoverage):
            raise ValueError("provider report coverage must be BoardCoverage or None")
        if self.cache is not None and not isinstance(self.cache, BoardCacheState):
            raise ValueError("provider report cache must be BoardCacheState or None")
        object.__setattr__(self, "warning_codes", tuple(sorted(self.warning_codes)))

    @property
    def is_readable(self) -> bool:
        """Whether this provider contributed a snapshot the board could read.

        A complete, partial, permitted-stale, or empty-complete answer is
        readable; emptiness is a fact about the provider's current offerings,
        not an outage.  A retrieval that succeeded is not by itself readable:
        an observation past its provider's stale-if-error ceiling, or
        timestamped ahead of the board's own clock, resolves no market at all.

        Both are read from this report's own typed evidence -- the freshness the
        board derived and the future-observation flag -- never from exclusion
        text.  A complete or partial outcome always carries a snapshot and so
        always carries an observation, so an absent freshness means exactly that
        the observation was beyond the permitted maximum age.
        """

        return (
            self.status in READABLE_PROVIDER_STATUSES
            and not self.future_observation
            and self.freshness is not None
        )


#: Provider outcome statuses that carry a snapshot at all.  Anything else
#: contributed no observation for the board to judge.
READABLE_PROVIDER_STATUSES = frozenset({"complete", "partial"})


def has_readable_provider(reports: Iterable[ProviderReport]) -> bool:
    """Whether any provider contributed a snapshot the board could read.

    This is the single authority both the comparison assembly and the published
    response consult, so the seam that decides a board is unusable and the seam
    that reports the outage can never disagree about what readable means.
    """

    return any(report.is_readable for report in reports)


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
    markets: tuple[BoardMarket, ...] = ()
    provider_reports: tuple[ProviderReport, ...] = ()
    disabled_providers: tuple[str, ...] = ()
    filters: ComparisonFilters = ComparisonFilters()
    market_count: int = 0
    unresolved_count: int | None = None
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
        markets = tuple(self.markets)
        if any(not isinstance(market, BoardMarket) for market in markets):
            raise ValueError("retained markets must be BoardMarket values")
        if tuple(sorted(markets, key=lambda market: market.order)) != markets:
            raise ValueError("retained markets must be deterministically ordered")
        # A board counts exactly what it kept.  There is no count-only board:
        # a read that retained nothing it would have published is not a board
        # at all, and states what it observed as
        # :class:`BoardReadEvidence` instead, so nothing that reaches the
        # serializer can report a count its own collections contradict.
        _exact_count(self.market_count, "a board market count")
        if len(markets) != self.market_count:
            raise ValueError("a board must retain every market it counted")
        if self.unresolved_count is None:
            object.__setattr__(self, "unresolved_count", len(unresolved))
        else:
            _exact_count(self.unresolved_count, "a board unresolved count")
            if self.unresolved_count != len(unresolved):
                raise ValueError(
                    "a board must retain every unresolved market it counted"
                )
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
        self._require_backed_partition(groups, unresolved, markets)

    @staticmethod
    def _require_backed_partition(
        groups: tuple["ComparisonGroup", ...],
        unresolved: tuple["UnresolvedMarket", ...],
        markets: tuple[BoardMarket, ...],
    ) -> None:
        """Require the three collections to be one partition of retained evidence.

        A board is not three independent lists that merely count consistently.
        Every market reference a comparison cites, and every reference stated as
        unresolved, is read from retained :class:`BoardMarket` evidence on this
        same board -- and each reference lands on exactly one side, so a caller
        auditing a comparison always finds the observation behind it and never
        finds the same market both compared and excluded.

        Backing is not one market per reference.  Contradicting observations of
        one source identity are all retained, so a single unresolved reference
        may stand on several retained observations; what is required is that at
        least one exists and that every retained observation names where it
        went.
        """

        grouped: dict[str, str] = {}
        for group in groups:
            for member in group.members:
                if member.market_reference in grouped:
                    raise ValueError(
                        "a market reference may enter only one comparison"
                    )
                grouped[member.market_reference] = group.reference
        unresolved_references: set[str] = set()
        for entry in unresolved:
            if entry.market_reference in unresolved_references:
                raise ValueError("a market reference is unresolved only once")
            unresolved_references.add(entry.market_reference)
        if unresolved_references & set(grouped):
            raise ValueError(
                "a market reference is compared or unresolved, never both"
            )
        compared_backing = {
            market.market_reference for market in markets if market.is_compared
        }
        excluded_backing = {
            market.market_reference for market in markets if not market.is_compared
        }
        if set(grouped) - compared_backing:
            raise ValueError(
                "every compared market requires the evidence it was read from"
            )
        if unresolved_references - excluded_backing:
            raise ValueError(
                "every unresolved market requires the evidence it was read from"
            )
        for market in markets:
            if market.is_compared:
                if grouped.get(market.market_reference) != market.comparison_reference:
                    raise ValueError(
                        "retained compared evidence must name the comparison its "
                        "market entered"
                    )
            elif market.market_reference not in unresolved_references:
                raise ValueError(
                    "retained excluded evidence must name an unresolved market"
                )

    @property
    def is_empty(self) -> bool:
        """Whether a complete read found nothing to compare and nothing left over.

        Read from everything the board retained, so it can never disagree with
        the counts: a board that kept a market is not empty, whether that
        market entered a comparison or stayed unresolved.
        """

        return not self.groups and not self.unresolved and not self.markets

    @property
    def mixed_freshness_groups(self) -> tuple[ComparisonGroup, ...]:
        return tuple(group for group in self.groups if group.is_mixed_freshness)

    @property
    def markets_by_reference(self) -> dict[str, BoardMarket]:
        """Every retained market, indexed by the reference its members cite.

        A contradicted reference names several retained observations; this
        index keeps the first in board order, and ``conflicting_markets``
        exposes them all.
        """

        index: dict[str, BoardMarket] = {}
        for market in self.markets:
            index.setdefault(market.market_reference, market)
        return index

    @property
    def conflicting_markets(self) -> tuple[BoardMarket, ...]:
        """Every retained observation whose source identity was contradicted."""

        return tuple(
            market for market in self.markets if market.conflict_ordinal is not None
        )

    def markets_for(self, reference: str) -> tuple[BoardMarket, ...]:
        """The retained markets one comparison reference was built from."""

        return tuple(
            market for market in self.markets if market.comparison_reference == reference
        )


@dataclass(frozen=True, slots=True)
class BoardReadEvidence:
    """What one board read established, whether or not it published a board.

    A read that is refused after retrieval -- one over the market ceiling, say
    -- has already learned everything an operator needs to explain it: how many
    markets it observed, which providers answered and how fresh and cached
    their answers were, and whether comparison identity was available at all.
    Keeping those facts on their own value, rather than only inside the
    :class:`ComparisonBoard` a refusal never builds, is what lets the refusal be
    observed as accurately as a success.

    Every field is either a count or an already-sanitized board value, so this
    carries no provider, athlete, event, market, or selection identity of its
    own and nothing here can widen what telemetry may state.
    """

    availability: ComparisonAvailability
    provider_reports: tuple[ProviderReport, ...] = ()
    disabled_providers: tuple[str, ...] = ()
    group_count: int = 0
    market_count: int = 0
    unresolved_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.availability, ComparisonAvailability):
            raise ValueError("board read evidence requires a ComparisonAvailability")
        for name in ("group_count", "market_count", "unresolved_count"):
            _exact_count(
                getattr(self, name),
                f"a board read evidence {name.replace('_', ' ')}",
            )
        # Typed counts are not yet possible counts.  Each comparison stands on
        # at least one retained observation, each unresolved reference stands on
        # at least one more, and no reference is on both sides, so a read can
        # never have established more comparisons and unresolved markets
        # together than the observations it made.  Contradictions only widen
        # that gap, never close it, so the relation holds for the count-only
        # evidence a refusal carries exactly as it does for a published board.
        if self.group_count + self.unresolved_count > self.market_count:
            raise ValueError(
                "board read evidence cannot state more comparisons and unresolved "
                "markets than the markets it observed"
            )

    @classmethod
    def of(cls, board: ComparisonBoard) -> "BoardReadEvidence":
        """The same evidence, read from a board that was published."""

        return cls(
            availability=board.availability,
            provider_reports=board.provider_reports,
            disabled_providers=board.disabled_providers,
            group_count=len(board.groups),
            market_count=board.market_count,
            unresolved_count=board.unresolved_count,
        )


__all__ = [
    "MAX_EXACT_DIFFERENCE_SPAN",
    "NORMALIZED_DECIMAL_PLACE_LIMIT",
    "READABLE_PROVIDER_STATUSES",
    "REFERENCE_VERSION",
    "SUPPORTED_NARROWING_FILTERS",
    "BoardAppearance",
    "BoardAthlete",
    "BoardCacheState",
    "BoardCompetition",
    "BoardCoverage",
    "BoardEvent",
    "BoardMarket",
    "BoardModifier",
    "BoardNamedEvidence",
    "BoardObservation",
    "BoardReadEvidence",
    "BoardSelection",
    "BoardStatistic",
    "BoardStatisticResolution",
    "BoardTeam",
    "BoardThreshold",
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
    "NumericDomainError",
    "ProviderReport",
    "UnresolvedMarket",
    "canonical_decimal",
    "canonical_selections",
    "exact_difference",
    "exact_scaled_seconds",
    "exact_seconds",
    "has_readable_provider",
    "market_content_key",
    "market_evidence_key",
    "market_reference",
    "normalized_decimal",
    "observation_evidence_key",
    "selection_reference",
]
