"""Typed, immutable statistic matching values shared by providers and services.

This module deliberately has no provider or catalog imports.  It owns the
closed vocabularies a statistic match is built from — scoring periods, units,
states, and unmapped reasons — so providers can carry a resolved match without
introducing an import cycle; the catalog remains the sole authority that
creates canonical matches.
"""

from __future__ import annotations

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


class StatisticMatch:
    """Immutable typed result of resolving provider statistic evidence."""

    __slots__ = (
        "state", "evidence", "canonical", "provider", "scoring_period",
        "unit", "reason", "_sealed",
    )

    def __init__(
        self,
        *,
        state: MatchState,
        evidence: StatisticEvidenceLike,
        canonical: CanonicalStatisticLike | None = None,
        provider: str | None = None,
        scoring_period: ScoringPeriod,
        unit: StatisticUnit | None = None,
        reason: MatchReason | None = None,
    ) -> None:
        if not isinstance(state, MatchState):
            raise TypeError("statistic match state must be a MatchState")
        if not isinstance(evidence, StatisticEvidenceLike):
            raise TypeError("statistic match evidence must be StatisticEvidence")
        if not isinstance(scoring_period, ScoringPeriod):
            raise TypeError("statistic match scoring_period must be a ScoringPeriod")
        if unit is not None and not isinstance(unit, StatisticUnit):
            raise TypeError("statistic match unit must be a StatisticUnit or None")
        if reason is not None and not isinstance(reason, MatchReason):
            raise TypeError("statistic match reason must be a MatchReason or None")
        if state is MatchState.CANONICAL:
            if not isinstance(canonical, CanonicalStatisticLike):
                raise ValueError("canonical statistic matches require a canonical statistic")
            if scoring_period is ScoringPeriod.UNKNOWN:
                raise ValueError(
                    "canonical statistic matches require an explicit scoring period"
                )
            if reason is not None:
                raise ValueError("canonical statistic matches cannot carry a reason")
        else:
            if canonical is not None:
                raise ValueError("unmapped statistic matches cannot carry a canonical statistic")
            if reason is None:
                raise ValueError("unmapped statistic matches require a reason")
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            raise ValueError("statistic match provider must be a non-empty string or None")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "canonical", canonical)
        object.__setattr__(self, "provider", provider.strip().casefold() if provider else None)
        object.__setattr__(self, "scoring_period", scoring_period)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("StatisticMatch is immutable")
        object.__setattr__(self, name, value)

    @property
    def match_state(self) -> MatchState:
        return self.state

    @property
    def canonical_id(self) -> str | None:
        return self.canonical.id if self.canonical is not None else None

    @property
    def canonical_statistic(self) -> CanonicalStatisticLike | None:
        return self.canonical

    @property
    def statistic(self) -> CanonicalStatisticLike | None:
        return self.canonical

    @property
    def status(self) -> MatchState:
        return self.state

    @property
    def provider_evidence(self) -> StatisticEvidenceLike:
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


__all__ = [
    "CanonicalStatisticLike",
    "MatchReason",
    "MatchState",
    "ScoringPeriod",
    "StatisticEvidenceLike",
    "StatisticMatch",
    "StatisticUnit",
]
