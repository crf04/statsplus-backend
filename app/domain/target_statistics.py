"""Target display-stat vocabulary shared verbatim with the frontend."""

from collections.abc import Mapping, Sequence
import json
from pathlib import Path

from app.errors import InvalidInputError
from app.domain.team_matchup_taxonomy import (
    THREE_POINT_SHOT_ZONES,
    TWO_POINT_SHOT_ZONES,
)


TARGET_STAT_KEYS = frozenset(
    json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/contracts/target-stat-catalogue.json"
        ).read_text()
    )
)


# Target defaults are display choices for the backtest, rather than the
# Defense Sheet's complete market mapping.  In particular, a shot-zone
# qualifier chooses the attempts that describe that zone, while the Matchup
# continues to publish every governed row and market it owns.
_PLAY_TYPE_DEFAULTS = ("PTS", "FGA", "PTS/36", "FGA/36")
_TWO_POINT_ZONE_DEFAULTS = ("PTS", "PTS/36", "FG2A", "FG2A/36")
_THREE_POINT_ZONE_DEFAULTS = ("PTS", "PTS/36", "3PA", "3PA/36")
_SHOT_TYPE_DEFAULTS = ("PTS", "PTS/36", "FGA", "FGA/36")
_ASSIST_LOCATION_DEFAULTS = ("AST", "AST/36")


def target_default_stat_columns(base: str, slice_key: str) -> tuple[str, ...]:
    """Return the approved display columns for one Qualifier slice."""

    if base == "play_types":
        return _PLAY_TYPE_DEFAULTS
    if base == "shot_zones":
        if slice_key in TWO_POINT_SHOT_ZONES:
            return _TWO_POINT_ZONE_DEFAULTS
        if slice_key in THREE_POINT_SHOT_ZONES:
            return _THREE_POINT_ZONE_DEFAULTS
    if base == "shot_types":
        return _SHOT_TYPE_DEFAULTS
    if base == "assist_locations":
        return _ASSIST_LOCATION_DEFAULTS
    # Validation normally makes this unreachable.  Returning an empty tuple
    # lets a malformed legacy row remain readable without inventing a stat.
    return ()


def target_stat_columns(
    qualifiers: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Resolve the approved default union order for a Target."""

    columns: list[str] = []
    for qualifier in qualifiers:
        for column in target_default_stat_columns(
            str(qualifier.get("base", "")),
            str(qualifier.get("slice_key", "")),
        ):
            if column not in columns:
                columns.append(column)
    return tuple(columns)


def validate_stat_preferences(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidInputError("Stat preferences must be an object or null.")
    columns = value.get("columns")
    if (
        not isinstance(columns, list)
        or not columns
        or any(
            not isinstance(key, str) or key not in TARGET_STAT_KEYS for key in columns
        )
    ):
        raise InvalidInputError(
            "Stat columns must be a non-empty list of catalogue keys."
        )
    graded_by = value.get("graded_by")
    if not isinstance(graded_by, str) or graded_by not in columns:
        raise InvalidInputError("The grading stat must be one of the selected columns.")
    return {"columns": list(dict.fromkeys(columns)), "graded_by": graded_by}
