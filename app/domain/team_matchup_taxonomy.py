"""Canonical taxonomy for team matchup publications and scoring.

This module is deliberately below the service layer.  The matchup response,
collection registry, and publication codecs all consume the same immutable
identities from here instead of maintaining parallel copies of the provider
labels and stream keys.
"""

from __future__ import annotations


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

SHOT_TYPE_DISPLAY_TO_STORED: dict[str, str] = {
    "Catch and Shoot": "catch_and_shoot",
    "Pullups": "pullups",
    "Less Than 10 ft": "less_than_10_ft",
}
SHOT_TYPE_STORED_TO_DISPLAY: dict[str, str] = {
    stored: display for display, stored in SHOT_TYPE_DISPLAY_TO_STORED.items()
}
SHOT_TYPE_SLICES: tuple[str, ...] = tuple(SHOT_TYPE_STORED_TO_DISPLAY)
SHOT_ZONE_SLICES: tuple[str, ...] = (
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Corner 3",
    "Above the Break 3",
)
TWO_POINT_SHOT_ZONES = frozenset(
    {"Restricted Area", "Paint", "In The Paint (Non-RA)", "Mid-Range"}
)
THREE_POINT_SHOT_ZONES = frozenset({"Corner 3", "Above the Break 3"})

PLAY_TYPE_STATS: tuple[str, ...] = ("PTS", "POSS")
SHOT_TYPE_STATS: tuple[str, ...] = ("FG2M", "FG2A", "FG3M", "FG3A")
SHOT_ZONE_STATS: tuple[str, ...] = ("FGM", "FGA")

NBA_PUBLICATION_STREAMS: dict[str, str] = {
    "play_types": "synergy_play_types_opponent_{window}",
    "shot_types": "grouped_shot_types_opponent_{window}",
    "shot_zones": "exact_shot_zones_opponent_{window}",
}
NBA_PUBLICATION_WINDOWS: tuple[str, ...] = ("season", "l15")
NBA_PUBLICATION_BASES = frozenset(NBA_PUBLICATION_STREAMS)
NBA_PUBLICATION_STREAM_KEYS = frozenset(
    template.format(window=window)
    for template in NBA_PUBLICATION_STREAMS.values()
    for window in NBA_PUBLICATION_WINDOWS
)
NBA_PUBLICATION_METRIC_KEYS = {
    "play_types": tuple(
        f"{slice_key}_{stat_key}"
        for slice_key in PLAY_TYPES
        for stat_key in PLAY_TYPE_STATS
    ),
    "shot_types": tuple(
        f"{slice_key}_{stat_key}"
        for slice_key in SHOT_TYPE_SLICES
        for stat_key in SHOT_TYPE_STATS
    ),
    "shot_zones": tuple(
        f"{slice_key}_{stat_key}"
        for slice_key in SHOT_ZONE_SLICES
        for stat_key in SHOT_ZONE_STATS
    ),
}
NBA_PUBLICATION_TAXONOMY = {
    base: frozenset(keys) for base, keys in NBA_PUBLICATION_METRIC_KEYS.items()
}


__all__ = [
    "NBA_PUBLICATION_BASES",
    "NBA_PUBLICATION_STREAM_KEYS",
    "NBA_PUBLICATION_STREAMS",
    "NBA_PUBLICATION_METRIC_KEYS",
    "NBA_PUBLICATION_TAXONOMY",
    "NBA_PUBLICATION_WINDOWS",
    "PLAY_TYPES",
    "PLAY_TYPE_STATS",
    "SHOT_TYPE_DISPLAY_TO_STORED",
    "SHOT_TYPE_SLICES",
    "SHOT_TYPE_STATS",
    "SHOT_TYPE_STORED_TO_DISPLAY",
    "SHOT_ZONE_SLICES",
    "SHOT_ZONE_STATS",
    "THREE_POINT_SHOT_ZONES",
    "TWO_POINT_SHOT_ZONES",
]
