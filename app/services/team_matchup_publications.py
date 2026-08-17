"""Shared taxonomy and provenance helpers for NBA team-window publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.models.catalogs import PLAY_TYPES


NBA_PUBLICATION_STREAMS = {
    "play_types": "synergy_play_types_opponent_{window}",
    "shot_types": "grouped_shot_types_opponent_{window}",
    "shot_zones": "exact_shot_zones_opponent_{window}",
}
NBA_PUBLICATION_WINDOWS = ("season", "l15")
NBA_PUBLICATION_BASES = frozenset(NBA_PUBLICATION_STREAMS)
NBA_PUBLICATION_STREAM_KEYS = frozenset(
    template.format(window=window)
    for template in NBA_PUBLICATION_STREAMS.values()
    for window in NBA_PUBLICATION_WINDOWS
)
SHOT_TYPE_DISPLAY_TO_STORED = {
    "Catch and Shoot": "catch_and_shoot",
    "Pullups": "pullups",
    "Less Than 10 ft": "less_than_10_ft",
}
SHOT_TYPE_STATS = frozenset({"FG2M", "FG2A", "FG3M", "FG3A"})
SHOT_ZONE_SLICES = frozenset(
    {
        "Restricted Area",
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Corner 3",
        "Above the Break 3",
    }
)
NBA_PUBLICATION_TAXONOMY = {
    "play_types": frozenset(
        f"{slice_key}_{stat_key}"
        for slice_key in PLAY_TYPES
        for stat_key in ("PTS", "POSS")
    ),
    "shot_types": frozenset(
        f"{slice_key}_{stat_key}"
        for slice_key in SHOT_TYPE_DISPLAY_TO_STORED.values()
        for stat_key in SHOT_TYPE_STATS
    ),
    "shot_zones": frozenset(
        f"{slice_key}_{stat_key}"
        for slice_key in SHOT_ZONE_SLICES
        for stat_key in ("FGM", "FGA")
    ),
}


@dataclass(frozen=True, slots=True)
class PublicationLineage:
    """Immutable source identity carried with one composed matchup surface."""

    publication_id: str | None
    cutoff: str | None
    freshness: str | None
    version: int | None


def publication_lineage(read) -> PublicationLineage:
    return PublicationLineage(
        publication_id=getattr(read, "publication_id", None),
        cutoff=publication_cutoff(read),
        freshness=getattr(read, "freshness", None),
        version=getattr(read, "version", None),
    )


def publication_cutoff(read) -> str | None:
    """Return the normalized immutable publication cutoff."""

    value = getattr(read, "cutoff", None)
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def publication_cutoff_reason(read, cutoff: date) -> str | None:
    """Reject a publication newer than the requested matchup cutoff."""

    value = getattr(read, "cutoff", None)
    if value is None:
        return None
    try:
        publication_date = (
            value.date()
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value)).date()
        )
    except ValueError:
        return "publication_cutoff_invalid"
    return (
        "publication_cutoff_after_as_of"
        if publication_date > cutoff
        else None
    )


def publication_metric_identity(base: str, metric_key: str) -> tuple[str, str]:
    """Split one publication key into the existing matchup taxonomy."""

    if "_" not in metric_key:
        return metric_key, metric_key
    slice_key, stat_key = metric_key.rsplit("_", 1)
    if base == "shot_types":
        slice_key = SHOT_TYPE_DISPLAY_TO_STORED.get(slice_key, slice_key)
    return slice_key, stat_key


def publication_stream(base: str, window: str) -> str:
    return NBA_PUBLICATION_STREAMS[base].format(window=window)


def publication_metric_keys(base: str) -> frozenset[str]:
    return NBA_PUBLICATION_TAXONOMY[base]


__all__ = [
    "NBA_PUBLICATION_BASES",
    "NBA_PUBLICATION_STREAMS",
    "NBA_PUBLICATION_STREAM_KEYS",
    "NBA_PUBLICATION_TAXONOMY",
    "NBA_PUBLICATION_WINDOWS",
    "PublicationLineage",
    "publication_cutoff",
    "publication_cutoff_reason",
    "publication_lineage",
    "publication_metric_identity",
    "publication_metric_keys",
    "publication_stream",
]
