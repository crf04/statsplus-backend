"""Next scheduled opponent and Season profile ranks for the Log Workspace."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.nba_events import (
    is_all_star_kind,
    is_final_event,
    is_postponed_event,
    resolve_stored_event_classification,
)
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    PLAY_TYPES,
    SHOT_TYPE_SLICES,
    SHOT_ZONE_SLICES,
    SHOT_ZONE_STATS,
    THREE_POINT_SHOT_ZONES,
)
from app.domain.utc import parse_utc_iso
from app.errors import ResourceNotFoundError
from app.services.research_season import schedule_season
from app.services.team_filter_rankings import (
    TEAM_FILTER_PUBLICATION_STREAM_KEYS,
    team_filter_ranking,
)
from app.services.team_matchup_query import publication_league_table
from app.services.team_service import opponent_profile_metrics


def _metric_families():
    def key(base, *metrics, per=None):
        return base, frozenset(metrics), per

    families = {
        key("traditional", "points"): ("points", ("PTS",)),
        key("traditional", "rebounds"): ("rebounds", ("REB",)),
        key("traditional", "offensive_rebounds"): ("rebounds", ("REB",)),
        key("traditional", "defensive_rebounds"): ("rebounds", ("REB",)),
        key("play_types", "OffRebound_PTS"): ("rebounds", ("PTS", "REB")),
        key("traditional", "assists"): ("assists", ("AST",)),
        key("assist_locations", "assists"): ("assists", ("AST",)),
        key("assist_locations", "two_point_assists", "three_point_assists"): ("assists", ("AST",)),
        key("traditional", "steals"): ("steals-blocks", ("STL",)),
        key("traditional", "blocks"): ("steals-blocks", ("BLK",)),
        key("traditional", "steals", "blocks"): ("steals-blocks", ("STL", "BLK")),
        key("traditional", "turnovers"): ("turnovers", ("TOV",)),
        key("traditional", "free_throws_attempted"): ("free-throws", ("FTA", "FTM", "PTS")),
        key("traditional", "field_goals_made"): ("field-goals", ("PTS",)),
        key("traditional", "field_goals_attempted"): ("field-goals", ("PTS",)),
        key("traditional", "field_goals_made", per="field_goals_attempted"): ("field-goals", ("PTS",)),
        key("traditional", "three_pointers_made"): ("threes", ("FG3M", "PTS")),
        key("traditional", "three_pointers_attempted"): ("threes", ("FG3M", "PTS")),
        key("traditional", "three_pointers_made", per="three_pointers_attempted"): (
            "threes",
            ("FG3M", "PTS"),
        ),
    }
    for metric in ("two_point", "at_rim", "short_mid_range", "long_mid_range"):
        families[key("assist_locations", metric + "_assists")] = ("2pt-assists", ("AST",))
    for metric in ("three_point", "arc3", "corner3"):
        families[key("assist_locations", metric + "_assists")] = ("3pt-assists", ("AST",))
    for play_type in PLAY_TYPES:
        families.setdefault(key("play_types", play_type + "_PTS"), (f"play-type:{play_type}", ("PTS",)))
    for zone in SHOT_ZONE_SLICES:
        family = "inside" if zone in ("Restricted Area", "In The Paint (Non-RA)") else f"zone:{zone}"
        markets = ("PTS", "FG3M") if zone in THREE_POINT_SHOT_ZONES else ("PTS",)
        for stat in SHOT_ZONE_STATS:
            families[key("shot_zones", f"{zone}_{stat}")] = (family, markets)
    for shot in SHOT_TYPE_SLICES:
        family = "inside" if shot == "less_than_10_ft" else f"shot-type:{shot}"
        threes = ("PTS",) if family == "inside" else ("PTS", "FG3M")
        for metrics, markets in (
            ((f"{shot}_FG2M",), ("PTS",)),
            ((f"{shot}_FG2A",), ("PTS",)),
            ((f"{shot}_FG3M",), threes),
            ((f"{shot}_FG3A",), threes),
            ((f"{shot}_FG2M", f"{shot}_FG3M"), ("PTS",)),
            ((f"{shot}_FG2A", f"{shot}_FG3A"), ("PTS",)),
        ):
            families[key("shot_types", *metrics)] = (family, markets)
    return families


_METRIC_FAMILIES = _metric_families()


def metric_family(definition):
    """The family a profile metric shares with its near-twins, and the stats it bears on."""
    return _METRIC_FAMILIES[
        definition.base,
        frozenset(metric for metric, _ in definition.numerator),
        definition.denominator,
    ]


class NextOpponentService:
    def __init__(self, game, events, rankings, *, clock=None):
        self.game = game
        self.events = events
        self.rankings = rankings
        self.clock = clock or (lambda: datetime.now(UTC))

    def _team_id(self, player_id, season):
        catalog = (
            self.game.athlete_catalog.get_catalog(season, active_only=False)
            if self.game.athlete_catalog
            else ()
        )
        athlete = next((row for row in catalog if int(row["player_id"]) == player_id), None)
        return athlete.get("team_id") if athlete else None

    def get_next_opponent(self, player_name):
        # Who the player is and how opponents rank come from the published
        # season; the next game comes from the schedule covering today.
        season = self.game.default_season()
        try:
            player_id = self.game.get_player_id(player_name, season)
        except ValueError as error:
            raise ResourceNotFoundError("The requested player was not found.", detail=error) from error
        empty = {"next_game": None, "opponent_ranks": []}
        if self.events is None:
            return empty
        now = self.clock()
        schedule = schedule_season(
            self.game.settings, self.events, now.astimezone(ZoneInfo("America/New_York")).date()
        )
        # The team comes from the schedule's season, so a player traded or
        # signed since the published season meets his new team's opponent.
        team_id = self._team_id(player_id, schedule)
        if team_id is None and schedule != season:
            team_id = self._team_id(player_id, season)
        if team_id is None:
            return empty
        candidates = []
        for event in self.events.get_events(schedule):
            at = parse_utc_iso(event["scheduled_at"])
            # The collected schedule can leave a future game without a status
            # code, so anything not yet final or postponed is still to come.
            if (
                at < now
                or is_final_event(event)
                or is_postponed_event(event)
                or is_all_star_kind(
                    resolve_stored_event_classification(
                        str(event["nba_game_id"]),
                        str(event.get("classification") or ""),
                    ).kind
                )
            ):
                continue
            if team_id in (event["home_team"]["id"], event["away_team"]["id"]):
                candidates.append((at, event["nba_game_id"], event))
        if not candidates:
            return empty
        at, game_id, event = min(candidates, key=lambda item: item[:2])
        home = team_id == event["home_team"]["id"]
        opponent = event["away_team" if home else "home_team"]
        ranks = []
        reader = self.rankings.publication_reader
        if reader is not None and opponent["id"] in NBA_TEAM_ID_TO_TRICODE:
            snapshot = reader.snapshot(tuple(TEAM_FILTER_PUBLICATION_STREAM_KEYS), season=season)
            rows_by_base = self.rankings.season_rows_by_base(
                (
                    "traditional",
                    "play_types",
                    "assist_locations",
                    "shot_zones",
                    "shot_types",
                ),
                season,
                publication_snapshot=snapshot,
            )
            emitted = set()
            for base, rows in rows_by_base.items():
                if rows is None:
                    continue
                for (
                    group,
                    label,
                    column,
                    definition,
                    token,
                    unit,
                ) in opponent_profile_metrics(base, publication_league_table(rows)):
                    # Every team ties on a column that cannot vary (no 3s
                    # inside 10 ft), so its "1st most" says nothing.
                    if len(set(column.values.values())) < 2:
                        continue
                    # A copy of a column already emitted (Inside 10 ft attempts
                    # once its 3PA all tie) says nothing new.
                    values = frozenset(column.values.items())
                    if values in emitted:
                        continue
                    emitted.add(values)
                    ordering = self.rankings.rank_definition(
                        token or label,
                        team_filter_ranking(token) if token else definition,
                        rows,
                    )
                    tricode = NBA_TEAM_ID_TO_TRICODE[opponent["id"]]
                    if tricode not in ordering or opponent["id"] not in column.values:
                        continue
                    value = column.values[opponent["id"]]
                    if unit == "league_ratio":
                        value = value / column.average if column.average else 0.0
                    family, markets = metric_family(definition)
                    ranks.append(
                        dict(
                            group=group,
                            label=label,
                            value=value,
                            vs_league_pct=column.percent_vs_league_average(opponent["id"]),
                            most_rank=ordering.index(tricode) + 1,
                            ranked_teams=len(ordering),
                            team_filter=token,
                            unit=unit,
                            family=family,
                            markets=list(markets),
                        )
                    )
        return {
            "next_game": dict(
                game_id=game_id,
                date=at.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
                opponent=opponent["tricode"],
                opponent_name=opponent["name"],
                home=home,
            ),
            "opponent_ranks": ranks,
        }
