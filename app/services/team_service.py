"""Opponent Team Profile categories, mapped from the Season publications.

Every category `GET /api/teams/stats` serves is one projection of the durable
window-aware team matchup publications at their Season window, through the
same league table the Matchups Defense Sheet uses.  There is no request-time
provider call and no legacy ranking table read: values are per-48 on nominal
minutes, ranks are ascending over all thirty published rows, and a requested
date is accepted and ignored because the rankings are always whole-season.
"""

from __future__ import annotations

from collections.abc import Callable

from nba_api.stats.static import teams

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.nba_teams import (
    NBA_TEAM_TRICODE_TO_ID,
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
from app.services.team_filter_rankings import TEAM_FILTER_RANKINGS, TeamFilterRanking
from app.services.team_matchup_query import (
    LeagueMetricColumn,
    league_metric_column,
    publication_league_table,
)

#: The opponent box columns the panel renders and the ledger metric each one
#: reads.  ``OPP_OREB`` and ``OPP_DREB`` are served only by publications whose
#: format carries the rebound split; a publication without it omits those two
#: fields, which the panel renders as ``N/A``.  They are never sent as zero:
#: a fabricated zero is indistinguishable from a defense that allowed none.
_TRADITIONAL_FIELDS: dict[str, str] = {
    "OPP_PTS": "points",
    "OPP_FGM": "field_goals_made",
    "OPP_FGA": "field_goals_attempted",
    "OPP_FG3M": "three_pointers_made",
    "OPP_FG3A": "three_pointers_attempted",
    "OPP_FTA": "free_throws_attempted",
    "OPP_REB": "rebounds",
    "OPP_OREB": "offensive_rebounds",
    "OPP_DREB": "defensive_rebounds",
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

#: A rate no team in the league has: every field it would place is omitted.
_NO_RATE_COLUMN = LeagueMetricColumn(
    values={}, ranks={}, average=0.0, sigma=0.0
)


class TeamService:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        season_publications=None,
    ):
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

        if category not in _CATEGORIES:
            raise InvalidInputError(
                f"Unsupported team stats category: {category}."
            )
        base, profile, empty = _CATEGORIES[category]
        team_id = _resolve_team_id(team)
        rows = self._season_rows(base)
        if team_id is None or rows is None:
            return empty()
        return profile(publication_league_table(rows), team_id)

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
            return NBA_TEAM_TRICODE_TO_ID.get(
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
    """Rank one published rate over the teams that have a rate at all.

    A team that faced no possessions of a play type has no points per
    possession, which is not the same as allowing none: scoring it zero would
    both rank it as the stingiest defense and pull the league average that
    every other team's ratio is measured against.  It is excluded from the
    column instead, exactly as a Team Filter excludes it from its ranking, and
    the panel renders the missing fields as ``N/A``.
    """

    denominator = table[denominator_key].values
    rates = {
        team_id: value / denominator[team_id]
        for team_id, value in table[numerator_key].values.items()
        if denominator[team_id]
    }
    # No team faced this at all: there is no league average to rank against.
    return league_metric_column(rates) if rates else _NO_RATE_COLUMN


def _place(stats, field, column, team_id) -> None:
    """Write one column's value, rank, and distance from the league average."""

    if team_id not in column.values:
        return
    stats[field] = column.values[team_id]
    stats[f"{field}_RANK"] = column.rank(team_id)
    stats[f"{field}_vs_avg_pct"] = column.percent_vs_league_average(team_id)


def _place_ratio(stats, field, column, team_id) -> None:
    """Write one column as a ratio to the league average, plus its rank.

    The play-type and assist charts are centred on 1.0, so those categories
    carry the ratio rather than the per-48 value.  The rank stays on the
    published column, where a division can neither create nor break a tie.
    """

    if team_id not in column.values:
        return
    stats[field] = (
        column.values[team_id] / column.average if column.average else 0.0
    )
    stats[f"{field}_RANK"] = column.rank(team_id)


def _traditional_profile(table, team_id) -> dict:
    stats = {}
    fields = {metric: field for field, metric in _TRADITIONAL_FIELDS.items()}
    for _, label, column, definition, token, unit in opponent_profile_metrics("traditional", table):
        if unit == "percent":
            field = "OPP_FG_PCT" if label == "FG%" else "OPP_FG3_PCT"
        elif token == "OPP_STOCKS":
            field = "OPP_STL+BLK"
        else:
            field = fields[definition.numerator[0][0]]
        _place(stats, field, column, team_id)
    return stats


def _play_type_profile(table, team_id) -> dict:
    stats = {}
    for _, _, column, _, token, _ in opponent_profile_metrics("play_types", table):
        _place_ratio(stats, token, column, team_id)
    return stats


def _play_type_points_profile(table, team_id) -> dict:
    stats: dict = {}
    for play_type in PLAY_TYPES:
        _place(stats, play_type, table[f"{play_type}_PTS"], team_id)
    return stats


def _assist_profile(table, team_id) -> dict:
    stats = {}
    fields = {metric: field for field, metric in _ASSIST_FIELDS.items()}
    for _, _, column, definition, _, _ in opponent_profile_metrics("assist_locations", table):
        field = fields[definition.numerator[0][0]] if len(definition.numerator) == 1 else "AssistPoints"
        _place_ratio(stats, field, column, team_id)
    return stats


def _shot_zone_profile(table, team_id) -> dict:
    stats = {}
    for _, _, column, definition, _, _ in opponent_profile_metrics("shot_zones", table):
        zone, stat = definition.numerator[0][0].rsplit("_", 1)
        _place(stats, f"{zone}_OPP_{stat}", column, team_id)
    return stats


def _shot_type_profile(table, team_id) -> list:
    profiles = {slice_key: {"ShootingType": SHOT_TYPE_STORED_TO_DISPLAY[slice_key]} for slice_key in SHOT_TYPE_SLICES}
    for _, _, column, definition, _, _ in opponent_profile_metrics("shot_types", table):
        slice_key, stat = definition.numerator[0][0].rsplit("_", 1)
        if len(definition.numerator) > 1:
            stat = "PTS" if definition.numerator[0][1] == 2.0 else "FGA"
        _place(profiles[slice_key], stat, column, team_id)
    return list(profiles.values())


#: One row per panel category: the publication base it is served from, the
#: projection that maps the league table into the panel's field names, and the
#: shape an unpublished answer takes.
_CATEGORIES: dict[str, tuple[str, Callable, Callable]] = {
    "Traditional": ("traditional", _traditional_profile, dict),
    "Playtypes": ("play_types", _play_type_profile, dict),
    "Playtype Points": ("play_types", _play_type_points_profile, dict),
    "Assists": ("assist_locations", _assist_profile, dict),
    "Zone Shooting": ("shot_zones", _shot_zone_profile, dict),
    "Shooting Type": ("shot_types", _shot_type_profile, list),
}


def opponent_profile_metrics(base, table):
    """Project the same five categories as the Opposing Team Profile."""
    if base == "traditional":
        labels = {
            "OPP_PTS": "Points",
            "OPP_REB": "Rebounds",
            "OPP_AST": "Assists",
            "OPP_STL": "Steals",
            "OPP_BLK": "Blocks",
            "OPP_FTA": "Free throw attempts",
            "OPP_TOV": "Turnovers forced",
            "OPP_FG3M": "3s made",
            "OPP_FG3A": "3PT attempts",
            "OPP_FGM": "FG made",
            "OPP_FGA": "FG attempts",
            "OPP_OREB": "Off. rebounds",
            "OPP_DREB": "Def. rebounds",
        }
        for field, key in _TRADITIONAL_FIELDS.items():
            if key in table:
                yield (
                    "General",
                    labels[field],
                    table[key],
                    TeamFilterRanking(base, ((key, 1.0),)),
                    field if field in TEAM_FILTER_RANKINGS else None,
                    "count",
                )
        yield (
            "General",
            "Steals + blocks",
            _combined_column(table, (("steals", 1.0), ("blocks", 1.0))),
            TEAM_FILTER_RANKINGS["OPP_STOCKS"],
            "OPP_STOCKS",
            "count",
        )
        for label, numerator, denominator in [
            ("FG%", "field_goals_made", "field_goals_attempted"),
            ("3PT%", "three_pointers_made", "three_pointers_attempted"),
        ]:
            yield (
                "General",
                label,
                _rate_column(table, numerator, denominator),
                TeamFilterRanking(base, ((numerator, 1.0),), denominator),
                None,
                "percent",
            )
    elif base == "play_types":
        for key in PLAY_TYPES:
            yield (
                "Play type",
                {
                    "PRBallHandler": "P&R ball-handler",
                    "PRRollMan": "P&R roll-man",
                    "Spotup": "Spot-up",
                    "OffScreen": "Off-screen",
                    "Postup": "Post-up",
                    "OffRebound": "Putbacks",
                    "Misc": "Misc.",
                    "Cut": "Cuts",
                }.get(key, key)
                + " (per poss.)",
                _rate_column(table, key + "_PTS", key + "_POSS"),
                TEAM_FILTER_RANKINGS[key],
                key,
                "league_ratio",
            )
    elif base == "assist_locations":
        for field, key in _ASSIST_FIELDS.items():
            yield (
                "Assists",
                {
                    "Assists": "All assists",
                    "TwoPtAssists": "2PT assists",
                    "ThreePtAssists": "3PT assists",
                    "Arc3Assists": "Arc-3 assists",
                    "Corner3Assists": "Corner-3 assists",
                    "AtRimAssists": "At-rim assists",
                    "ShortMidRangeAssists": "Short mid assists",
                    "LongMidRangeAssists": "Long mid assists",
                }[field],
                table[key],
                TeamFilterRanking(base, ((key, 1.0),)),
                field if field in TEAM_FILTER_RANKINGS else None,
                "league_ratio",
            )
        terms = (("two_point_assists", 2.0), ("three_point_assists", 3.0))
        yield (
            "Assists",
            "Assist points",
            _combined_column(table, terms),
            TeamFilterRanking(base, terms),
            None,
            "league_ratio",
        )
    elif base == "shot_zones":
        for zone in SHOT_ZONE_SLICES:
            for stat in SHOT_ZONE_STATS:
                key = zone + "_" + stat
                yield (
                    "Zones",
                    zone + (" makes" if stat == "FGM" else " attempts"),
                    table[key],
                    TeamFilterRanking(base, ((key, 1.0),)),
                    None,
                    "count",
                )
    elif base == "shot_types":
        for shot in SHOT_TYPE_SLICES:
            columns = [(stat, ((shot + "_" + stat, 1.0),)) for stat in SHOT_TYPE_STATS]
            columns += [
                ("PTS", ((shot + "_FG2M", 2.0), (shot + "_FG3M", 3.0))),
                ("FGA", ((shot + "_FG2A", 1.0), (shot + "_FG3A", 1.0))),
            ]
            for stat, terms in columns:
                definition = TeamFilterRanking(base, terms)
                token = next(
                    (
                        name
                        for name, ranking in TEAM_FILTER_RANKINGS.items()
                        if ranking.base == definition.base
                        and dict(ranking.numerator) == dict(definition.numerator)
                        and ranking.denominator == definition.denominator
                    ),
                    None,
                )
                yield (
                    "Shot type",
                    {
                        "catch_and_shoot": "C&S",
                        "pullups": "Pull-up",
                        "less_than_10_ft": "Inside 10 ft",
                    }.get(shot, SHOT_TYPE_STORED_TO_DISPLAY[shot])
                    + " "
                    + {
                        "PTS": "points",
                        "FGA": "attempts",
                        "FG2M": "2s",
                        "FG2A": "2PA",
                        "FG3M": "3s",
                        "FG3A": "3PA",
                    }[stat],
                    _combined_column(table, terms),
                    definition,
                    token,
                    "count",
                )
