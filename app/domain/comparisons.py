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


def canonical_decimal(value: Decimal) -> str:
    """The one canonical text for an exact decimal, independent of scale.

    ``25.5`` and ``25.50`` are the same number, so they must produce the same
    reference; ``0`` and ``-0`` are the same number too.
    """

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("a reference requires a finite Decimal")
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _framed(tag: str, text: str) -> bytes:
    """One length-framed, typed token; no payload can span two fields."""

    payload = text.encode("utf-8")
    return f"{tag}:{len(payload)}:".encode("ascii") + payload


def _encode(value: object) -> bytes:
    """Canonically encode one typed value, injectively.

    Every token carries its type tag and its byte length, and every sequence
    carries its element count, so distinct structures always encode to distinct
    bytes -- whatever separators the underlying strings happen to contain.
    """

    if value is None:
        return b"n:0:"
    if isinstance(value, bool):
        return _framed("b", "1" if value else "0")
    if isinstance(value, Enum):
        return _framed("e", f"{type(value).__name__}={value.value}")
    if isinstance(value, Decimal):
        return _framed("d", canonical_decimal(value))
    if isinstance(value, int):
        return _framed("i", str(value))
    if isinstance(value, str):
        return _framed("s", value)
    if isinstance(value, datetime):
        return _framed("t", _aware_utc(value, field="reference timestamp").isoformat())
    if isinstance(value, (tuple, list)):
        body = b"".join(_encode(item) for item in value)
        return f"q:{len(value)}:{len(body)}:".encode("ascii") + body
    raise ValueError(
        f"a reference cannot be derived from a {type(value).__name__} value"
    )


def _reference(kind: str, payload: object) -> str:
    """One versioned deterministic reference over the facts that define it."""

    digest = hashlib.sha256(
        _encode((kind, REFERENCE_VERSION, payload))
    ).hexdigest()[:32]
    return f"{kind}_{REFERENCE_VERSION}_{digest}"


def _team_facts(team: TeamEvidence | None) -> object:
    if team is None:
        return None
    return (team.provider_id, team.canonical_id, team.name, team.abbreviation)


def _athlete_facts(athlete: AthleteEvidence | None) -> object:
    if athlete is None:
        return None
    return (
        athlete.provider_id,
        athlete.canonical_id,
        athlete.name,
        _team_facts(athlete.team),
    )


def _event_facts(event: EventEvidence | None) -> object:
    """Every normalized fact an event carries, including its canonical claim."""

    if event is None:
        return None
    return (
        event.provider_id,
        event.canonical_id,
        event.label,
        event.starts_at,
        event.ends_at,
        event.updated_at,
        event.status_label,
        _team_facts(event.home_team),
        _team_facts(event.away_team),
    )


def _statistic_facts(statistic: StatisticEvidence | None) -> object:
    if statistic is None:
        return None
    return (
        statistic.provider_id,
        statistic.canonical_id,
        statistic.label,
        tuple(statistic.components),
    )


def _named_facts(evidence: LeagueEvidence | SportEvidence | None) -> object:
    if evidence is None:
        return None
    return (evidence.provider_id, evidence.canonical_id, evidence.label)


def _competition_facts(competition: CompetitionEvidence | None) -> object:
    if competition is None:
        return None
    return (
        competition.provider_id,
        competition.canonical_id,
        competition.label,
        _named_facts(competition.sport),
    )


def _appearance_facts(appearance: AppearanceEvidence | None) -> object:
    if appearance is None:
        return None
    return (appearance.provider_id, appearance.appearance_type, appearance.label)


def _threshold_facts(threshold: MarketThreshold | None) -> object:
    if threshold is None:
        return None
    return (threshold.value, threshold.unit, threshold.original_value)


def _modifier_facts(modifier: SelectionModifier) -> object:
    return (modifier.value, modifier.kind, modifier.scope, modifier.label)


def _selection_facts(selection: Selection) -> object:
    """Every normalized fact that defines one offered selection."""

    return (
        selection.selection_id,
        selection.label,
        selection.direction,
        selection.direction_label,
        selection.status,
        tuple(_modifier_facts(modifier) for modifier in selection.modifiers),
        selection.american_price,
        selection.decimal_price,
    )


def market_reference(market: PlayerProjectionMarket) -> str:
    """The versioned reference for one normalized market.

    A provider market ID is the market's own source identity, so it defines the
    reference by itself.  A provider that publishes no market ID names the
    market by every fact it did report -- the athlete, the complete event
    evidence including its teams and times, the statistic, threshold, variant,
    scoring period, source labels, and the offered selections with their
    modifiers and prices -- and those define the reference instead, so two
    legitimately distinct offerings never collapse into one.  Availability is
    deliberately absent from both: a market that is suspended and then
    available again is the same market.
    """

    if market.market_id is not None:
        return _reference("mkt", ("source_identity", market.provider, market.market_id))
    return _reference(
        "mkt",
        (
            "reported_evidence",
            market.provider,
            _athlete_facts(market.athlete),
            _event_facts(market.event),
            _team_facts(market.team),
            _team_facts(market.opponent),
            _named_facts(market.league),
            _competition_facts(market.competition),
            _named_facts(market.sport),
            _statistic_facts(market.statistic),
            _threshold_facts(market.threshold),
            market.variant,
            market.variant_label,
            market.scoring_period,
            market.scoring_period_label,
            market.starts_at,
            market.updated_at,
            _appearance_facts(market.appearance),
            tuple(_selection_facts(selection) for selection in market.selections),
        ),
    )


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


@dataclass(frozen=True, slots=True)
class BoardMarket:
    """One retained normalized market, and where it went on this board.

    Exactly one of ``comparison_reference`` and ``exclusion`` is set, so a
    market is either part of a stated comparison or visibly, auditably not.
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
        object.__setattr__(self, "selections", tuple(self.selections))

    @property
    def is_compared(self) -> bool:
        return self.comparison_reference is not None

    @property
    def order(self) -> tuple[str, str]:
        return (self.provider, self.market_reference)


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
    snapshot_status: str | None = None
    future_observation: bool = False

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
    markets: tuple[BoardMarket, ...] = ()
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
        markets = tuple(self.markets)
        if any(not isinstance(market, BoardMarket) for market in markets):
            raise ValueError("retained markets must be BoardMarket values")
        if tuple(sorted(markets, key=lambda market: market.order)) != markets:
            raise ValueError("retained markets must be deterministically ordered")
        if markets and len(markets) != self.market_count:
            raise ValueError("a board must retain every market it counted")
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

    @property
    def markets_by_reference(self) -> dict[str, BoardMarket]:
        """Every retained market, indexed by the reference its members cite."""

        return {market.market_reference: market for market in self.markets}

    def markets_for(self, reference: str) -> tuple[BoardMarket, ...]:
        """The retained markets one comparison reference was built from."""

        return tuple(
            market for market in self.markets if market.comparison_reference == reference
        )


__all__ = [
    "REFERENCE_VERSION",
    "SUPPORTED_NARROWING_FILTERS",
    "BoardAppearance",
    "BoardAthlete",
    "BoardCompetition",
    "BoardEvent",
    "BoardMarket",
    "BoardModifier",
    "BoardNamedEvidence",
    "BoardObservation",
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
    "ProviderReport",
    "UnresolvedMarket",
    "canonical_decimal",
    "exact_seconds",
    "market_reference",
    "selection_reference",
]
