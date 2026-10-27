"""Target display-stat vocabulary shared verbatim with the frontend."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.domain.team_matchup_taxonomy import (
    THREE_POINT_SHOT_ZONES,
    TWO_POINT_SHOT_ZONES,
)
from app.errors import InvalidInputError

TARGET_STAT_KEYS = frozenset(
    json.loads(
        (Path(__file__).resolve().parents[2] / "docs/contracts/target-stat-catalogue.json").read_text()
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
#: What a Target shows when no Qualifier names a Diet slice to describe, so a
#: Position-only or Average-only Target still has columns to read.
_PLAYER_RULE_DEFAULTS = ("PTS", "PTS/36")
#: The columns the Backtest reads per game: the governed markets (``3PA`` is the
#: display spelling of ``FG3A``) and their per-36 rates.  An Average Qualifier
#: adds its own stat only where the Backtest can read it per game.
_DISPLAYABLE_STATS = frozenset(
    {"PTS", "REB", "AST", "3PM", "3PA", "FG2A", "FGA", "STL", "BLK", "TOV", "PA", "PR", "RA", "PRA"}
)


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
        if qualifier.get("base") == "season_average":
            stat = str(qualifier.get("stat", ""))
            candidates = (stat,) if stat.removesuffix("/36") in _DISPLAYABLE_STATS else ()
        else:
            candidates = target_default_stat_columns(
                str(qualifier.get("base", "")),
                str(qualifier.get("slice_key", "")),
            )
        for column in candidates:
            if column not in columns:
                columns.append(column)
    return tuple(columns) or _PLAYER_RULE_DEFAULTS


#: The box-score totals a season stat is read from, by the names the Backtest's
#: ``season_totals`` carries.
_COMBINATIONS = {
    "PA": ("PTS", "AST"),
    "PR": ("PTS", "REB"),
    "RA": ("REB", "AST"),
    "PRA": ("PTS", "REB", "AST"),
    "SB": ("STL", "BLK"),
}
_BOX_TOTALS = {
    "PTS": "points",
    "REB": "rebounds",
    "AST": "assists",
    "3PM": "threes_made",
    "3PA": "threes_attempted",
    "FGM": "field_goals_made",
    "FGA": "field_goals_attempted",
    "FTM": "free_throws_made",
    "FTA": "free_throws_attempted",
    "STL": "steals",
    "BLK": "blocks",
    "TOV": "turnovers",
    "OREB": "offensive_rebounds",
    "DREB": "defensive_rebounds",
    "PF": "fouls",
    "MIN": "minutes",
}


#: The Backtest's box-score field names and the game-log record attribute each
#: one sums.
BOX_FIELD_ATTRIBUTES = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "field_goals_made": "field_goals_made",
    "field_goals_attempted": "field_goals_attempted",
    "threes_made": "three_pointers_made",
    "threes_attempted": "three_pointers_attempted",
    "free_throws_made": "free_throws_made",
    "free_throws_attempted": "free_throws_attempted",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "offensive_rebounds": "offensive_rebounds",
    "defensive_rebounds": "defensive_rebounds",
    "fouls": "personal_fouls",
    "minutes": "minutes",
}


def _ratio(numerator: float, denominator: float, multiplier: float = 1.0) -> float | None:
    return numerator / denominator * multiplier if denominator > 0 else None


def _total(stat: str, totals: Mapping[str, float]) -> float:
    """One stat summed over a span: a box total, a combination or FG2A."""

    if stat in _COMBINATIONS:
        return sum(_total(part, totals) for part in _COMBINATIONS[stat])
    if stat == "FG2A":
        return totals["field_goals_attempted"] - totals["threes_attempted"]
    return totals[_BOX_TOTALS[stat]]


def season_stat(stat: str, totals: Mapping[str, float], games: int) -> float | None:
    """Return a Target stat column over a season, from its box-score totals.

    The same arithmetic the web app applies to ``season_totals``: a counting
    stat is a per-game average, a /36 stat is the rate over total minutes, and
    an efficiency stat is a ratio of totals (percentages scaled to 0-100).
    ``None`` when the span has nothing to divide by.
    """

    if stat.endswith("/36"):
        return _ratio(_total(stat[:-3], totals), totals["minutes"], 36.0)
    if stat == "FG%":
        return _ratio(totals["field_goals_made"], totals["field_goals_attempted"], 100.0)
    if stat == "3P%":
        return _ratio(totals["threes_made"], totals["threes_attempted"], 100.0)
    if stat == "TS%":
        return _ratio(
            totals["points"],
            2 * (totals["field_goals_attempted"] + 0.44 * totals["free_throws_attempted"]),
            100.0,
        )
    if stat == "PTS/FGA":
        return _ratio(totals["points"], totals["field_goals_attempted"])
    return _ratio(_total(stat, totals), games)


def season_average_value(stat: str, records: Sequence[object]) -> float | None:
    """Return a Target stat column over a player's game-log records, or ``None``."""

    totals = {
        field: sum(getattr(record, attribute) for record in records)
        for field, attribute in BOX_FIELD_ATTRIBUTES.items()
    }
    return season_stat(stat, totals, len(records))


def validate_stat_preferences(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidInputError("Stat preferences must be an object or null.")
    columns = value.get("columns")
    if (
        not isinstance(columns, list)
        or not columns
        or any(not isinstance(key, str) or key not in TARGET_STAT_KEYS for key in columns)
    ):
        raise InvalidInputError("Stat columns must be a non-empty list of catalogue keys.")
    graded_by = value.get("graded_by")
    if not isinstance(graded_by, str) or graded_by not in columns:
        raise InvalidInputError("The grading stat must be one of the selected columns.")
    return {"columns": list(dict.fromkeys(columns)), "graded_by": graded_by}
