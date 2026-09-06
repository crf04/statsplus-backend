"""Opponent roster minutes from stored season game logs."""

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.errors import InvalidInputError
from app.services.publication_snapshot_calls import call_with_read_scope


class TargetSeasonMinutesService:
    def __init__(self, *, player_logs, settings, publication_reader=None):
        self.player_logs = player_logs
        self.settings = settings
        self.publication_reader = publication_reader

    def get(self, tricode):
        tricode = tricode.strip().upper()
        if tricode not in NBA_TEAM_TRICODE_TO_ID:
            raise InvalidInputError("The team must be a canonical NBA tricode.")
        season = self.settings.nba.current_season
        snapshot = (
            self.publication_reader.snapshot(
                ("player_game_logs",),
                season=season,
                projection_only_keys=frozenset({"player_game_logs"}),
            )
            if self.publication_reader
            else None
        )
        players = {}
        for row in call_with_read_scope(
            self.player_logs.list_team_rows,
            season,
            NBA_TEAM_TRICODE_TO_ID[tricode],
            publication_snapshot=snapshot,
        ):
            if row.season_type != "Regular Season":
                continue
            item = players.setdefault(
                row.player_id,
                {
                    "player_id": row.player_id,
                    "name": row.player_name,
                    "games_played": 0,
                    "average_minutes": 0.0,
                },
            )
            item["games_played"] += 1
            item["average_minutes"] += row.minutes
        for item in players.values():
            item["average_minutes"] = round(
                item["average_minutes"] / item["games_played"], 6
            )
        return {
            "season": season,
            "players": sorted(
                players.values(),
                key=lambda item: (-item["average_minutes"], item["player_id"]),
            ),
        }
