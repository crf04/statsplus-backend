"""Canonical player Diet slice vocabulary and how each slice reads.

This module is deliberately below the service layer, beside the matchup
taxonomy that already owns the play types, shot types, and shot zones it
builds on.  Collection, criteria validation, and any user-facing rendering all
consume the same identities from here instead of keeping parallel copies.

Three vocabularies, narrowing:

``PLAYER_DIET_BASE_SLICES``
    Everything a base publishes.  This is what the collectors write.
``PLAYER_DIET_QUALIFIER_SLICES``
    The subset a criterion may name.  A slice can be collected and reported
    without being something a player is filtered on.
``PLAYER_DIET_SLICE_LABELS``
    How each qualifiable slice reads in user-facing text.  The slice keys are
    provider vocabulary and are what gets stored; this is the one backend
    source for how they are shown, so a rendered label cannot drift from the
    key it names.
"""

from __future__ import annotations

from types import MappingProxyType

from app.domain.team_matchup_taxonomy import (
    PLAY_TYPES,
    SHOT_TYPE_DISPLAY_TO_STORED,
    SHOT_ZONE_SLICES,
)


#: Assist locations, in publication order.  Owned here rather than by the
#: collection service so the diet vocabulary is defined in one layer.
ASSIST_SLICES: tuple[str, ...] = (
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)

#: Shot types under their display keys, which is the form a diet slice uses.
_SHOT_TYPE_SLICES: tuple[str, ...] = tuple(SHOT_TYPE_DISPLAY_TO_STORED)

#: The slice vocabulary each diet base publishes, in publication order.
PLAYER_DIET_BASE_SLICES = MappingProxyType({
    "assist_locations": ASSIST_SLICES,
    "play_types": PLAY_TYPES,
    "shot_types": _SHOT_TYPE_SLICES,
    "shot_zones": SHOT_ZONE_SLICES,
})

#: Published slices that cannot be used as a criterion.  ``Misc`` is Synergy's
#: residual bucket rather than a shot profile, so it is collected and reported
#: but never offered as something a player can be filtered on.
_UNQUALIFIABLE_DIET_SLICES = frozenset({"Misc"})

#: The slices a criterion may name, per base.
PLAYER_DIET_QUALIFIER_SLICES = MappingProxyType({
    base: tuple(
        slice_key
        for slice_key in slices
        if slice_key not in _UNQUALIFIABLE_DIET_SLICES
    )
    for base, slices in PLAYER_DIET_BASE_SLICES.items()
})

#: The human-readable label each qualifiable slice reads as.
PLAYER_DIET_SLICE_LABELS = MappingProxyType({
    # shot_zones
    "Restricted Area": "Restricted area",
    "In The Paint (Non-RA)": "Paint (non-RA)",
    "Mid-Range": "Mid-range",
    "Corner 3": "Corner 3",
    "Above the Break 3": "Above-break 3",
    # play_types
    "Transition": "Transition",
    "Isolation": "Isolation",
    "PRBallHandler": "P&R ball handler",
    "PRRollMan": "P&R roll man",
    "Spotup": "Spot up",
    "Cut": "Cut",
    "Handoff": "Handoff",
    "OffScreen": "Off screen",
    "Postup": "Post up",
    "OffRebound": "Putback",
    # shot_types
    "Catch and Shoot": "Catch & shoot",
    "Pullups": "Pull-up",
    "Less Than 10 ft": "Inside 10 ft",
    # assist_locations
    "Arc3Assists": "Arc 3 assists",
    "Corner3Assists": "Corner 3 assists",
    "AtRimAssists": "At-rim assists",
    "ShortMidRangeAssists": "Short mid assists",
    "LongMidRangeAssists": "Long mid assists",
})


__all__ = [
    "ASSIST_SLICES",
    "PLAYER_DIET_BASE_SLICES",
    "PLAYER_DIET_QUALIFIER_SLICES",
    "PLAYER_DIET_SLICE_LABELS",
]
