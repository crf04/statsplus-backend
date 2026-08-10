"""Typed parsing of one DFS Board request into the central board query.

The board itself narrows nothing by label: every filter it accepts names an
exact identity the board established, and this module is where a query string
becomes those identities or one invalid-input failure.  It knows the closed
vocabularies -- the supported providers, the market statuses, the parameter
names themselves -- and refuses everything else, so a misspelled filter is a
400 rather than a silently unfiltered board.

Nothing here touches Flask.  The parser reads any multi-value mapping, so the
route hands it ``request.args`` while a test hands it a plain ``MultiDict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.settings import RuntimeSettings
from app.dfs_catalog import DFS_PROVIDER_NAMES, DFS_PROVIDER_NAME_SET
from app.domain.comparisons import ComparisonFilters
from app.errors import InvalidInputError
from app.providers.dfs import MarketStatus, NBAMarketQuery

#: Every query parameter the board accepts.  A caller that sends anything else
#: is asking for a narrowing this board cannot perform, and is told so.
BOARD_QUERY_PARAMETERS = (
    "canonical_athlete_ids",
    "canonical_event_ids",
    "canonical_statistic_ids",
    "market_statuses",
    "providers",
    "season",
)

#: The most values one filter may name.  A board read is narrowed by a caller
#: who knows what they want; an unbounded identity list is a way to spend the
#: request deadline rather than a way to narrow.
MAX_FILTER_VALUES = 100


@dataclass(frozen=True, slots=True)
class BoardRequest:
    """One parsed board read: what to retrieve, and how to narrow it."""

    query: NBAMarketQuery
    filters: ComparisonFilters


def parse_board_request(args: Any, *, settings: RuntimeSettings) -> BoardRequest:
    """Parse one request's query string into a typed board read.

    ``args`` is any mapping exposing ``getlist``; every unrecognized parameter,
    unsupported vocabulary value, and unparsable identity raises the shared
    :class:`~app.errors.InvalidInputError` contract.
    """

    unknown = sorted(set(args.keys()) - set(BOARD_QUERY_PARAMETERS))
    if unknown:
        raise InvalidInputError(
            "Unsupported query parameters: "
            + ", ".join(unknown)
            + ". Supported filters: "
            + ", ".join(BOARD_QUERY_PARAMETERS)
            + ".",
            detail=f"unsupported board query parameters: {unknown}",
        )

    season = _single(args, "season") or settings.nba.current_season
    try:
        query = NBAMarketQuery(season=season)
    except ValueError as error:
        raise InvalidInputError(
            "season must be a canonical NBA season such as 2024-25.",
            detail=error,
        ) from error

    filters = ComparisonFilters(
        providers=tuple(
            _provider(value) for value in _values(args, "providers")
        ),
        canonical_athlete_ids=tuple(
            _athlete_id(value) for value in _values(args, "canonical_athlete_ids")
        ),
        canonical_event_ids=tuple(
            _identity(value, parameter="canonical_event_ids")
            for value in _values(args, "canonical_event_ids")
        ),
        canonical_statistic_ids=tuple(
            _identity(value, parameter="canonical_statistic_ids")
            for value in _values(args, "canonical_statistic_ids")
        ),
        market_statuses=tuple(
            _market_status(value) for value in _values(args, "market_statuses")
        ),
    )
    return BoardRequest(query=query, filters=filters)


def _values(args: Any, parameter: str) -> tuple[str, ...]:
    """Every value given for one filter, repeated or comma-separated."""

    values = tuple(
        item
        for raw in args.getlist(parameter)
        for item in str(raw).split(",")
        if item.strip()
    )
    if len(values) > MAX_FILTER_VALUES:
        raise InvalidInputError(
            f"{parameter} accepts at most {MAX_FILTER_VALUES} values.",
            detail=f"{parameter} received {len(values)} values",
        )
    return values


def _single(args: Any, parameter: str) -> str | None:
    """One value for a parameter that names a single fact, or none."""

    values = args.getlist(parameter)
    if not values:
        return None
    if len(values) > 1:
        raise InvalidInputError(
            f"{parameter} accepts a single value.",
            detail=f"{parameter} was given {len(values)} times",
        )
    value = str(values[0]).strip()
    return value or None


def _provider(value: str) -> str:
    name = value.strip().casefold()
    if name not in DFS_PROVIDER_NAME_SET:
        raise InvalidInputError(
            "providers accepts only: " + ", ".join(sorted(DFS_PROVIDER_NAMES)) + ".",
            detail=f"unsupported provider filter: {name!r}",
        )
    return name


def _athlete_id(value: str) -> int:
    text = value.strip()
    try:
        return int(text)
    except ValueError as error:
        raise InvalidInputError(
            "canonical_athlete_ids accepts NBA player IDs as integers.",
            detail=f"unparsable canonical athlete id: {text!r}",
        ) from error


def _identity(value: str, *, parameter: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidInputError(
            f"{parameter} values must be non-empty canonical identities.",
            detail=f"empty {parameter} value",
        )
    return text


def _market_status(value: str) -> MarketStatus:
    text = value.strip().casefold()
    try:
        return MarketStatus(text)
    except ValueError as error:
        raise InvalidInputError(
            "market_statuses accepts only: "
            + ", ".join(status.value for status in MarketStatus)
            + ".",
            detail=f"unsupported market status filter: {text!r}",
        ) from error


__all__ = [
    "BOARD_QUERY_PARAMETERS",
    "MAX_FILTER_VALUES",
    "BoardRequest",
    "parse_board_request",
]
