"""Shared taxonomy and provenance helpers for NBA team-window publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from collections.abc import Mapping

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.slate_time import publication_cutoff_is_after_slate_day
from app.domain.utc import parse_utc_iso
from app.domain.team_matchup_taxonomy import (
    NBA_PUBLICATION_BASES,
    NBA_PUBLICATION_STREAM_KEYS,
    NBA_PUBLICATION_STREAMS,
    NBA_PUBLICATION_METRIC_KEYS,
    NBA_PUBLICATION_TAXONOMY,
    NBA_PUBLICATION_WINDOWS,
    PLAY_TYPE_STATS,
    SHOT_TYPE_DISPLAY_TO_STORED,
    SHOT_TYPE_SLICES,
    SHOT_TYPE_STATS,
    SHOT_TYPE_STORED_TO_DISPLAY,
    SHOT_ZONE_SLICES,
    SHOT_ZONE_STATS,
)


class PublicationValidationError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def publication_base_for_stream(stream_key: str) -> str | None:
    for base, template in NBA_PUBLICATION_STREAMS.items():
        if stream_key in {template.format(window=window) for window in NBA_PUBLICATION_WINDOWS}:
            return base
    return None


def validate_publication_rows(
    base: str,
    rows,
    *,
    expected_game_ids_by_team=None,
    window: str | None = None,
    expected_l15_game_ids=None,
    expected_team_ids=None,
) -> tuple[str, ...]:
    """Apply canonical league, taxonomy, and optional governed-window rules.

    ``expected_team_ids`` is evidence from a caller's governed roster.  It is
    deliberately an additional equality constraint: a caller can narrow the
    accepted publication only after the payload has already proved the exact
    canonical NBA identity set.
    """
    expected_keys = tuple(sorted(NBA_PUBLICATION_TAXONOMY[base]))
    canonical_team_ids = set(NBA_TEAM_ID_TO_TRICODE)
    row_team_ids = [row.team_id for row in rows]
    if len(rows) != len(canonical_team_ids) or len(row_team_ids) != len(set(row_team_ids)):
        raise PublicationValidationError("publication_surface_incomplete")
    if set(row_team_ids) != canonical_team_ids:
        raise PublicationValidationError("publication_surface_incomplete")
    if expected_team_ids is not None:
        try:
            governed_team_ids = {int(team_id) for team_id in expected_team_ids}
        except (TypeError, ValueError, OverflowError) as error:
            raise PublicationValidationError("publication_surface_incomplete") from error
        if governed_team_ids != canonical_team_ids:
            raise PublicationValidationError("publication_surface_incomplete")
    expected = set(expected_keys)
    for row in rows:
        if row.team_tricode != NBA_TEAM_ID_TO_TRICODE[row.team_id]:
            raise PublicationValidationError("publication_team_identity_mismatch")
        for values in (row.per48,):
            raw_keys = tuple(values)
            if len(raw_keys) != len(set(raw_keys)) or set(raw_keys) != expected:
                raise PublicationValidationError("publication_metric_taxonomy_mismatch")
        for values in (
            row.league_average,
            row.population_sigma,
            row.competition_rank,
        ):
            if values and set(values) != expected:
                raise PublicationValidationError(
                    "publication_metric_taxonomy_mismatch"
                )
    if expected_game_ids_by_team is None and expected_l15_game_ids is not None:
        expected_game_ids_by_team = expected_l15_game_ids
        window = "l15"
    if expected_game_ids_by_team is not None:
        if window not in NBA_PUBLICATION_WINDOWS:
            raise PublicationValidationError("publication_game_set_mismatch")
        if {row.team_id for row in rows} != set(expected_game_ids_by_team) or any(
            len(row.game_ids) != len(set(row.game_ids))
            or (window == "l15" and len(row.game_ids) != 15)
            or set(row.game_ids) != set(expected_game_ids_by_team[row.team_id])
            for row in rows
        ):
            raise PublicationValidationError("publication_game_set_mismatch")
    return expected_keys


def resolve_governed_l15_game_ids(
    resolver,
    season: str,
    cutoff,
) -> Mapping[int, frozenset[str]]:
    """Resolve one independent, season-and-cutoff L15 expectation.

    The production resolver is intentionally injected.  Supporting the small
    ``resolve_l15_game_ids``/``resolve``/callable vocabulary keeps test and
    offline orchestration adapters at the same public seam without allowing a
    missing or malformed resolver result to become an implicit empty window.
    """

    return resolve_governed_team_game_ids(resolver, season, cutoff, window="l15")


def resolve_governed_team_game_ids(
    resolver,
    season: str,
    cutoff,
    *,
    window: str,
    manifest_id: str | None = None,
    event_catalog_publication_id: str | None = None,
    event_catalog_checksum: str | None = None,
) -> Mapping[int, frozenset[str]]:
    """Resolve exact governed per-team IDs for one immutable window."""

    if window not in NBA_PUBLICATION_WINDOWS or resolver is None:
        raise ValueError("publication_governance_unavailable")
    authority_requested = any((
        manifest_id,
        event_catalog_publication_id,
        event_catalog_checksum,
    ))
    result = None
    method_names = (
        "resolve_team_game_ids",
        "resolve_l15_game_ids" if window == "l15" else "resolve_season_game_ids",
        "resolve",
        "read_for_composition",
    )
    for name in method_names:
        operation = getattr(resolver, name, None)
        if callable(operation):
            if authority_requested and name != "resolve_team_game_ids":
                raise ValueError("publication_governance_unavailable")
            if name == "resolve_team_game_ids":
                try:
                    result = operation(
                        season,
                        cutoff,
                        window=window,
                        manifest_id=manifest_id,
                        event_catalog_publication_id=(
                            event_catalog_publication_id
                        ),
                        event_catalog_checksum=event_catalog_checksum,
                    )
                except TypeError:
                    if authority_requested:
                        raise ValueError(
                            "publication_governance_unavailable"
                        ) from None
                    # Small offline/test adapters may implement the earlier
                    # season/cutoff vocabulary only when no immutable
                    # authority was requested.
                    result = operation(season, cutoff, window=window)
            else:
                result = operation(season, cutoff)
            break
    else:
        if callable(resolver):
            if authority_requested:
                raise ValueError("publication_governance_unavailable")
            result = resolver(season, cutoff)
        else:
            raise ValueError("publication_governance_unavailable")
    attribute = (
        "expected_l15_game_ids"
        if window == "l15"
        else "expected_season_game_ids"
    )
    result = getattr(result, attribute, result)
    if not isinstance(result, Mapping):
        raise ValueError("publication_governance_unavailable")
    normalized: dict[int, frozenset[str]] = {}
    try:
        for team_id, game_ids in result.items():
            normalized[int(team_id)] = frozenset(str(game_id) for game_id in game_ids)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError("publication_governance_unavailable") from None
    if set(normalized) != set(NBA_TEAM_ID_TO_TRICODE):
        raise ValueError("publication_governance_unavailable")
    if any(not game_ids for game_ids in normalized.values()):
        raise ValueError("publication_governance_unavailable")
    return normalized


@dataclass(frozen=True, slots=True)
class PublicationLineage:
    """Immutable source identity carried with one composed matchup surface."""

    publication_id: str | None
    cutoff: str | None
    freshness: str | None
    version: int | None


def publication_lineage(read) -> PublicationLineage | None:
    publication_id = getattr(read, "publication_id", None)
    if not publication_id:
        return None
    return PublicationLineage(
        publication_id=publication_id,
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
        publication_instant = (
            value if isinstance(value, datetime) else parse_utc_iso(str(value))
        )
        if publication_instant.tzinfo is None or publication_instant.utcoffset() is None:
            raise ValueError("publication cutoff must be timezone-aware")
    except (AttributeError, TypeError, ValueError):
        return "publication_cutoff_invalid"
    return (
        "publication_cutoff_after_as_of"
        if publication_cutoff_is_after_slate_day(publication_instant, cutoff)
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


def publication_metric_keys(base: str) -> tuple[str, ...]:
    return NBA_PUBLICATION_METRIC_KEYS[base]


__all__ = [
    "NBA_PUBLICATION_BASES",
    "NBA_PUBLICATION_STREAMS",
    "NBA_PUBLICATION_STREAM_KEYS",
    "NBA_PUBLICATION_TAXONOMY",
    "NBA_PUBLICATION_WINDOWS",
    "PLAY_TYPE_STATS",
    "PublicationLineage",
    "PublicationValidationError",
    "SHOT_TYPE_SLICES",
    "SHOT_TYPE_STATS",
    "SHOT_TYPE_STORED_TO_DISPLAY",
    "SHOT_ZONE_SLICES",
    "SHOT_ZONE_STATS",
    "publication_base_for_stream",
    "publication_cutoff",
    "publication_cutoff_reason",
    "publication_lineage",
    "publication_metric_identity",
    "publication_metric_keys",
    "publication_stream",
    "resolve_governed_l15_game_ids",
    "resolve_governed_team_game_ids",
    "validate_publication_rows",
]
