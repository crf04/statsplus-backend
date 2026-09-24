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

#: The ledger-owned non-shot matchup surfaces that have a legacy-versus-ledger
#: dual-run before activation.  NBA-owned shot zones, grouped shot types, and
#: Synergy play types are composed from governed publications and excluded.
LEDGER_OWNED_MATCHUP_SURFACES: tuple[str, ...] = ("traditional", "assist_locations")
LEDGER_OWNED_MATCHUP_STREAM_PREFIX: dict[str, str] = {
    "traditional": "traditional_opponent",
    "assist_locations": "assist_locations",
}
LEDGER_OWNED_MATCHUP_STREAM_KEYS = frozenset(
    f"{prefix}_{window}"
    for prefix in LEDGER_OWNED_MATCHUP_STREAM_PREFIX.values()
    for window in NBA_PUBLICATION_WINDOWS
)


def publication_metric_identity(base: str, metric_key: str) -> tuple[str, str]:
    """Split one publication key into the existing matchup taxonomy."""

    if "_" not in metric_key:
        return metric_key, metric_key
    slice_key, stat_key = metric_key.rsplit("_", 1)
    if base == "shot_types":
        slice_key = SHOT_TYPE_DISPLAY_TO_STORED.get(slice_key, slice_key)
    return slice_key, stat_key


#: The Matchup Defense Sheet's bases, in the order the response lists them.
DEFENSE_SHEET_BASES: tuple[str, ...] = (
    "play_types",
    "shot_zones",
    "shot_types",
    "assist_locations",
    "traditional",
)

#: The ledger-owned Defense Sheet rows: each row's display identity and the
#: publication metric it projects.  The NBA-owned bases project every key
#: their publication carries, split by :func:`publication_metric_identity`.
LEDGER_DEFENSE_SHEET_METRICS: dict[str, dict[str, str]] = {
    "traditional": {
        "OPP_REB": "rebounds",
        "OPP_TOV": "turnovers",
        "OPP_STL": "steals",
        "OPP_BLK": "blocks",
    },
    "assist_locations": {
        "Assists": "assists",
        "Arc3Assists": "arc3_assists",
        "Corner3Assists": "corner3_assists",
        "AtRimAssists": "at_rim_assists",
        "ShortMidRangeAssists": "short_mid_range_assists",
        "LongMidRangeAssists": "long_mid_range_assists",
    },
}


def defense_sheet_identities(base: str) -> tuple[tuple[str, str, str], ...]:
    """Return ``(slice_key, stat_key, metric_key)`` for one base's Sheet rows.

    Grouped shot/zone/Synergy publications carry their complete identity in
    the metric key (for example ``Isolation_PTS``); the ledger-owned bases
    project the curated rows above.
    """

    display = LEDGER_DEFENSE_SHEET_METRICS.get(base)
    if display is None:
        return tuple(
            (*publication_metric_identity(base, key), key)
            for key in NBA_PUBLICATION_METRIC_KEYS[base]
        )
    return tuple(
        (display_key, display_key, metric_key)
        for display_key, metric_key in display.items()
    )


def defense_sheet_display_slice(base: str, slice_key: str) -> str:
    """The slice as the Sheet shows it (shot types by their display name)."""

    if base == "shot_types":
        return SHOT_TYPE_STORED_TO_DISPLAY[slice_key]
    return slice_key


def defense_sheet_row_key(base: str, slice_key: str, stat_key: str) -> str:
    """The ``defense_sheet[<base>][].key`` of one Sheet row."""

    display_slice = defense_sheet_display_slice(base, slice_key)
    return (
        display_slice
        if display_slice == stat_key
        else f"{display_slice}:{stat_key}"
    )


#: Every Defense Sheet row key and the Season publication metric behind it,
#: per base: the vocabulary ``sheet:<base>:<row key>`` Team Filters accept.
DEFENSE_SHEET_ROW_METRICS: dict[str, dict[str, str]] = {
    base: {
        defense_sheet_row_key(base, slice_key, stat_key): metric_key
        for slice_key, stat_key, metric_key in defense_sheet_identities(base)
    }
    for base in DEFENSE_SHEET_BASES
}

#: The prefix marking a ``teams_against`` entry as a Defense Sheet row.
DEFENSE_SHEET_FILTER_PREFIX = "sheet:"


def defense_sheet_filter(team_filter: str) -> tuple[str, str] | None:
    """Resolve ``sheet:<base>:<row key>`` to ``(base, metric_key)``.

    ``None`` means the value is not a Defense Sheet row reference this
    taxonomy knows: not prefixed, an unknown base, or an unknown row key.
    """

    if not team_filter.startswith(DEFENSE_SHEET_FILTER_PREFIX):
        return None
    base, separator, row_key = team_filter[
        len(DEFENSE_SHEET_FILTER_PREFIX):
    ].partition(":")
    if not separator:
        return None
    metric_key = DEFENSE_SHEET_ROW_METRICS.get(base, {}).get(row_key)
    if metric_key is None:
        return None
    return base, metric_key


def matchup_stream_key(surface: str, window: str) -> str:
    """Return the canonical ledger-owned stream key for one surface and window."""

    if surface not in LEDGER_OWNED_MATCHUP_STREAM_PREFIX:
        raise ValueError(f"unsupported matchup surface {surface}")
    if window not in NBA_PUBLICATION_WINDOWS:
        raise ValueError(f"unsupported matchup window {window}")
    return f"{LEDGER_OWNED_MATCHUP_STREAM_PREFIX[surface]}_{window}"


__all__ = [
    "DEFENSE_SHEET_BASES",
    "DEFENSE_SHEET_FILTER_PREFIX",
    "DEFENSE_SHEET_ROW_METRICS",
    "LEDGER_DEFENSE_SHEET_METRICS",
    "LEDGER_OWNED_MATCHUP_STREAM_KEYS",
    "LEDGER_OWNED_MATCHUP_STREAM_PREFIX",
    "LEDGER_OWNED_MATCHUP_SURFACES",
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
    "defense_sheet_display_slice",
    "defense_sheet_filter",
    "defense_sheet_identities",
    "defense_sheet_row_key",
    "matchup_stream_key",
    "publication_metric_identity",
]
