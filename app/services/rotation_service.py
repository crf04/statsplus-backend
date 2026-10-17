"""The ``GET /api/games/rotation`` read (crf04/statsplus#223).

Answers who starts, how many minutes a player plays and when, and what changed
recently, from the stored LiveData rotations.  Every average, count and
recent-versus-season comparison is computed here; the MCP only trims and
relabels.

*Recent* is the player's last ``last_n`` stored regular-season games for his
current team whose box score lists him, so a game he sat out (DNP or inactive)
counts as 0 minutes but a team game he is not listed for does not count.  *Season*
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
from app.services.athlete_resolver import catalog_athlete_matches, normalize_athlete_name
from app.services.rotation_derivation import clock_text

CS_PER_MINUTE = 6000
#: A ``without`` split with fewer games than this is returned but marked insufficient.
MIN_WITHOUT_GAMES = 3
_PERIOD_COLUMNS = {
    "Q1": "q1_cs",
    "Q2": "q2_cs",
    "Q3": "q3_cs",
    "Q4": "q4_cs",
    "OT": "ot_cs",
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


def _stint(stint: Mapping[str, Any]) -> dict[str, Any]:
    """One stored stint as the response shape both views share."""

    return {
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


# -- rotation split (crf04/statsplus#239) ------------------------------------
#
# A *without* game is a stored team game in which every named player has a
# box-score line for the team showing 0 minutes; a *with* game is one in which
# every named player's line shows more than 0.  A game where any named player
# has no line for the team, or the named players disagree, is neither.


def classify_games(
    lines_by_game: Mapping[str, Mapping[int, Mapping[str, Any]]],
    absent_ids: Sequence[int],
) -> tuple[list[str], list[str]]:
    """Game ids (in input order) where all of ``absent_ids`` sat, and where all played."""

    without: list[str] = []
    played: list[str] = []
    for game_id, lines in lines_by_game.items():
        if any(player_id not in lines for player_id in absent_ids):
            continue
        present = [lines[player_id]["minutes_cs"] > 0 for player_id in absent_ids]
        if not any(present):
            without.append(game_id)
        elif all(present):
            played.append(game_id)
    return without, played


def _side_counts(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`_counts`, with ``minutes`` null for a side with no games."""

    if not lines:
        return {"games": 0, "played": 0, "starts": 0, "minutes": None, "closed": 0}
    return _counts(lines)


def _side_detail(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`_side_counts` plus period minutes and stints per game."""

    if not lines:
        return {**_side_counts(lines), "minutes_by_period": None, "stints": None}
    summary = _summary(lines)
    return {
        "games": summary["games"],
        "played": summary["played"],
        "starts": summary["starts"],
        "minutes": summary["minutes"],
        "closed": summary["closed"],
        "minutes_by_period": summary["minutes_by_period"],
        "stints": summary["stints"],
    }


def _minutes_change(
    without: Sequence[Mapping[str, Any]], played: Sequence[Mapping[str, Any]]
) -> float | None:
    """Without minus with per-game minutes from exact values, rounded once."""

    if not without or not played:
        return None
    return _round1((_average_cs(without, "minutes_cs") - _average_cs(played, "minutes_cs")) / CS_PER_MINUTE)


def rotation_split(
    without_lines: Sequence[Mapping[str, Any]],
    with_lines: Sequence[Mapping[str, Any]],
    *,
    detail: bool = False,
) -> dict[str, Any]:
    """One subject's lines in the without games and the with games, side by side."""

    side = _side_detail if detail else _side_counts
    return {
        "without": side(without_lines),
        "with": side(with_lines),
        "minutes_change": _minutes_change(without_lines, with_lines),
        "insufficient": len(without_lines) < MIN_WITHOUT_GAMES,
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
        without: Sequence[str] = (),
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
            self.repository.player_games(season, player_ids=[athlete.player_id for athlete in namesakes])
        )
        with_data = [a for a in namesakes if rows_by_player.get(a.player_id)]
        if not with_data:
            raise ResourceNotFoundError(
                "The requested player has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        if len(with_data) > 1:
            raise InvalidInputError(
                "The player_name matches more than one player; " "narrow it with player_team.",
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
        if without:
            return self._player_without(season, athlete, own_rows, team, without)
        recent = self._recent(own_rows, team, last_n)
        stints = self.repository.stints(athlete.player_id, [row["game_id"] for row in recent])
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
                    "started": game["started"],
                    "minutes": _minutes(game["minutes_cs"]),
                    "closed": game["closed"],
                    "minutes_by_period": _period_minutes([game]),
                    "stints": [_stint(stint) for stint in stints.get(game["game_id"], ())],
                }
                for game in recent
            ],
        }

    def get_team_rotation(
        self, *, team: str, last_n: int, season: str | None, without: Sequence[str] = ()
    ) -> dict[str, Any]:
        self._tricode_id(team, "team")
        season = self._season(season)
        if without:
            return self._team_without(season, team, without)
        recent_games = self.repository.team_games(season, team)[:last_n]
        if not recent_games:
            raise ResourceNotFoundError(
                "The requested team has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        team_rows = self.repository.player_games(season, team=team)
        recent_ids = {game["game_id"] for game in recent_games}
        # Only players on the team's box score in one of its recent games:
        # players who left the team before them drop out.
        player_ids = sorted({row["player_id"] for row in team_rows if row["game_id"] in recent_ids})
        rows_by_player = self._rows_by_player(self.repository.player_games(season, player_ids=player_ids))
        starters = {game["game_id"]: [] for game in recent_games}
        for row in team_rows:
            if row["game_id"] in recent_ids and row["started"]:
                starters[row["game_id"]].append(row["player_name"])
        players = []
        for player_id in player_ids:
            own_rows = rows_by_player[player_id]
            recent = self._recent(own_rows, team, last_n)
            players.append(
                {
                    "name": own_rows[0]["player_name"],
                    "recent": _counts(recent),
                    "season_summary": _counts(own_rows),
                    "minutes_change": _round1(
                        (_average_cs(recent, "minutes_cs") - _average_cs(own_rows, "minutes_cs"))
                        / CS_PER_MINUTE
                    ),
                }
            )
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

    def get_game_rotation(self, *, game_id: str) -> dict[str, Any]:
        """Every box-score player's stints in one stored game, both teams."""

        game = self.repository.game(game_id)
        if game is None:
            raise ResourceNotFoundError(
                "The requested game has no rotation data.",
                public_details={"reason": "no_rotation_data"},
            )
        rows = self.repository.player_games(game["season"], game_ids=[game_id])
        stints = self.repository.game_stints(game_id)
        teams = []
        for team in (game["away_tricode"], game["home_tricode"]):
            lines = sorted(
                (row for row in rows if row["team_tricode"] == team),
                key=lambda row: (
                    not row["started"],
                    -row["minutes_cs"],
                    row["player_name"],
                    row["player_id"],
                ),
            )
            teams.append(
                {
                    "team": team,
                    "players": [
                        {
                            "name": row["player_name"],
                            "started": row["started"],
                            "minutes": _minutes(row["minutes_cs"]),
                            "closed": row["closed"],
                            "minutes_by_period": _period_minutes([row]),
                            "stints": [_stint(s) for s in stints.get(row["player_id"], ())],
                        }
                        for row in lines
                    ],
                }
            )
        return {
            "game": {
                "game_id": game["game_id"],
                "game_date": game["game_date"].isoformat(),
                "season": game["season"],
                "away": game["away_tricode"],
                "home": game["home_tricode"],
                "periods": game["periods"],
            },
            "teams": teams,
        }

    # -- the without split ----------------------------------------------

    def _player_without(
        self,
        season: str,
        athlete: Any,
        own_rows: Sequence[Mapping[str, Any]],
        team: str,
        without: Sequence[str],
    ) -> dict[str, Any]:
        lines_by_game, names = self._team_lines(season, team)
        absent = self._resolve_absent(without, names, team, subject_id=athlete.player_id)
        without_ids, with_ids = classify_games(lines_by_game, list(absent))
        subject = athlete.player_id
        without_lines = [lines_by_game[g][subject] for g in without_ids if subject in lines_by_game[g]]
        with_lines = [lines_by_game[g][subject] for g in with_ids if subject in lines_by_game[g]]
        return {
            "season": season,
            "player": {"name": athlete.display_name, "team": team},
            "without": {
                "absent": list(absent.values()),
                **rotation_split(without_lines, with_lines, detail=True),
                "games": [
                    {
                        "game_date": line["game_date"].isoformat(),
                        "matchup": _matchup(line, team),
                        "started": line["started"],
                        "minutes": _minutes(line["minutes_cs"]),
                        "closed": line["closed"],
                    }
                    for line in without_lines
                ],
            },
        }

    def _team_without(self, season: str, team: str, without: Sequence[str]) -> dict[str, Any]:
        lines_by_game, names = self._team_lines(season, team)
        if not lines_by_game:
            raise ResourceNotFoundError(
                "The requested team has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        absent = self._resolve_absent(without, names, team, subject_id=None)
        without_ids, with_ids = classify_games(lines_by_game, list(absent))
        sides: dict[int, tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]] = {}
        for game_id in without_ids:
            for player_id, line in lines_by_game[game_id].items():
                if player_id not in absent:
                    sides.setdefault(player_id, ([], []))[0].append(line)
        for game_id in with_ids:
            for player_id, line in lines_by_game[game_id].items():
                if player_id in sides:
                    sides[player_id][1].append(line)
        players = [
            {
                "name": names[player_id],
                **rotation_split(without_lines, with_lines),
            }
            for player_id, (without_lines, with_lines) in sides.items()
        ]
        players.sort(
            key=lambda player: (
                player["minutes_change"] is None,
                -abs(player["minutes_change"] or 0.0),
                player["name"],
            )
        )
        starters = [
            {
                "name": names[player_id],
                "starts_without": sum(1 for line in without_lines if line["started"]),
                "starts_with": sum(1 for line in with_lines if line["started"]),
            }
            for player_id, (without_lines, with_lines) in sides.items()
            if any(line["started"] for line in without_lines)
        ]
        starters.sort(key=lambda entry: (-entry["starts_without"], entry["name"]))
        return {
            "season": season,
            "team": team,
            "without": {
                "absent": list(absent.values()),
                "games_without": len(without_ids),
                "games_with": len(with_ids),
                "insufficient": len(without_ids) < MIN_WITHOUT_GAMES,
                "starters": starters,
                "players": players,
            },
        }

    def _team_lines(
        self, season: str, team: str
    ) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[int, str]]:
        """A team's stored box-score lines by game (newest first) and player, plus names."""

        lines_by_game: dict[str, dict[int, dict[str, Any]]] = {}
        names: dict[int, str] = {}
        for row in self.repository.player_games(season, team=team):
            lines_by_game.setdefault(row["game_id"], {})[row["player_id"]] = row
            names.setdefault(row["player_id"], row["player_name"])
        return lines_by_game, names

    @staticmethod
    def _resolve_absent(
        requested: Sequence[str],
        names: Mapping[int, str],
        team: str,
        *,
        subject_id: int | None,
    ) -> dict[int, str]:
        """Named ``without`` players as ``{player_id: name}``, in request order."""

        def invalid(message: str, **details: Any) -> InvalidInputError:
            return InvalidInputError(
                message,
                public_details={"parameter": "without", "parameters": ["without"], **details},
            )

        absent: dict[int, str] = {}
        for typed in requested:
            target = normalize_athlete_name(typed)
            matches = sorted(
                (name, player_id)
                for player_id, name in names.items()
                if normalize_athlete_name(name) == target
            )
            if not target or not matches:
                raise invalid("The without player was not found on the team's box scores for the season.")
            if len(matches) > 1:
                raise invalid(
                    "The without name matches more than one player.",
                    candidates=[{"name": name, "team": team} for name, _ in matches],
                )
            ((name, player_id),) = matches
            if player_id == subject_id:
                raise invalid("The without player cannot be the player_name player.")
            absent[player_id] = name
        return absent

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
    def _recent(own_rows: Sequence[Mapping[str, Any]], team: str, last_n: int) -> list[dict[str, Any]]:
        """His last ``last_n`` games for ``team`` whose box score lists him.

        Team games he is not on the box score for are not his games and never
        count as 0 minutes; a DNP or an inactive listing does.  ``own_rows``
        are newest first.
        """

        return [dict(row) for row in own_rows if row["team_tricode"] == team][:last_n]


__all__ = ["MIN_WITHOUT_GAMES", "RotationService", "classify_games", "rotation_split"]
