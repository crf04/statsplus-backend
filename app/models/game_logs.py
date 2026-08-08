"""Typed query and response models for the game-log endpoint.

The game-log endpoint used to pass an implicit, unvalidated filter dictionary
between the route and the service and returned pandas JSON strings nested
inside the response JSON.  These models replace that implicit contract:

* :class:`GameLogQuery` is the single typed filter interface accepted by
  :meth:`GameService.get_filtered_logs` and the filtering pipeline.
* :class:`GameLogResponse` describes the top-level response contract, where
  the logs and averages fields are ordinary JSON arrays (fresh records) rather
  than strings produced by ``DataFrame.to_json``.

Filters that cannot be validated raise pydantic :class:`ValidationError``s,
which routes translate into a clear ``invalid_input`` client error.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Location = Literal["Home", "Away", "Both"]

# Supported opponent-filter names defined here once so filters and docs stay
# in sync.  The service still interprets the full set of rank-able categories.
SUPPORTED_TEAM_FILTERS = (
    "OPP_PTS",
    "OPP_REB",
    "OPP_AST",
    "OPP_STOCKS",
    "OPP_FTA",
    "OPP_TOV",
    "OPP_BLK",
    "OPP_STL",
    "OPP_FG3M",
    "OPP_FG3A",
    "C&S 3s",
    "C&S PTS",
    "C&S 3A",
    "PU 2s",
    "PU 3s",
    "PU PTS",
    "Transition",
    "Isolation",
    "PRBallHandler",
    "PRRollMan",
    "OffRebound",
    "Spotup",
    "Cut",
    "Handoff",
    "OffScreen",
    "Misc",
    "Postup",
)


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
    self_filters: dict[str, tuple[float, float]] = Field(default_factory=dict)

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
    def normalize_self_filters(cls, value: Any) -> dict[str, tuple[float, float]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("self_filters must be a stat -> min,max mapping")
        normalized: dict[str, tuple[float, float]] = {}
        for stat, bounds in value.items():
            if isinstance(bounds, str):
                bounds = bounds.split(",")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise ValueError(
                    f"self_filter for {stat!r} must contain min,max values"
                )
            normalized[str(stat)] = (float(bounds[0]), float(bounds[1]))
        return normalized

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
    arrays; ``next_game`` is the full name of the player's next opponent, or
    ``null`` when it cannot be determined.
    """

    game_logs: list[dict[str, Any]]
    averages: list[dict[str, Any]]
    season_averages: list[dict[str, Any]]
    next_game: str | None = None


__all__ = [
    "GameLogQuery",
    "GameLogResponse",
    "Location",
    "SUPPORTED_TEAM_FILTERS",
]