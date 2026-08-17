"""Shared taxonomy and provenance helpers for NBA team-window publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


NBA_PUBLICATION_STREAMS = {
    "play_types": "synergy_play_types_opponent_{window}",
    "shot_types": "grouped_shot_types_opponent_{window}",
    "shot_zones": "exact_shot_zones_opponent_{window}",
}
NBA_PUBLICATION_BASES = frozenset(NBA_PUBLICATION_STREAMS)
SHOT_TYPE_DISPLAY_TO_STORED = {
    "Catch and Shoot": "catch_and_shoot",
    "Pullups": "pullups",
    "Less Than 10 ft": "less_than_10_ft",
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


__all__ = [
    "NBA_PUBLICATION_BASES",
    "NBA_PUBLICATION_STREAMS",
    "PublicationLineage",
    "publication_cutoff",
    "publication_cutoff_reason",
    "publication_lineage",
    "publication_metric_identity",
]
