"""The ``GET /api/games/rotation`` read (crf04/statsplus#223).

Answers who starts, how many minutes a player plays and when, and what changed
recently, from the stored LiveData rotations.  Every average, count and
recent-versus-season comparison is computed here; the MCP only trims and
relabels.

*Recent* is the team's last ``last_n`` stored regular-season games, so a game a
player sat out (or was not on the roster for) counts as 0 minutes.  *Season*
is all of the player's own stored games in the season, on any team.  Averages
are exact fractions of the stored centiseconds, rounded half-up to one decimal
only for display.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.errors import InvalidInputError, ResourceNotFoundError
from app.services.athlete_resolver import catalog_athlete_matches
from app.services.rotation_derivation import clock_text

CS_PER_MINUTE = 6000
_PERIOD_COLUMNS = {
    "Q1": "q1_cs", "Q2": "q2_cs", "Q3": "q3_cs", "Q4": "q4_cs", "OT": "ot_cs",
}


def _round1(value: Fraction) -> float:
    """A fraction rounded half-up to one decimal."""

    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return float(exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) + 0.0


def _minutes(centiseconds: int) -> float:
    return _round1(Fraction(centiseconds, CS_PER_MINUTE))


def _average_minutes(total_centiseconds: int, games: int) -> float:
    return _round1(Fraction(total_centiseconds, CS_PER_MINUTE * games))


def _average_cs(lines: Sequence[Mapping[str, Any]], column: str) -> Fraction:
    return Fraction(sum(line[column] for line in lines), len(lines))


def _period_minutes(lines: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    return {
        key: _average_minutes(sum(line[column] for line in lines), len(lines))
        for key, column in _PERIOD_COLUMNS.items()
    }


def _matchup(game: Mapping[str, Any], team: str) -> str:
    """``DET @ CHA`` for an away game, ``DET vs. CHA`` for a home game."""

    if game["home_tricode"] == team:
        return f"{team} vs. {game['away_tricode']}"
    return f"{team} @ {game['home_tricode']}"


def _counts(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "games": len(lines),
        "played": sum(1 for line in lines if line["minutes_cs"] > 0),
        "starts": sum(1 for line in lines if line["started"]),
        "minutes": _average_minutes(sum(line["minutes_cs"] for line in lines), len(lines)),
        "closed": sum(1 for line in lines if line["closed"]),
    }


def _summary(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts and per-game averages over ``lines`` (DNPs count as 0)."""

    counts = _counts(lines)
    return {
        "games": counts["games"],
        "played": counts["played"],
        "starts": counts["starts"],
        "minutes": counts["minutes"],
        "stints": _round1(Fraction(sum(line["stint_count"] for line in lines), len(lines))),
        "closed": counts["closed"],
        "minutes_by_period": _period_minutes(lines),
    }


class RotationService:
    """Compose the player and team rotation documents from stored rows."""

    def __init__(
        self,
        repository: Any,
        athlete_catalog: Any | None,
        default_season: Callable[[], str],
    ) -> None:
        self.repository = repository
        self.athlete_catalog = athlete_catalog
        self._default_season = default_season

    # -- public reads ---------------------------------------------------

    def get_player_rotation(
        self,
        *,
        player_name: str,
        player_team: str | None,
        last_n: int,
        season: str | None,
    ) -> dict[str, Any]:
        player_team_id = self._tricode_id(player_team, "player_team")
        season = self._season(season)
        catalog = (
            ()
            if self.athlete_catalog is None
            else tuple(self.athlete_catalog.get_catalog(season, active_only=False))
        )
        namesakes = [
            athlete
            for athlete in catalog_athlete_matches(catalog, player_name)
            if player_team_id is None or athlete.team_id == player_team_id
        ]
        if not namesakes:
            raise ResourceNotFoundError("The requested player was not found.")
        rows_by_player = self._rows_by_player(
            self.repository.player_games(
                season, player_ids=[athlete.player_id for athlete in namesakes]
            )
        )
        with_data = [a for a in namesakes if rows_by_player.get(a.player_id)]
        if not with_data:
            raise ResourceNotFoundError(
                "The requested player has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        if len(with_data) > 1:
            raise InvalidInputError(
                "The player_name matches more than one player; "
                "narrow it with player_team.",
                public_details={
                    "parameter": "player_name",
                    "parameters": ["player_name"],
                    "candidates": sorted(
                        (
                            {
                                "name": athlete.display_name,
                                "team": rows_by_player[athlete.player_id][0]["team_tricode"],
                            }
                            for athlete in with_data
                        ),
                        key=lambda candidate: (candidate["name"], candidate["team"]),
                    ),
                },
            )
        athlete = with_data[0]
        own_rows = rows_by_player[athlete.player_id]
        team = own_rows[0]["team_tricode"]
        recent_games = self.repository.team_games(season, team)[:last_n]
        recent = [self._game_line(game, team, own_rows) for game in recent_games]
        stints = self.repository.stints(
            athlete.player_id, [game["game_id"] for game in recent_games]
        )
        return {
            "season": season,
            "last_n": last_n,
            "player": {"name": athlete.display_name, "team": team},
            "recent": _summary(recent),
            "season_summary": _summary(own_rows),
            "games": [
                {
                    "game_date": game["game_date"].isoformat(),
                    "matchup": _matchup(game, team),
                    "started": line["started"],
                    "minutes": _minutes(line["minutes_cs"]),
                    "closed": line["closed"],
                    "minutes_by_period": _period_minutes([line]),
                    "stints": [
                        {
                            "in": {
                                "period": stint["in_period"],
                                "clock": clock_text(stint["in_clock_cs"]),
                            },
                            "out": {
                                "period": stint["out_period"],
                                "clock": clock_text(stint["out_clock_cs"]),
                            },
                            "minutes": _minutes(stint["minutes_cs"]),
                        }
                        for stint in stints.get(game["game_id"], ())
                    ],
                }
                for game, line in zip(recent_games, recent)
            ],
        }

    def get_team_rotation(
        self, *, team: str, last_n: int, season: str | None
    ) -> dict[str, Any]:
        self._tricode_id(team, "team")
        season = self._season(season)
        recent_games = self.repository.team_games(season, team)[:last_n]
        if not recent_games:
            raise ResourceNotFoundError(
                "The requested team has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        team_rows = self.repository.player_games(season, team=team)
        player_ids = sorted({row["player_id"] for row in team_rows})
        rows_by_player = self._rows_by_player(
            self.repository.player_games(season, player_ids=player_ids)
        )
        recent_ids = {game["game_id"] for game in recent_games}
        starters = {game["game_id"]: [] for game in recent_games}
        for row in team_rows:
            if row["game_id"] in recent_ids and row["started"]:
                starters[row["game_id"]].append(row["player_name"])
        players = []
        for player_id in player_ids:
            own_rows = rows_by_player[player_id]
            recent = [self._game_line(game, team, own_rows) for game in recent_games]
            players.append({
                "name": own_rows[0]["player_name"],
                "recent": _counts(recent),
                "season_summary": _counts(own_rows),
                "minutes_change": _round1(
                    (_average_cs(recent, "minutes_cs") - _average_cs(own_rows, "minutes_cs"))
                    / CS_PER_MINUTE
                ),
            })
        players.sort(key=lambda player: (-abs(player["minutes_change"]), player["name"]))
        return {
            "season": season,
            "last_n": last_n,
            "team": team,
            "games": [
                {
                    "game_date": game["game_date"].isoformat(),
                    "matchup": _matchup(game, team),
                    "starters": sorted(starters[game["game_id"]]),
                }
                for game in recent_games
            ],
            "players": players,
        }

    # -- helpers --------------------------------------------------------

    def _season(self, requested: str | None) -> str:
        seasons = self.repository.seasons()
        if requested is not None:
            if requested not in seasons:
                raise ResourceNotFoundError(
                    "There is no rotation data for the requested season.",
                    public_details={"reason": "no_rotation_data"},
                )
            return requested
        current = self._default_season()
        if current in seasons:
            return current
        if seasons:
            return seasons[0]
        raise ResourceNotFoundError(
            "There is no rotation data yet.",
            public_details={"reason": "no_rotation_data"},
        )

    @staticmethod
    def _tricode_id(tricode: str | None, parameter: str) -> int | None:
        if tricode is None:
            return None
        team_id = NBA_TEAM_TRICODE_TO_ID.get(tricode)
        if team_id is None:
            raise InvalidInputError(
                f"The {parameter} is not an NBA tricode.",
                public_details={"parameter": parameter, "parameters": [parameter]},
            )
        return team_id

    @staticmethod
    def _rows_by_player(rows: Iterable[Mapping[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["player_id"], []).append(dict(row))
        return grouped

    @staticmethod
    def _game_line(
        game: Mapping[str, Any], team: str, own_rows: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """The player's line in one of ``team``'s games; a 0-minute line if absent."""

        for row in own_rows:
            if row["game_id"] == game["game_id"] and row["team_tricode"] == team:
                return dict(row)
        return {
            "game_id": game["game_id"], "started": False, "minutes_cs": 0,
            "closed": False, "stint_count": 0,
            **{column: 0 for column in _PERIOD_COLUMNS.values()},
        }


__all__ = ["RotationService"]
