"""Typed query and response models for the game-log endpoint.

The game-log endpoint used to pass an implicit, unvalidated filter dictionary
between the route and the service and returned pandas JSON strings nested
inside the response JSON.  These models replace that implicit contract:

* :class:`GameLogQuery` is the single typed filter interface accepted by
  :meth:`GameService.get_filtered_logs` and the filtering pipeline.
* :class:`SelfFilter` is the canonical comparison model.  Query-string
  ``STAT=min,max`` input is normalized to ``between``; natural-language
  ``gte``, ``gt``, ``lt``, ``lte``, and ``eq`` comparisons retain their exact
  operator semantics.  The ordered list preserves multiple constraints for
  the same stat (for example, ``PTS >= 20`` and ``PTS < 30``).
* :class:`GameLogResponse` describes the top-level response contract, where
  the logs and averages fields are ordinary JSON arrays (fresh records) rather
  than strings produced by ``DataFrame.to_json``.

Filters that cannot be validated raise pydantic :class:`ValidationError``s,
which routes translate into a clear ``invalid_input`` client error.
"""

from __future__ import annotations

from datetime import date
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from operator import eq, ge, gt, le, lt
import re
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.catalogs import (
    SUPPORTED_SELF_FILTER_STATS,
    TEAM_FILTER_ALIASES,
    SUPPORTED_TEAM_FILTERS,
)

Location = Literal["Home", "Away", "Both"]


@dataclass(frozen=True, slots=True)
class _SelfFilterOperatorDescriptor:
    """Comparison behavior owned by one member of the closed operator set."""

    validate: Callable[["SelfFilterOperator", float, float | None], None]
    apply: Callable[[Any, float, float | None], Any]


class SelfFilterOperator(str, Enum):
    """The comparison operators accepted by HTTP, NLP, and game filters."""

    GTE = "gte"
    GT = "gt"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    BETWEEN = "between"

    @property
    def descriptor(self) -> _SelfFilterOperatorDescriptor:
        return _SELF_FILTER_OPERATOR_DESCRIPTORS[self]

    def validate_values(self, value: float, value2: float | None) -> None:
        """Validate the operands for this operator."""

        self.descriptor.validate(self, value, value2)

    def apply(self, values: Any, value: float, value2: float | None = None) -> Any:
        """Return a pandas-like boolean mask for the comparison."""

        self.validate_values(value, value2)
        return self.descriptor.apply(values, value, value2)


def _validate_single(
    operator: SelfFilterOperator, value: float, value2: float | None
) -> None:
    del value
    if value2 is not None:
        raise ValueError(
            f"{operator.value} self_filters accept one value, not value2"
        )


def _validate_between(
    operator: SelfFilterOperator, value: float, value2: float | None
) -> None:
    del operator
    if value2 is None:
        raise ValueError("between self_filters require value2")
    if value > value2:
        raise ValueError("between self_filter value must not exceed value2")


def _single_apply(comparison: Callable[[Any, float], Any]) -> Callable[
    [Any, float, float | None], Any
]:
    def apply(values: Any, value: float, value2: float | None) -> Any:
        del value2
        return comparison(values, value)

    return apply


def _between_apply(values: Any, value: float, value2: float | None) -> Any:
    return (values >= value) & (values <= value2)


_SELF_FILTER_OPERATOR_DESCRIPTORS: dict[SelfFilterOperator, _SelfFilterOperatorDescriptor] = {
    SelfFilterOperator.GTE: _SelfFilterOperatorDescriptor(
        validate=_validate_single, apply=_single_apply(ge)
    ),
    SelfFilterOperator.GT: _SelfFilterOperatorDescriptor(
        validate=_validate_single, apply=_single_apply(gt)
    ),
    SelfFilterOperator.LT: _SelfFilterOperatorDescriptor(
        validate=_validate_single, apply=_single_apply(lt)
    ),
    SelfFilterOperator.LTE: _SelfFilterOperatorDescriptor(
        validate=_validate_single, apply=_single_apply(le)
    ),
    SelfFilterOperator.EQ: _SelfFilterOperatorDescriptor(
        validate=_validate_single, apply=_single_apply(eq)
    ),
    SelfFilterOperator.BETWEEN: _SelfFilterOperatorDescriptor(
        validate=_validate_between, apply=_between_apply
    ),
}


class SelfFilter(BaseModel):
    """One validated comparison against a player game-log column.

    The natural-language parser historically emitted a ``SelfFilter``
    dataclass with a ``stat_column`` field, while HTTP callers supplied a
    ``stat -> min,max`` dictionary.  This model is the canonical boundary
    representation for both inputs.  ``stat_column`` remains a read-only
    compatibility property for parser/executor callers during migration.
    """

    stat: str
    operator: SelfFilterOperator
    value: float
    value2: float | None = None
    original_text: str = ""

    @property
    def stat_column(self) -> str:
        """Return the parser-era name for the canonical stat field."""

        return self.stat

    @field_validator("stat", mode="before")
    @classmethod
    def normalize_stat(cls, value: Any) -> str:
        stat = str(value).strip().upper()
        if not stat:
            raise ValueError("self_filter stat must not be empty")
        if stat not in SUPPORTED_SELF_FILTER_STATS:
            raise ValueError(
                f"self_filter contains unsupported stat {stat!r}. Supported stats are: "
                + ", ".join(SUPPORTED_SELF_FILTER_STATS)
            )
        return stat

    @field_validator("value", "value2", mode="before")
    @classmethod
    def normalize_numeric_value(cls, value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("self_filter values must be numbers") from error
        if not isfinite(number):
            raise ValueError("self_filter values must be finite numbers")
        return number

    @model_validator(mode="after")
    def validate_operator_values(self) -> "SelfFilter":
        self.operator.validate_values(self.value, self.value2)
        return self

    def apply(self, values: Any) -> Any:
        """Apply this filter to a pandas-like series."""

        return self.operator.apply(values, self.value, self.value2)

def _normalize_self_filter_entry(stat: Any, raw: Any) -> SelfFilter:
    """Convert one HTTP, NLP, or typed self-filter entry."""

    if isinstance(raw, SelfFilter):
        if stat is None or raw.stat == str(stat).strip().upper():
            return raw
        return raw.model_copy(update={"stat": stat})

    if isinstance(raw, Mapping):
        payload = dict(raw)
        payload.setdefault("stat", payload.pop("stat_column", stat))
        if payload.get("stat") is None:
            raise ValueError("self_filter entries require a stat")
        return SelfFilter(**payload)

    # NLP's legacy dataclass uses attributes rather than a mapping.
    if hasattr(raw, "stat_column") or hasattr(raw, "stat"):
        payload = {
            "stat": getattr(raw, "stat_column", None) or getattr(raw, "stat", stat),
            "operator": getattr(raw, "operator", None),
            "value": getattr(raw, "value", None),
            "value2": getattr(raw, "value2", None),
            "original_text": getattr(raw, "original_text", ""),
        }
        return SelfFilter(**payload)

    # The query-string contract remains ``STAT=min,max`` and is interpreted
    # as an inclusive between comparison.
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        parts = list(raw)
    else:
        raise ValueError(
            f"self_filter for {stat!r} must be a range or typed comparison"
        )
    if len(parts) != 2:
        raise ValueError(f"self_filter for {stat!r} must contain min,max values")
    return SelfFilter(stat=stat, operator="between", value=parts[0], value2=parts[1])


class GameLogQuery(BaseModel):
    """One typed, validated game-log filter request.

    The route layer builds this from URL query parameters; the natural
    language executor builds it from parsed components; the service and
    filtering pipeline consume only this interface.  No other shape (implicit
    dictionaries or JSON strings) is accepted.
    """

    season_filter: str
    minutes_filter: tuple[int, int] = (0, 48)
    players_on: list[str] = Field(default_factory=list)
    players_off: list[str] = Field(default_factory=list)
    date_filter: date | None = None
    teams_against: list[str] = Field(default_factory=list)
    rank_filter: list[int] = Field(default_factory=list)
    location_filter: Location = "Both"
    game_filter: int | None = Field(default=None, ge=1)
    playstyle_range: tuple[float, float] = (0.0, 200.0)
    # Keep this as a sequence rather than a stat-keyed mapping: conjunctions
    # can contain more than one constraint for the same stat.
    self_filters: list[SelfFilter] = Field(default_factory=list)

    @field_validator("season_filter", mode="before")
    @classmethod
    def normalize_season_filter(cls, value: Any) -> str:
        """Require one canonical NBA season label (for example, ``2024-25``)."""

        if not isinstance(value, str):
            raise ValueError("season_filter must be a string in YYYY-YY format")
        season = value.strip()
        match = re.fullmatch(r"([0-9]{4})-([0-9]{2})", season)
        if match is None:
            raise ValueError("season_filter must use YYYY-YY format")
        start_year = int(match.group(1))
        expected_suffix = f"{(start_year + 1) % 100:02d}"
        if match.group(2) != expected_suffix:
            raise ValueError(
                "season_filter must end with the following calendar year's "
                "final two digits"
            )
        return season

    @field_validator("minutes_filter", mode="before")
    @classmethod
    def normalize_minutes(cls, value: Any) -> tuple[int, int]:
        if value is None:
            return (0, 48)
        if isinstance(value, tuple):
            return value
        if isinstance(value, str):
            parts = value.split(",")
            if len(parts) != 2:
                raise ValueError("minutes_filter must contain two integer values")
            value = tuple(parts)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("minutes_filter must contain min,max minutes")
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError) as error:
            raise ValueError("minutes_filter values must be integers") from error

    @field_validator("rank_filter", mode="before")
    @classmethod
    def normalize_rank_filter(cls, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, (int, str)):
            value = [value]
        ranks: list[int] = []
        for entry in value:
            try:
                ranks.append(int(entry))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"rank_filter entry {entry!r} is not a valid integer"
                ) from error
        return ranks

    @field_validator("playstyle_range", mode="before")
    @classmethod
    def normalize_playstyle_range(cls, value: Any) -> tuple[float, float]:
        if value is None:
            return (0.0, 200.0)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("playstyle_range must contain min,max ratings")
        try:
            normalized = (float(value[0]), float(value[1]))
        except (TypeError, ValueError) as error:
            raise ValueError("playstyle_range values must be numbers") from error
        if not all(isfinite(number) for number in normalized):
            raise ValueError("playstyle_range values must be finite numbers")
        return normalized

    @field_validator("self_filters", mode="before")
    @classmethod
    def normalize_self_filters(cls, value: Any) -> list[SelfFilter]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            entries = value.items()
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            # The route uses ``(stat, raw_range)`` pairs so repeated query
            # parameters retain their order.  Typed mappings and legacy NLP
            # objects continue to use the single-entry form.
            entries = (
                (entry[0], entry[1])
                if isinstance(entry, (list, tuple)) and len(entry) == 2
                else (None, entry)
                for entry in value
            )
        else:
            raise ValueError(
                "self_filters must be a stat range mapping or a list of typed filters"
            )
        normalized: list[SelfFilter] = []
        for stat, raw in entries:
            entry = _normalize_self_filter_entry(stat, raw)
            normalized.append(entry)
        return normalized

    @field_validator("teams_against")
    @classmethod
    def reject_unsupported_team_filters(cls, value: list[str]) -> list[str]:
        # Normalize legacy spellings (notably ``<10 Ft``) before validation so
        # callers can migrate without changing the canonical service/table
        # value they receive back.
        value = [TEAM_FILTER_ALIASES.get(item, item) for item in value]
        unsupported = [
            item for item in value if item not in SUPPORTED_TEAM_FILTERS
        ]
        if unsupported:
            raise ValueError(
                f"teams_against contains unsupported filters: {unsupported}. "
                "Supported filters are: "
                + ", ".join(SUPPORTED_TEAM_FILTERS)
            )
        return value

    @model_validator(mode="after")
    def _check_rank_alignment(self) -> "GameLogQuery":
        if self.teams_against and len(self.teams_against) != len(self.rank_filter):
            raise ValueError(
                "rank_filter must contain one rank per teams_against filter"
            )
        if self.minutes_filter[0] > self.minutes_filter[1]:
            raise ValueError("minutes_filter min must not exceed minutes_filter max")
        if self.playstyle_range[0] > self.playstyle_range[1]:
            raise ValueError(
                "playstyle_RTG_min must not exceed playstyle_RTG_max"
            )
        return self


class GameLogResponse(BaseModel):
    """The public game-log response contract.

    ``game_logs``, ``averages``, and ``season_averages`` are ordinary JSON
    arrays; ``next_game`` remains ``null`` under the existing game-log
    contract.
    """

    game_logs: list[dict[str, Any]]
    averages: list[dict[str, Any]]
    season_averages: list[dict[str, Any]]
    next_game: str | None = None


__all__ = [
    "GameLogQuery",
    "GameLogResponse",
    "Location",
    "SelfFilter",
    "SelfFilterOperator",
    "SUPPORTED_TEAM_FILTERS",
]
