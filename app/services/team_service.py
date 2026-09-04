"""Opponent Team Profile categories, mapped from the Season publications.

Every category `GET /api/teams/stats` serves is one projection of the durable
window-aware team matchup publications at their Season window, through the
same league table the Matchups Defense Sheet uses.  There is no request-time
provider call and no legacy ranking table read: values are per-48 on nominal
minutes, ranks are ascending over all thirty published rows, and a requested
date is accepted and ignored because the rankings are always whole-season.
"""

from __future__ import annotations

from nba_api.stats.static import teams

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.nba_teams import (
    NBA_TEAM_ID_TO_TRICODE,
    canonical_nba_team_abbreviation,
)
from app.domain.team_matchup_taxonomy import (
    PLAY_TYPES,
    SHOT_TYPE_SLICES,
    SHOT_TYPE_STATS,
    SHOT_TYPE_STORED_TO_DISPLAY,
    SHOT_ZONE_SLICES,
    SHOT_ZONE_STATS,
)
from app.errors import InvalidInputError
from app.services.team_matchup_query import (
    LeagueMetricColumn,
    league_metric_column,
    publication_league_table,
)

#: The publication base each panel category is served from.
CATEGORY_PUBLICATION_BASES: dict[str, str] = {
    "Traditional": "traditional",
    "Playtypes": "play_types",
    "Assists": "assist_locations",
    "Zone Shooting": "shot_zones",
    "Shooting Type": "shot_types",
}

#: The opponent box columns the panel renders and the ledger metric each one
#: reads.  ``OPP_OREB`` and ``OPP_DREB`` are absent because the ledger
#: publishes no rebound split; they render as ``N/A``.
_TRADITIONAL_FIELDS: dict[str, str] = {
    "OPP_PTS": "points",
    "OPP_FGM": "field_goals_made",
    "OPP_FGA": "field_goals_attempted",
    "OPP_FG3M": "three_pointers_made",
    "OPP_FG3A": "three_pointers_attempted",
    "OPP_FTA": "free_throws_attempted",
    "OPP_REB": "rebounds",
    "OPP_AST": "assists",
    "OPP_TOV": "turnovers",
    "OPP_STL": "steals",
    "OPP_BLK": "blocks",
}

#: The assist-location fields the panel renders and their ledger metrics.
_ASSIST_FIELDS: dict[str, str] = {
    "Assists": "assists",
    "TwoPtAssists": "two_point_assists",
    "ThreePtAssists": "three_point_assists",
    "Arc3Assists": "arc3_assists",
    "Corner3Assists": "corner3_assists",
    "AtRimAssists": "at_rim_assists",
    "ShortMidRangeAssists": "short_mid_range_assists",
    "LongMidRangeAssists": "long_mid_range_assists",
}

#: The one display name the panel sends that the team catalog does not carry.
_TEAM_NAME_ALIASES: dict[str, str] = {"LA Clippers": "Los Angeles Clippers"}

_TRICODE_TO_TEAM_ID: dict[str, int] = {
    tricode: team_id for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
}


class TeamService:
    def __init__(
        self,
        db_engine,
        settings: RuntimeSettings | None = None,
        season_publications=None,
    ):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        # The publication read seam of #198: it owns a publication reader and
        # a governance resolver, so no provider client is reachable from here.
        self.publications = season_publications

    def get_all_teams(self):
        team = teams.get_teams()
        team_names = [d['full_name'] for d in team]
        return team_names

    def get_team_stats(self, category, team, date=None):
        """Serve one opponent's Season profile for one panel category.

        ``date`` is accepted and ignored: the panel's rankings are always
        whole-season.  An unpublished, unproven, or unresolvable request
        serves nothing rather than a partial league.
        """

        if category not in CATEGORY_PUBLICATION_BASES:
            raise InvalidInputError(
                f"Unsupported team stats category: {category}."
            )
        empty = [] if category == "Shooting Type" else {}
        team_id = _resolve_team_id(team)
        rows = self._season_rows(CATEGORY_PUBLICATION_BASES[category])
        if team_id is None or rows is None:
            return empty
        return _CATEGORY_PROFILES[category](publication_league_table(rows), team_id)

    def _season_rows(self, base):
        """Read one publication base's canonical thirty Season rows."""

        if self.publications is None:
            return None
        return self.publications.season_rows(
            base, self.settings.nba.current_season
        )


def _resolve_team_id(team_name) -> int | None:
    """Resolve the panel's display name to a published team identity."""

    name = str(team_name or "").strip()
    name = _TEAM_NAME_ALIASES.get(name, name)
    for entry in teams.get_teams():
        if entry["full_name"] == name:
            return _TRICODE_TO_TEAM_ID.get(
                canonical_nba_team_abbreviation(entry["abbreviation"])
            )
    return None


def _combined_column(table, terms) -> LeagueMetricColumn:
    """Rank a linear combination of published columns across the league."""

    return league_metric_column({
        team_id: sum(
            coefficient * table[metric_key].values[team_id]
            for metric_key, coefficient in terms
        )
        for team_id in table[terms[0][0]].values
    })


def _rate_column(table, numerator_key, denominator_key) -> LeagueMetricColumn:
    """Rank one published rate across the league.

    A team that faced none of something has no rate at all.  The panel renders
    one row per team, so such a team scores 0.0 here rather than being dropped
    from the category the way a Team Filter drops it from its ranking.
    """

    denominator = table[denominator_key].values
    return league_metric_column({
        team_id: (value / denominator[team_id] if denominator[team_id] else 0.0)
        for team_id, value in table[numerator_key].values.items()
    })


def _place(stats, field, column, team_id) -> None:
    """Write one column's value, rank, and distance from the league average."""

    stats[field] = column.values[team_id]
    stats[f"{field}_RANK"] = column.rank(team_id)
    stats[f"{field}_vs_avg_pct"] = column.percent_vs_league_average(team_id)


def _place_ratio(stats, field, column, team_id) -> None:
    """Write one column as a ratio to the league average, plus its rank.

    The play-type and assist charts are centred on 1.0, so those categories
    carry the ratio rather than the per-48 value.  The rank stays on the
    published column, where a division can neither create nor break a tie.
    """

    stats[field] = (
        column.values[team_id] / column.average if column.average else 0.0
    )
    stats[f"{field}_RANK"] = column.rank(team_id)


def _traditional_profile(table, team_id) -> dict:
    stats: dict = {}
    for field, metric_key in _TRADITIONAL_FIELDS.items():
        _place(stats, field, table[metric_key], team_id)
    _place(
        stats,
        "OPP_STL+BLK",
        _combined_column(table, (("steals", 1.0), ("blocks", 1.0))),
        team_id,
    )
    _place(
        stats,
        "OPP_FG_PCT",
        _rate_column(table, "field_goals_made", "field_goals_attempted"),
        team_id,
    )
    _place(
        stats,
        "OPP_FG3_PCT",
        _rate_column(table, "three_pointers_made", "three_pointers_attempted"),
        team_id,
    )
    return stats


def _play_type_profile(table, team_id) -> dict:
    stats: dict = {}
    for play_type in PLAY_TYPES:
        _place_ratio(
            stats,
            play_type,
            _rate_column(table, f"{play_type}_PTS", f"{play_type}_POSS"),
            team_id,
        )
    return stats


def _assist_profile(table, team_id) -> dict:
    stats: dict = {}
    for field, metric_key in _ASSIST_FIELDS.items():
        _place_ratio(stats, field, table[metric_key], team_id)
    _place_ratio(
        stats,
        "AssistPoints",
        _combined_column(
            table, (("two_point_assists", 2.0), ("three_point_assists", 3.0))
        ),
        team_id,
    )
    return stats


def _shot_zone_profile(table, team_id) -> dict:
    stats: dict = {}
    for zone in SHOT_ZONE_SLICES:
        for stat_key in SHOT_ZONE_STATS:
            _place(
                stats,
                f"{zone}_OPP_{stat_key}",
                table[f"{zone}_{stat_key}"],
                team_id,
            )
    return stats


def _shot_type_profile(table, team_id) -> list:
    profile = []
    for slice_key in SHOT_TYPE_SLICES:
        stats = {"ShootingType": SHOT_TYPE_STORED_TO_DISPLAY[slice_key]}
        _place(
            stats,
            "PTS",
            _combined_column(
                table,
                (
                    (f"{slice_key}_FG2M", 2.0),
                    (f"{slice_key}_FG3M", 3.0),
                ),
            ),
            team_id,
        )
        for stat_key in SHOT_TYPE_STATS:
            _place(stats, stat_key, table[f"{slice_key}_{stat_key}"], team_id)
        profile.append(stats)
    return profile


_CATEGORY_PROFILES = {
    "Traditional": _traditional_profile,
    "Playtypes": _play_type_profile,
    "Assists": _assist_profile,
    "Zone Shooting": _shot_zone_profile,
    "Shooting Type": _shot_type_profile,
}
