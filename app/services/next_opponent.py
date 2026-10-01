"""Next scheduled opponent and Season profile ranks for the Log Workspace."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.domain.nba_events import (
    is_postponed_event,
    is_all_star_kind,
    resolve_stored_event_classification,
)
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.utc import parse_utc_iso
from app.errors import ResourceNotFoundError
from app.services.team_filter_rankings import (
    TEAM_FILTER_PUBLICATION_STREAM_KEYS,
    TEAM_FILTER_RANKINGS,
)
from app.services.team_matchup_query import publication_league_table
from app.services.team_service import opponent_profile_metrics


class NextOpponentService:
    def __init__(self, game, events, rankings, *, clock=None):
        self.game = game
        self.events = events
        self.rankings = rankings
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get_next_opponent(self, player_name):
        season = self.game.settings.nba.current_season
        try:
            player_id = self.game.get_player_id(player_name, season)
        except ValueError as error:
            raise ResourceNotFoundError(
                "The requested player was not found.", detail=error
            ) from error
        catalog = (
            self.game.athlete_catalog.get_catalog(season, active_only=False)
            if self.game.athlete_catalog
            else ()
        )
        athlete = next(
            (row for row in catalog if int(row["player_id"]) == player_id), None
        )
        team_id = athlete.get("team_id") if athlete else None
        empty = {"next_game": None, "opponent_ranks": []}
        if team_id is None or self.events is None:
            return empty
        now = self.clock()
        candidates = []
        for event in self.events.get_events(season):
            at = parse_utc_iso(event["scheduled_at"])
            if (
                at < now
                or event.get("status_code") != 1
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
            snapshot = reader.snapshot(
                tuple(TEAM_FILTER_PUBLICATION_STREAM_KEYS), season=season
            )
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
                    ordering = self.rankings.rank_definition(
                        token or label,
                        TEAM_FILTER_RANKINGS[token] if token else definition,
                        rows,
                    )
                    tricode = NBA_TEAM_ID_TO_TRICODE[opponent["id"]]
                    if tricode not in ordering or opponent["id"] not in column.values:
                        continue
                    value = column.values[opponent["id"]]
                    if unit == "league_ratio":
                        value = value / column.average if column.average else 0.0
                    ranks.append(
                        dict(
                            group=group,
                            label=label,
                            value=value,
                            vs_league_pct=column.percent_vs_league_average(
                                opponent["id"]
                            ),
                            most_rank=ordering.index(tricode) + 1,
                            ranked_teams=len(ordering),
                            team_filter=token,
                            unit=unit,
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
