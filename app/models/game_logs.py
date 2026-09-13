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

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.domain.nba_teams import NBA_TEAM_TRICODES
from app.models.catalogs import (
    SUPPORTED_SELF_FILTER_STATS,
    TEAM_FILTER_ALIASES,
    SUPPORTED_TEAM_FILTERS,
)

Location = Literal["Home", "Away", "Both"]


class GameLogFilterError(ValueError):
    """One validation failure that names the filter a caller can act on.

    Raised by :class:`GameLogQuery` validators instead of a bare
    :class:`ValueError` when the facts a caller needs -- the parameter that
    failed and the submitted values that were unusable -- are safe to publish.
    The message still stays the generic one; only these facts and, where the
    service owns the accepted vocabulary, the canonical supported values are
    read into the error payload's ``details``. The route layer builds them
    with the same redaction the diagnostics get.
    """

    def __init__(
        self,
        parameter: str,
        values: tuple[Any, ...],
        message: str,
        *,
        supported_values: tuple[str, ...] | None = None,
        supported_aliases: tuple[str, ...] | None = None,
        context: str | None = None,
    ) -> None:
        super().__init__(message)
        self.parameter = parameter
        self.values = tuple(str(value) for value in values)
        self.supported_values = supported_values
        self.supported_aliases = supported_aliases
        # The complete submitted value a published value was extracted
        # from, when the model split it (a ``min,max`` range or part).
        # Splitting can destroy the context a credential pattern needs,
        # so the route sanitizes this whole text and publishes it instead
        # of a leaky fragment.
        self.context = context


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

    def _describe(self) -> str:
        """The HTTP parameter name ``self_filters[STAT]`` for this filter."""

        return f"self_filters[{self.stat}]"

    @field_validator("stat", mode="before")
    @classmethod
    def normalize_stat(cls, value: Any) -> str:
        stat = str(value).strip().upper()
        if not stat:
            raise GameLogFilterError(
                "self_filters[]",
                (value,),
                "self_filter stat must not be empty",
            )
        if stat not in SUPPORTED_SELF_FILTER_STATS:
            raise GameLogFilterError(
                f"self_filters[{stat}]",
                (value,),
                f"self_filter contains unsupported stat {stat!r}. Supported stats are: "
                + ", ".join(SUPPORTED_SELF_FILTER_STATS),
            )
        return stat

    @field_validator("value", "value2", mode="before")
    @classmethod
    def normalize_numeric_value(cls, value: Any, info: ValidationInfo) -> float | None:
        if value is None:
            return None
        # When the stat itself was already rejected it is not in the
        # validated data; the parent self_filters validator then re-labels
        # these facts under the actual submitted stat.
        stat = info.data.get("stat")
        parameter = f"self_filters[{stat}]" if stat else "self_filters"
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise GameLogFilterError(
                parameter,
                (value,),
                "self_filter values must be numbers",
            ) from error
        if not isfinite(number):
            raise GameLogFilterError(
                parameter,
                (value,),
                "self_filter values must be finite numbers",
            )
        return number

    @model_validator(mode="after")
    def validate_operator_values(self) -> "SelfFilter":
        try:
            self.operator.validate_values(self.value, self.value2)
        except ValueError as error:
            # A rejected bound is a submitted value even when it is zero,
            # so report every submitted operand, not only the truthy ones.
            submitted = (self.value, self.value2)
            raise GameLogFilterError(
                self._describe(),
                tuple(
                    _format_rating(operand)
                    for operand in submitted
                    if operand is not None
                ),
                str(error),
            ) from error
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
        # An empty stat is still a submitted key value (``self_filters[]``);
        # only a missing stat (typed, non-HTTP calls) stays unnamed.
        raise GameLogFilterError(
            f"self_filters[{stat}]" if stat is not None else "self_filters",
            (str(raw),),
            f"self_filter for {stat!r} must be a range or typed comparison",
        )
    if len(parts) != 2:
        raise GameLogFilterError(
            f"self_filters[{stat}]" if stat is not None else "self_filters",
            (str(raw),),
            f"self_filter for {stat!r} must contain min,max values",
        )
    return SelfFilter(stat=stat, operator="between", value=parts[0], value2=parts[1])


def _relabel_self_filter_failure(
    stat: Any,
    raw: Any,
    error: ValidationError,
) -> GameLogFilterError | ValidationError:
    """Re-label one nested self-filter rejection under the actual stat.

    When the stat itself is rejected, pydantic can no longer attribute the
    stat's range failures to it -- the validated fields no longer carry the
    rejected stat name. Re-labeling here keeps every fact under the
    parameter the caller actually submitted, ``self_filters[STAT]``,
    without leaking the nested rejection wholesale.
    """

    rejected_values = []
    for nested in error.errors():
        cause = (nested.get("ctx") or {}).get("error")
        if (
            isinstance(cause, GameLogFilterError)
            and nested.get("loc")
            and nested["loc"][-1] != "stat"
        ):
            rejected_values.extend(cause.values)
    if not rejected_values:
        return error
    # An empty stat is still a submitted key value (``self_filters[]``);
    # only a missing stat (typed, non-HTTP calls) stays unnamed.
    label = f"self_filters[{stat}]" if stat is not None else "self_filters"
    return GameLogFilterError(
        label,
        tuple(rejected_values),
        f"self_filter {raw!r} was rejected: {error}",
        # The range the caller submitted: the fragments named above came
        # from splitting it, so credential context survives the split.
        context=raw if isinstance(raw, str) else None,
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
    opponent_tricode: str | None = None
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
            raise GameLogFilterError(
                "season_filter",
                (value,),
                "season_filter must be a string in YYYY-YY format",
            )
        season = value.strip()
        match = re.fullmatch(r"([0-9]{4})-([0-9]{2})", season)
        if match is None:
            raise GameLogFilterError(
                "season_filter",
                (value,),
                "season_filter must use YYYY-YY format",
            )
        start_year = int(match.group(1))
        expected_suffix = f"{(start_year + 1) % 100:02d}"
        if match.group(2) != expected_suffix:
            raise GameLogFilterError(
                "season_filter",
                (value,),
                "season_filter must end with the following calendar year's "
                "final two digits",
            )
        return season

    @field_validator("minutes_filter", mode="before")
    @classmethod
    def normalize_minutes(cls, value: Any) -> tuple[int, int]:
        if value is None:
            return (0, 48)
        if isinstance(value, tuple):
            return value
        complete: str | None = None
        if isinstance(value, str):
            complete = value
            parts = value.split(",")
            if len(parts) != 2:
                raise GameLogFilterError(
                    "minutes_filter",
                    (value,),
                    "minutes_filter must contain two integer values",
                )
            value = tuple(parts)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise GameLogFilterError(
                "minutes_filter",
                (str(value),),
                "minutes_filter must contain min,max minutes",
            )
        # Name the offending part, not the whole pair: the working bound
        # stays untouched while the unusable one identifies itself. The
        # complete submitted value travels along, so even a part cut out
        # of a credential can be sanitized in context.
        try:
            low = int(value[0])
        except (TypeError, ValueError) as error:
            raise GameLogFilterError(
                "minutes_filter",
                (value[0],),
                "minutes_filter values must be integers",
                context=complete,
            ) from error
        try:
            high = int(value[1])
        except (TypeError, ValueError) as error:
            raise GameLogFilterError(
                "minutes_filter",
                (value[1],),
                "minutes_filter values must be integers",
                context=complete,
            ) from error
        return (low, high)

    @field_validator("rank_filter", mode="before")
    @classmethod
    def normalize_rank_filter(cls, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, (int, str)):
            value = [value]
        # Like teams_against, report every unusable entry and leave the
        # parseable ranks out of the details.
        ranks: list[int] = []
        invalid: list[Any] = []
        for entry in value:
            try:
                ranks.append(int(entry))
            except (TypeError, ValueError):
                invalid.append(entry)
        if invalid:
            raise GameLogFilterError(
                "rank_filter",
                tuple(invalid),
                f"rank_filter contains invalid entries: {invalid!r}",
            )
        return ranks

    @field_validator("opponent_tricode", mode="before")
    @classmethod
    def normalize_opponent_tricode(cls, value: Any) -> str | None:
        """Require one canonical NBA tricode naming a single opponent.

        This is a filter on the opponent recorded against each game log, not
        a ranking, so it is validated against the closed tricode catalog
        rather than the rank-able Team Filters. The catalog is the 30 canonical
        tricodes, so provider dialects such as ``PHO`` and ``GS`` are refused
        rather than translated.
        """

        if value is None:
            return None
        tricode = value.strip().upper()
        if tricode not in NBA_TEAM_TRICODES:
            raise GameLogFilterError(
                "opponent_tricode",
                (value,),
                f"opponent_tricode {value!r} is not an NBA team tricode",
            )
        return tricode

    @field_validator("playstyle_range", mode="before")
    @classmethod
    def normalize_playstyle_range(cls, value: Any) -> tuple[float, float]:
        if value is None:
            return (0.0, 200.0)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("playstyle_range must contain min,max ratings")
        # The two bounds are separate query parameters, so each failure
        # carries the parameter it was submitted as.
        try:
            low = float(value[0])
        except (TypeError, ValueError) as error:
            raise GameLogFilterError(
                "playstyle_RTG_min",
                (value[0],),
                "playstyle_RTG_min must be a finite number",
            ) from error
        try:
            high = float(value[1])
        except (TypeError, ValueError) as error:
            raise GameLogFilterError(
                "playstyle_RTG_max",
                (value[1],),
                "playstyle_RTG_max must be a finite number",
            ) from error
        if not isfinite(low):
            raise GameLogFilterError(
                "playstyle_RTG_min",
                (value[0],),
                "playstyle_RTG_min must be a finite number",
            )
        if not isfinite(high):
            raise GameLogFilterError(
                "playstyle_RTG_max",
                (value[1],),
                "playstyle_RTG_max must be a finite number",
            )
        return (low, high)

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
            # Only internally reached (the route always sends ordered
            # pairs); it stays an unknown-shape rejection, so the generic
            # message with no details remains the contract here.
            raise ValueError(
                "self_filters must be a stat range mapping or a list of typed filters"
            )
        normalized: list[SelfFilter] = []
        for stat, raw in entries:
            try:
                entry = _normalize_self_filter_entry(stat, raw)
            except ValidationError as error:
                raise _relabel_self_filter_failure(stat, raw, error) from error
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
            raise GameLogFilterError(
                "teams_against",
                tuple(unsupported),
                f"teams_against contains unsupported filters: {unsupported}. "
                "Supported filters are: "
                + ", ".join(SUPPORTED_TEAM_FILTERS),
                # The canonical vocabulary travels with the refusal, so a
                # caller never needs a copy of these constants to recover.
                supported_values=tuple(SUPPORTED_TEAM_FILTERS),
                supported_aliases=tuple(TEAM_FILTER_ALIASES),
            )
        return value

    @model_validator(mode="after")
    def _check_rank_alignment(self) -> "GameLogQuery":
        if self.teams_against and len(self.teams_against) != len(self.rank_filter):
            raise GameLogFilterError(
                "rank_filter",
                tuple(self.rank_filter),
                "rank_filter must contain one rank per teams_against filter",
            )
        if self.minutes_filter[0] > self.minutes_filter[1]:
            raise GameLogFilterError(
                "minutes_filter",
                (f"{self.minutes_filter[0]},{self.minutes_filter[1]}",),
                "minutes_filter min must not exceed minutes_filter max",
            )
        if self.playstyle_range[0] > self.playstyle_range[1]:
            raise GameLogFilterError(
                "playstyle_RTG_range",
                (_format_rating(self.playstyle_range[0]), _format_rating(self.playstyle_range[1])),
                "playstyle_RTG_min must not exceed playstyle_RTG_max",
            )
        return self


def _format_rating(raw: float) -> str:
    """One submitted rating bound, spelled the way decimals read."""

    if raw.is_integer():
        return str(int(raw))
    return str(raw)


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
    "GameLogFilterError",
    "GameLogQuery",
    "GameLogResponse",
    "Location",
    "SelfFilter",
    "SelfFilterOperator",
    "SUPPORTED_TEAM_FILTERS",
]
