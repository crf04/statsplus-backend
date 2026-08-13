"""Closed, centrally defined domain constants for provider-driven filters.

The NBA play types, opponent stat filters, and PBP data types used to be
repeated as free-form string literals across services and route models, which
let them drift one-of-many places at a time.  They live here once, as closed
sets, so model validation, the filtering pipeline, refresh jobs, and the
documentation cannot disagree about what a supported value is.
"""

from __future__ import annotations

from typing import Literal

#: Canonical Synergy play types; order matches the published research tables.
PLAY_TYPES: tuple[str, ...] = (
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

#: Shooting-type categories used by the "shooting type" filters and tables.
SHOOTING_TYPES: tuple[str, ...] = (
    "Catch and Shoot",
    "Pullups",
    "Less Than 10 ft",
)
LESS_THAN_TEN_FEET_FILTER = SHOOTING_TYPES[2]

# Values accepted from older clients.  The service normalizes these to the
# canonical catalog value before selecting a table or provider category.
TEAM_FILTER_ALIASES: dict[str, str] = {
    "<10 Ft": "Less Than 10 ft",
    "<10 ft": "Less Than 10 ft",
    "Less Than 10 Ft": "Less Than 10 ft",
    "Less than 10 ft": "Less Than 10 ft",
}

#: PBP totals data kinds the adapter accepts.
PBPDataKind = Literal["player", "opponent"]
PBP_DATA_KINDS: tuple[str, ...] = ("player", "opponent")

#: Opponent rank-able categories sourced from the league dash "Opponent" sets.
OPPONENT_FILTERS: tuple[str, ...] = (
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
)

#: Catch-and-shoot and pull-up opponent categories.
CATCH_SHOOT_FILTERS: tuple[str, ...] = ("C&S 3s", "C&S PTS", "C&S 3A")
PULLUP_FILTERS: tuple[str, ...] = ("PU 2s", "PU 3s", "PU PTS")

#: Processed assist-location categories used by the assist filter tables.
ASSIST_LOCATION_FILTERS: tuple[str, ...] = (
    "TwoPtAssists",
    "ThreePtAssists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)

#: Every rank-able team("teams_against") filter the game-log endpoint accepts.
SUPPORTED_TEAM_FILTERS: tuple[str, ...] = (
    *OPPONENT_FILTERS,
    *CATCH_SHOOT_FILTERS,
    *PULLUP_FILTERS,
    LESS_THAN_TEN_FEET_FILTER,
    *PLAY_TYPES,
    *ASSIST_LOCATION_FILTERS,
)

#: Stat columns that may be filtered by the player-self filters.  The service
# computes the derived columns before applying these ranges.
SUPPORTED_SELF_FILTER_STATS: tuple[str, ...] = (
    "MIN",
    "PTS",
    "REB",
    "AST",
    "STL",
    "BLK",
    "TOV",
    "FGA",
    "FG3A",
    "FTA",
    "FGM",
    "FG3M",
    "FTM",
    "FG_PCT",
    "OREB",
    "DREB",
    "PF",
    "PRA",
    "PA",
    "PR",
    "RA",
    "STKS",
    "FD_PTS",
)


__all__ = [
    "ASSIST_LOCATION_FILTERS",
    "CATCH_SHOOT_FILTERS",
    "OPPONENT_FILTERS",
    "PBPDataKind",
    "PBP_DATA_KINDS",
    "PLAY_TYPES",
    "PULLUP_FILTERS",
    "SHOOTING_TYPES",
    "LESS_THAN_TEN_FEET_FILTER",
    "TEAM_FILTER_ALIASES",
    "SUPPORTED_SELF_FILTER_STATS",
    "SUPPORTED_TEAM_FILTERS",
]
