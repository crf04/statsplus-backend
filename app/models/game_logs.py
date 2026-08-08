"""Typed query and response models for the game-log endpoint.

The game-log endpoint used to pass an implicit, unvalidated filter dictionary
between the route and the service and returned pandas JSON strings nested
inside the response JSON.  These models replace that implicit contract:

* :class:`GameLogQuery` is the single typed filter interface accepted by
  :meth:`GameService.get_filtered_logs` and the filtering pipeline.
* :class:`SelfFilter` is the canonical comparison model.  Query-string
  ``STAT=min,max`` input is normalized to ``between``; natural-language
  ``gte``, ``gt``, ``lt``, ``lte``, and ``eq`` comparisons retain their exact
  operator semantics.
* :class:`GameLogResponse` describes the top-level response contract, where
  the logs and averages fields are ordinary JSON arrays (fresh records) rather
  than strings produced by ``DataFrame.to_json``.

Filters that cannot be validated raise pydantic :class:`ValidationError``s,
which routes translate into a clear ``invalid_input`` client error.
"""

from __future__ import annotations

from datetime import date
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.catalogs import (
    SUPPORTED_SELF_FILTER_STATS,
    TEAM_FILTER_ALIASES,
    SUPPORTED_TEAM_FILTERS,
)

Location = Literal["Home", "Away", "Both"]
SelfFilterOperator = Literal["gte", "gt", "lt", "lte", "eq", "between"]


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
        if self.operator == "between":
            if self.value2 is None:
                raise ValueError("between self_filters require value2")
            if self.value > self.value2:
                raise ValueError("between self_filter value must not exceed value2")
        elif self.value2 is not None:
            raise ValueError(
                f"{self.operator} self_filters accept one value, not value2"
            )
        return self

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
    self_filters: dict[str, SelfFilter] = Field(default_factory=dict)

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
        if isinstance(value, tuple):
            return value
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("playstyle_range must contain min,max ratings")
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError) as error:
            raise ValueError("playstyle_range values must be numbers") from error

    @field_validator("self_filters", mode="before")
    @classmethod
    def normalize_self_filters(cls, value: Any) -> dict[str, SelfFilter]:
        if value is None:
            return {}
        normalized: dict[str, SelfFilter] = {}
        if isinstance(value, Mapping):
            entries = value.items()
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            entries = ((None, entry) for entry in value)
        else:
            raise ValueError(
                "self_filters must be a stat range mapping or a list of typed filters"
            )
        for stat, raw in entries:
            entry = _normalize_self_filter_entry(stat, raw)
            normalized[entry.stat] = entry
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
