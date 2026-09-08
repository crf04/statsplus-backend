"""Provider vocabulary behind the player Zone Shooting profile.

The Player Profile's Zone Shooting tab is wider than the five canonical Diet
slices in :mod:`app.domain.player_diet_taxonomy`.  It shows the two corner
sides separately, and its ``Sum``/``PTS%``/``PTS%+`` arithmetic reads
``Backcourt`` before dropping it.  Those extra categories are provider
evidence for one profile, not a widening of the shared opponent/Target shot
zone taxonomy, so they are named here and nowhere else.

The residential collector cannot import this module -- it ships as a
standalone wheel with no ``app`` package -- so ``app/collector/normalizers.py``
keeps its own copy of these names.  ``tests/test_residential_collector.py``
pins the two against each other.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


#: The eight ``LeagueDashPlayerShotLocations`` categories, in the order the
#: provider's ``SHOT_CATEGORY`` header lists them.  The order is recorded for
#: readers; validation compares sets, because the header names the columns and
#: a reordered header is not a contract break.
PLAYER_SHOT_ZONE_PROFILE_CATEGORIES: tuple[str, ...] = (
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Left Corner 3",
    "Right Corner 3",
    "Above the Break 3",
    "Backcourt",
    "Corner 3",
)

#: The three values the endpoint reports for every category.
PLAYER_SHOT_ZONE_PROFILE_METRICS: tuple[str, ...] = ("FGM", "FGA", "FG_PCT")

#: The identity block the endpoint emits ahead of the categories.
PLAYER_SHOT_ZONE_PROFILE_IDENTITY: tuple[str, ...] = (
    "PLAYER_ID",
    "PLAYER_NAME",
    "TEAM_ID",
    "TEAM_ABBREVIATION",
    "AGE",
    "NICKNAME",
)

#: The category whose values reach ``Sum`` and the league ``PTS%`` reference
#: but never the rendered profile.
PLAYER_SHOT_ZONE_RECONCILED_CATEGORY = "Backcourt"


def player_shot_zone_profile_columns() -> tuple[str, ...]:
    """Return the exact flat source columns the profile transform may read.

    This is the fence that keeps games played, minutes, retrieval metadata and
    any other numeric column the provider or a later collector adds out of the
    legacy ``Sum``, which sums every remaining numeric column.
    """

    return (
        *PLAYER_SHOT_ZONE_PROFILE_IDENTITY,
        *(
            f"{category}_{metric}"
            for category in PLAYER_SHOT_ZONE_PROFILE_CATEGORIES
            for metric in PLAYER_SHOT_ZONE_PROFILE_METRICS
        ),
    )


def player_shot_zone_profile_violation(
    values: Mapping[str, Any], *, exact: bool = False
) -> str | None:
    """Name the first defect in one category's ``FGM``/``FGA``/``FG_PCT``.

    The zone counterpart of ``shot_type_shooting_violation``: every reported
    metric must be a finite nonnegative number and makes cannot exceed
    attempts.

    A category the provider did not report is ``None`` in all three metrics,
    which is legitimate and must stay ``None``.  The legacy profile's league
    ``PTS%`` reference is a mean that skips missing cells, so substituting zero
    would move every other player's ``PTS%+``.

    ``exact`` is for integer ``Totals`` evidence, where makes and attempts are
    whole counts and a rate attached to no attempts is a real defect.  Under ``PerGame`` it is not: the endpoint
    rounds attempts to one decimal while reporting the unrounded season rate,
    so a player with rare attempts legitimately reads ``0.0`` beside a nonzero
    percentage.
    """

    if all(values.get(metric) is None for metric in PLAYER_SHOT_ZONE_PROFILE_METRICS):
        return None
    numbers: dict[str, float] = {}
    for metric in PLAYER_SHOT_ZONE_PROFILE_METRICS:
        raw = values.get(metric)
        if raw is None:
            return f"{metric} is missing beside a reported metric"
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return f"{metric} is not a number"
        number = float(raw)
        if not math.isfinite(number) or number < 0:
            return f"{metric} is out of range"
        numbers[metric] = number
    if numbers["FGM"] > numbers["FGA"]:
        return "FGM exceeds FGA"
    if numbers["FG_PCT"] > 1:
        return "FG_PCT is out of range"
    if exact and numbers["FGA"] == 0 and numbers["FG_PCT"] != 0:
        return "FG_PCT describes no attempts"
    if exact and not (numbers["FGM"].is_integer() and numbers["FGA"].is_integer()):
        # Season ``Totals`` field goals are provider counts.  A fractional
        # value is a rounded ``PerGame`` reading wearing the ``Totals`` label,
        # which would understate every Diet volume derived from it.
        return "Totals counts are not whole"
    return None


__all__ = [
    "PLAYER_SHOT_ZONE_PROFILE_CATEGORIES",
    "PLAYER_SHOT_ZONE_PROFILE_IDENTITY",
    "PLAYER_SHOT_ZONE_PROFILE_METRICS",
    "PLAYER_SHOT_ZONE_RECONCILED_CATEGORY",
    "player_shot_zone_profile_columns",
    "player_shot_zone_profile_violation",
]
