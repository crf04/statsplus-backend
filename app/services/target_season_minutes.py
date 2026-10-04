"""Opponent roster minutes from stored season game logs."""

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.errors import InvalidInputError
from app.services.backtest_season import BacktestSeasons
from app.services.publication_snapshot_calls import call_with_read_scope

_GAME_LOGS = ("player_game_logs",)


class TargetSeasonMinutesService:
    def __init__(self, *, player_logs, settings, publication_reader=None):
        self.player_logs = player_logs
        self.settings = settings
        self.publication_reader = publication_reader
        self.seasons = BacktestSeasons(settings, publication_reader)

    def get(self, tricode, *, season=None):
        """The team's roster minutes in the season its Backtest would read.

        ``season`` takes the Backtest's values, default, and fallback (#104),
        so a defender is chosen from the season the Backtest reads; a past
        season reads its retained game logs.
        """

        tricode = tricode.strip().upper()
        if tricode not in NBA_TEAM_TRICODE_TO_ID:
            raise InvalidInputError("The team must be a canonical NBA tricode.")
        # The season and the rows come from one capture.
        choice, snapshot = self.seasons.capture(
            season,
            lambda season: self.publication_reader.snapshot(
                _GAME_LOGS,
                season=season,
                projection_only_keys=frozenset(_GAME_LOGS),
            ),
            _GAME_LOGS,
            projection_only_keys=frozenset(_GAME_LOGS),
        )
        players = {}
        for row in call_with_read_scope(
            self.player_logs.list_team_rows,
            choice.season,
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
            "season": choice.season,
            "season_reason": choice.reason,
            "published_season": choice.published,
            "players": sorted(
                players.values(),
                key=lambda item: (-item["average_minutes"], item["player_id"]),
            ),
        }
