"""Typed, immutable statistic matching values shared by providers and services.

This module deliberately has no provider or catalog imports.  It owns the
closed vocabularies a statistic match is built from — scoring periods, units,
states, and unmapped reasons — so providers can carry a resolved match without
introducing an import cycle; the catalog remains the sole authority that
creates canonical matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ScoringPeriod(str, Enum):
    """Reviewed scoring periods; unknown provider labels remain evidence."""

    FULL_GAME = "full_game"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    FIRST_QUARTER = "first_quarter"
    SECOND_QUARTER = "second_quarter"
    UNKNOWN = "unknown"


class StatisticUnit(str, Enum):
    """Closed unit vocabulary reviewed for catalog definitions."""

    COUNT = "count"
    DECIMAL = "decimal"
    MINUTES = "minutes"
    PERCENTAGE = "percentage"
    POINTS = "points"


class MatchState(str, Enum):
    """Closed state vocabulary for statistic resolution."""

    CANONICAL = "canonical"
    UNMAPPED = "unmapped"


class MatchReason(str, Enum):
    """Closed vocabulary explaining why a match is unmapped."""

    MISSING_STATISTIC_EVIDENCE = "missing_statistic_evidence"
    MISSING_STATISTIC_LABEL = "missing_statistic_label"
    UNKNOWN_SCORING_PERIOD = "unknown_scoring_period"
    UNKNOWN_PROVIDER_LABEL = "unknown_provider_label"
    UNSUPPORTED_SCORING_PERIOD = "unsupported_scoring_period"
    UNIT_MISMATCH = "unit_mismatch"
    COMPONENT_MISMATCH = "component_mismatch"


@runtime_checkable
class CanonicalStatisticLike(Protocol):
    id: str
    components: tuple[str, ...]
    comparable: bool


@runtime_checkable
class StatisticEvidenceLike(Protocol):
    provider_id: str | None
    canonical_id: str | None
    label: str | None
    components: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class StatisticMatch:
    """Immutable typed result of resolving provider statistic evidence."""

    state: MatchState
    evidence: StatisticEvidenceLike
    scoring_period: ScoringPeriod
    canonical: CanonicalStatisticLike | None = None
    provider: str | None = None
    unit: StatisticUnit | None = None
    reason: MatchReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, MatchState):
            raise TypeError("statistic match state must be a MatchState")
        if not isinstance(self.evidence, StatisticEvidenceLike):
            raise TypeError("statistic match evidence must be StatisticEvidence")
        if not isinstance(self.scoring_period, ScoringPeriod):
            raise TypeError("statistic match scoring_period must be a ScoringPeriod")
        if self.unit is not None and not isinstance(self.unit, StatisticUnit):
            raise TypeError("statistic match unit must be a StatisticUnit or None")
        if self.reason is not None and not isinstance(self.reason, MatchReason):
            raise TypeError("statistic match reason must be a MatchReason or None")
        if self.state is MatchState.CANONICAL:
            if not isinstance(self.canonical, CanonicalStatisticLike):
                raise ValueError("canonical statistic matches require a canonical statistic")
            if self.scoring_period is ScoringPeriod.UNKNOWN:
                raise ValueError(
                    "canonical statistic matches require an explicit scoring period"
                )
            if self.reason is not None:
                raise ValueError("canonical statistic matches cannot carry a reason")
        else:
            if self.canonical is not None:
                raise ValueError("unmapped statistic matches cannot carry a canonical statistic")
            if self.reason is None:
                raise ValueError("unmapped statistic matches require a reason")
        provider = self.provider
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            raise ValueError("statistic match provider must be a non-empty string or None")
        object.__setattr__(
            self, "provider", provider.strip().casefold() if provider else None
        )

    @property
    def canonical_id(self) -> str | None:
        return self.canonical.id if self.canonical is not None else None

    @property
    def is_comparable(self) -> bool:
        return self.state is MatchState.CANONICAL and bool(
            self.canonical is not None and self.canonical.comparable
        )


__all__ = [
    "CanonicalStatisticLike",
    "MatchReason",
    "MatchState",
    "ScoringPeriod",
    "StatisticEvidenceLike",
    "StatisticMatch",
    "StatisticUnit",
]
