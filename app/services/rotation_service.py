"""The ``GET /api/games/rotation`` read (crf04/statsplus#223).

Answers who starts, how many minutes a player plays and when, and what changed
recently, from the stored LiveData rotations.  Every average, count and
recent-versus-season comparison is computed here; the MCP only trims and
relabels.

*Recent* is his current team's last ``last_n`` stored regular-season games
inside his team window (crf04/statsplus#449, #483), and *Season* is every team
game inside his window for each team he played for.  A game he has no box-score
line in (2026-27 box scores list only active players) is a missed game, not
skipped.  Minutes average over the games he played.  Averages are exact
fractions of the stored centiseconds, rounded half-up to one decimal only for
display.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from functools import cache
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.domain.team_window import Membership, TeamWindow, team_window
from app.errors import InvalidInputError, ResourceNotFoundError
from app.services.athlete_resolver import catalog_athlete_matches, normalize_athlete_name
from app.services.injury_listing import listed_entry, report_status
from app.services.research_season import season_scoped
from app.services.rotation_derivation import clock_text
from app.services.team_membership import TeamMembership

logger = logging.getLogger(__name__)

CS_PER_MINUTE = 6000
#: A split with fewer asked games than this is returned but marked insufficient.
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


def _missed(game: Mapping[str, Any], team: str) -> dict[str, Any]:
    """A team game he has no box-score line in, as a 0-minute entry."""

    return {
        "game_id": game["game_id"],
        "game_date": game["game_date"],
        "home_tricode": game["home_tricode"],
        "away_tricode": game["away_tricode"],
        "team_tricode": team,
        "started": False,
        "minutes_cs": 0,
        "closed": False,
        "stint_count": 0,
        "on_box_score": False,
        **{column: 0 for column in _PERIOD_COLUMNS.values()},
    }


def _report_fields(entry: Mapping[str, Any] | None) -> dict[str, str | None]:
    """A missed game's status and reason from its stored report; null when the report does not list him."""

    if entry is None:
        return {"injury_status": None, "injury_reason": None}
    return {"injury_status": report_status(entry), "injury_reason": str(entry.get("reason") or "")}


def _team_entries(
    own_rows: Sequence[Mapping[str, Any]], team: str, team_games: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """His lines for ``team`` plus each team game inside his window he has no line in, newest first."""

    window = team_window(((row["game_date"], row["team_tricode"]) for row in own_rows), team, Membership())
    listed = {row["game_id"] for row in own_rows}
    entries = [{**row, "on_box_score": True} for row in own_rows if row["team_tricode"] == team]
    entries += [
        _missed(game, team)
        for game in team_games
        if game["game_id"] not in listed and window is not None and window.contains(game["game_date"])
    ]
    return sorted(entries, key=lambda entry: (entry["game_date"], entry["game_id"]), reverse=True)


def _played_average(entries: Sequence[Mapping[str, Any]], column: str) -> Fraction | None:
    played = [entry for entry in entries if entry["minutes_cs"] > 0]
    return Fraction(sum(entry[column] for entry in played), len(played)) if played else None


def _window_counts(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Team games, games played and starts and closes over ``entries``; minutes per game played."""

    minutes = _played_average(entries, "minutes_cs")
    return {
        "games": len(entries),
        "played": sum(1 for entry in entries if entry["minutes_cs"] > 0),
        "starts": sum(1 for entry in entries if entry["started"]),
        "minutes": None if minutes is None else _round1(minutes / CS_PER_MINUTE),
        "closed": sum(1 for entry in entries if entry["closed"]),
    }


def _window_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`_window_counts` plus stints and period minutes per game played."""

    stints = _played_average(entries, "stint_count")
    periods = {key: _played_average(entries, column) for key, column in _PERIOD_COLUMNS.items()}
    counts = _window_counts(entries)
    return {
        "games": counts["games"],
        "played": counts["played"],
        "starts": counts["starts"],
        "minutes": counts["minutes"],
        "stints": None if stints is None else _round1(stints),
        "closed": counts["closed"],
        "minutes_by_period": (
            {key: _round1(value / CS_PER_MINUTE) for key, value in periods.items()}
            if counts["played"]
            else None
        ),
    }


def _window_minutes_change(
    recent: Sequence[Mapping[str, Any]], season: Sequence[Mapping[str, Any]]
) -> float | None:
    """Recent minus season minutes per game played, from exact values; ``None`` without a played game."""

    recent_minutes = _played_average(recent, "minutes_cs")
    season_minutes = _played_average(season, "minutes_cs")
    if recent_minutes is None or season_minutes is None:
        return None
    return _round1((recent_minutes - season_minutes) / CS_PER_MINUTE)


# -- rotation split (crf04/statsplus#239, #499) -----------------------------
#
# A named player *played* a stored team game inside his team window
# (crf04/statsplus#449) when his line shows more than 0, and *missed* it when
# he has no line or a 0-minute line.  The *asked* games are those every
# ``with`` player played and every ``without`` player missed.  With one list
# the *other* games are those where every named player has the opposite status
# and a game where they disagree is *mixed*; with both lists every other game
# is an other game.  A game outside any named player's window is neither.
# Membership evidence opens a window before a player's first game for the team
# (crf04/statsplus#450).


def absent_windows(
    repository: Any,
    season: str,
    team: str,
    absent_ids: Iterable[int],
    membership: TeamMembership,
    lines_by_game: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> dict[int, TeamWindow | None]:
    """Each absent player's team window for ``team``, from his stored lines for every team."""

    appearances: dict[int, list[tuple[Any, str]]] = {player_id: [] for player_id in absent_ids}
    for row in repository.player_games(season, player_ids=list(appearances)):
        appearances[row["player_id"]].append((row["game_date"], row["team_tricode"]))
    days = {next(iter(lines.values()))["game_date"] for lines in lines_by_game.values()}
    windows = membership.windows(
        season, {player_id: (rows, {team: days}) for player_id, rows in appearances.items()}
    )
    return {player_id: by_team[team] for player_id, by_team in windows.items()}


def split_games(
    lines_by_game: Mapping[str, Mapping[int, Mapping[str, Any]]],
    present: Mapping[int, TeamWindow | None],
    absent: Mapping[int, TeamWindow | None],
) -> tuple[list[str], list[str], int]:
    """The asked and other game ids (in input order) for the ``with`` and ``without`` windows, and the mixed count."""

    windows = {**present, **absent}
    asked: list[str] = []
    other: list[str] = []
    mixed = 0
    for game_id, lines in lines_by_game.items():
        day = next(iter(lines.values()))["game_date"]
        if not all(window is not None and window.contains(day) for window in windows.values()):
            continue
        played = {
            player_id: player_id in lines and lines[player_id]["minutes_cs"] > 0 for player_id in windows
        }
        if all(played[p] for p in present) and not any(played[p] for p in absent):
            asked.append(game_id)
        elif (present and absent) or (not any(played[p] for p in present) and all(played[p] for p in absent)):
            other.append(game_id)
        else:
            mixed += 1
    return asked, other, mixed


def _side_counts(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`_counts`, with ``minutes`` null for a side with no games, and
    ``minutes_per_game_played`` (null when he played none)."""

    if not lines:
        return {
            "games": 0,
            "played": 0,
            "starts": 0,
            "minutes": None,
            "minutes_per_game_played": None,
            "closed": 0,
        }
    counts = _counts(lines)
    played = counts["played"]
    total = sum(line["minutes_cs"] for line in lines)
    return {**counts, "minutes_per_game_played": _average_minutes(total, played) if played else None}


def _side_detail(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`_side_counts` plus period minutes and stints per game."""

    if not lines:
        return {**_side_counts(lines), "minutes_by_period": None, "stints": None}
    summary = _summary(lines)
    return {
        **_side_counts(lines),
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


def named_in_with_and_without() -> InvalidInputError:
    """The refusal of one player named in both ``with`` and ``without``."""

    return InvalidInputError(
        "The same player is named in with and without.",
        public_details={"parameters": ["with", "without"]},
    )


def rotation_split(
    without_lines: Sequence[Mapping[str, Any]],
    with_lines: Sequence[Mapping[str, Any]],
    *,
    detail: bool = False,
    labels: tuple[str, str] = ("without", "with"),
) -> dict[str, Any]:
    """One subject's lines in the without (asked) games and the with (other) games, side by side."""

    side = _side_detail if detail else _side_counts
    return {
        labels[0]: side(without_lines),
        labels[1]: side(with_lines),
        "minutes_change": _minutes_change(without_lines, with_lines),
        "insufficient": len(without_lines) < MIN_WITHOUT_GAMES,
    }


# -- box-score stats per side (crf04/statsplus#252) -------------------------

#: Output key -> game-log column, summed exactly and averaged over games played: every counting column
#: the stored game log carries (crf04/statsplus#252 listed six; the rest followed). ``3PM`` keeps its
#: original name; the rest use the game log's own.
_STAT_COLUMNS = (
    ("minutes", "MIN"),
    ("PTS", "PTS"),
    ("REB", "REB"),
    ("AST", "AST"),
    ("3PM", "FG3M"),
    ("FGA", "FGA"),
    ("FGM", "FGM"),
    ("FG3A", "FG3A"),
    ("FTM", "FTM"),
    ("FTA", "FTA"),
    ("OREB", "OREB"),
    ("DREB", "DREB"),
    ("STL", "STL"),
    ("BLK", "BLK"),
    ("TOV", "TOV"),
    ("PF", "PF"),
    ("PLUS_MINUS", "PLUS_MINUS"),
)


def _exact(value: Any) -> Fraction | None:
    """An exact value, or ``None`` for a missing one (an absent column, a null or NaN)."""

    if value is None or (isinstance(value, float) and value != value):
        return None
    return Fraction(str(value))


def _log_rows_by_game(frame: Any, team: str) -> dict[str, dict[str, Fraction | None]]:
    """One player's game-log rows for ``team`` by NBA game id, as exact values.

    Game-log and rotation game ids are the same NBA id string, so they match
    as stored (as Injury Scenarios matches them).
    """

    if frame is None or frame.empty or "TEAM_ABBREVIATION" not in frame:
        return {}
    rows: dict[str, dict[str, Fraction | None]] = {}
    for record in frame[frame["TEAM_ABBREVIATION"] == team].to_dict("records"):
        rows.setdefault(
            str(record["GAME_ID"]), {column: _exact(record.get(column)) for _, column in _STAT_COLUMNS}
        )
    return rows


def _side_stats(
    lines: Sequence[Mapping[str, Any]], logs: Mapping[str, Mapping[str, Fraction | None]]
) -> dict[str, Any] | None:
    """Per-game-played box-score averages over a side's games, or ``None``.

    Games where his rotation line shows minutes but no game-log row exists are
    counted in ``missing_logs`` and left out of the averages. A stat missing from
    any played game's row is ``None`` rather than an average of the rest.
    """

    played = [logs[line["game_id"]] for line in lines if line["game_id"] in logs]
    if not played:
        return None
    count = len(played)

    def average(*columns: str) -> float | None:
        values = [row[column] for row in played for column in columns]
        if any(v is None for v in values):
            return None
        return _round1(sum(values, Fraction(0)) / count)

    stats: dict[str, Any] = {"played": count}
    for key, column in _STAT_COLUMNS:
        stats[key] = average(column)
        if key == "3PM":
            stats["PRA"] = average("PTS", "REB", "AST")
    stats["missing_logs"] = sum(1 for line in lines if line["minutes_cs"] > 0 and line["game_id"] not in logs)
    return stats


def _side_games(
    lines: Sequence[Mapping[str, Any]], logs: Mapping[str, Mapping[str, Fraction | None]]
) -> list[dict[str, Any]]:
    """A side's games, newest first: the rotation line (date, started, minutes) and the game-log box score
    in the same keys as ``stats``, or ``box: null`` where he has no game-log row (a DNP). Sent only on
    request (``game_logs=true``), so a caller can count any line or minutes band itself."""

    out = []
    for line in sorted(lines, key=lambda line: line["game_date"], reverse=True):
        row = logs.get(line["game_id"])
        box = None
        if row is not None:
            box = {
                key: (None if row[column] is None else float(row[column])) for key, column in _STAT_COLUMNS
            }
            parts = [row[c] for c in ("PTS", "REB", "AST")]
            box["PRA"] = None if None in parts else float(sum(parts, Fraction(0)))
        out.append(
            {
                "game_date": line["game_date"].isoformat(),
                "started": line["started"],
                "minutes": _minutes(line["minutes_cs"]),
                "box": box,
            }
        )
    return out


def _team_game_logs(
    entries: Sequence[Mapping[str, Any]],
    game_ids: set[str],
    team: str,
    logs: Mapping[str, Mapping[str, Fraction | None]],
) -> list[dict[str, Any]]:
    """His ``_side_games`` in the team's ``game_ids`` inside his window, each with its matchup: a game he
    missed is a 0-minute entry with ``box: null`` (crf04/statsplus#504)."""

    lines = [entry for entry in entries if entry["game_id"] in game_ids]
    return [
        {**game, "matchup": _matchup(line, team)}
        for line, game in zip(lines, _side_games(lines, logs), strict=True)
    ]


class RotationService:
    """Compose the player and team rotation documents from stored rows."""

    def __init__(
        self,
        repository: Any,
        athlete_catalog: Any | None,
        default_season: Callable[[], str],
        game_logs: Any,
        injury_reports: Any | None = None,
        lineups: Any | None = None,
    ) -> None:
        self.repository = repository
        self.athlete_catalog = athlete_catalog
        self._default_season = default_season
        #: The game service: its publication-snapshot reads give ``without`` its box-score stats.
        self.game_logs = game_logs
        #: The stored injury reports: a missed game's status, and membership evidence for a ``without`` window.
        self.injury_reports = injury_reports
        self.membership = TeamMembership(injury_reports, game_logs.get_players_logs_by_id, athlete_catalog)
        #: The stored daily lineups the team view reports as ``lineup``.
        self.lineups = lineups

    # -- public reads ---------------------------------------------------

    def get_player_rotation(
        self,
        *,
        player_name: str,
        player_team: str | None,
        last_n: int,
        season: str | None,
        with_: Sequence[str] = (),
        without: Sequence[str] = (),
        game_logs: bool = False,
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
        if with_ or without:
            return self._player_split(season, athlete, team, with_, without, game_logs)
        team_games = cache(lambda tricode: self.repository.team_games(season, tricode))
        recent = _team_entries(own_rows, team, team_games(team))[:last_n]
        stints = self.repository.stints(
            athlete.player_id, [row["game_id"] for row in recent if row["on_box_score"]]
        )
        reported = self._reported(
            season, athlete.player_id, [row for row in recent if not row["on_box_score"]]
        )
        return {
            "season": season,
            "last_n": last_n,
            "team_games_counted": True,
            "player": {"name": athlete.display_name, "team": team},
            "recent": _window_summary(recent),
            "season_summary": _window_summary(_season_entries(own_rows, team_games)),
            "games": [
                {
                    "game_date": game["game_date"].isoformat(),
                    "matchup": _matchup(game, team),
                    "started": game["started"],
                    "minutes": _minutes(game["minutes_cs"]),
                    "closed": game["closed"],
                    "on_box_score": game["on_box_score"],
                    **_report_fields(reported.get(game["game_id"])),
                    "minutes_by_period": _period_minutes([game]),
                    "stints": [_stint(stint) for stint in stints.get(game["game_id"], ())],
                }
                for game in recent
            ],
        }

    def get_team_rotation(
        self,
        *,
        team: str,
        last_n: int,
        season: str | None,
        with_: Sequence[str] = (),
        without: Sequence[str] = (),
        game_logs: bool = False,
        day: date | None = None,
    ) -> dict[str, Any]:
        """The team view; its ``lineup`` also rides on a ``no_rotation_data`` 404 (opening night)."""

        try:
            document = self._team_rotation(team, last_n, season, with_, without, game_logs)
        except ResourceNotFoundError as error:
            details = error.public_details or {}
            if self.lineups is not None and details.get("reason") == "no_rotation_data":
                error._public_details = {**details, "lineup": self.lineups.read(team, day=day)}
            raise
        if self.lineups is None:
            return document
        return {**document, "lineup": self.lineups.read(team, day=day)}

    def _team_rotation(
        self,
        team: str,
        last_n: int,
        season: str | None,
        with_: Sequence[str],
        without: Sequence[str],
        game_logs: bool,
    ) -> dict[str, Any]:
        self._tricode_id(team, "team")
        season = self._season(season)
        if with_ or without:
            return self._team_split(season, team, with_, without, game_logs)
        team_games = cache(lambda tricode: self.repository.team_games(season, tricode))
        recent_games = team_games(team)[:last_n]
        if not recent_games:
            raise ResourceNotFoundError(
                "The requested team has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        team_rows = self.repository.player_games(season, team=team)
        recent_ids = {game["game_id"] for game in recent_games}
        # Players on the team's box score in one of its recent games, and
        # players who missed them all inside an open team window unless the
        # catalog says they left; players whose window closed before them drop out.
        listed = {row["player_id"] for row in team_rows if row["game_id"] in recent_ids}
        departed = self._catalog_departed_ids(season, team)
        player_ids = sorted({row["player_id"] for row in team_rows} - (departed - listed))
        rows_by_player = self._rows_by_player(self.repository.player_games(season, player_ids=player_ids))
        starters = {game["game_id"]: [] for game in recent_games}
        for row in team_rows:
            if row["game_id"] in recent_ids and row["started"]:
                starters[row["game_id"]].append(row["player_name"])
        logs = self._logs_by_player(season, team, player_ids) if game_logs else None
        players = []
        for player_id in player_ids:
            own_rows = rows_by_player[player_id]
            entries = _team_entries(own_rows, team, team_games(team))
            if player_id not in listed and not any(entry["game_id"] in recent_ids for entry in entries):
                continue
            recent = entries[:last_n]
            season_entries = _season_entries(own_rows, team_games)
            players.append(
                {
                    "name": own_rows[0]["player_name"],
                    "recent": _window_counts(recent),
                    "season_summary": _window_counts(season_entries),
                    "minutes_change": _window_minutes_change(recent, season_entries),
                    **(
                        {}
                        if logs is None
                        else {"game_logs": _team_game_logs(entries, recent_ids, team, logs[player_id])}
                    ),
                }
            )
        players.sort(
            key=lambda player: (
                player["minutes_change"] is None,
                -abs(player["minutes_change"] or 0.0),
                player["name"],
            )
        )
        return {
            "season": season,
            "last_n": last_n,
            "team_games_counted": True,
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

    # -- the split ------------------------------------------------------
    #
    # ``without`` alone answers the ``without`` document it always has
    # (crf04/statsplus#239): sides ``without`` and ``with``.  Any request with
    # ``with`` (crf04/statsplus#499) answers ``split``: sides ``asked`` and
    # ``other``, the named players and the mixed-game count.

    def _split(
        self, season: str, team: str, with_: Sequence[str], without: Sequence[str], subject_id: int | None
    ) -> _Split:
        lines_by_game, names = self._team_lines(season, team)
        if not lines_by_game:
            raise ResourceNotFoundError(
                "The requested team has no rotation data for the season.",
                public_details={"reason": "no_rotation_data"},
            )
        present, present_windows = self._named(season, team, "with", with_, lines_by_game, names, subject_id)
        absent, absent_windows = self._named(
            season, team, "without", without, lines_by_game, names, subject_id
        )
        if present.keys() & absent.keys():
            raise named_in_with_and_without()
        asked, other, mixed = split_games(lines_by_game, present_windows, absent_windows)
        return _Split(lines_by_game, names, present, absent, asked, other, mixed, legacy=not with_)

    def _player_split(
        self,
        season: str,
        athlete: Any,
        team: str,
        with_: Sequence[str],
        without: Sequence[str],
        game_logs: bool = False,
    ) -> dict[str, Any]:
        split = self._split(season, team, with_, without, athlete.player_id)
        subject = athlete.player_id
        asked_lines = split.lines(split.asked, subject)
        other_lines = split.lines(split.other, subject)
        logs = self._logs_by_player(season, team, [subject])[subject]
        asked_key, other_key = split.labels
        sides = rotation_split(asked_lines, other_lines, detail=True, labels=split.labels)
        sides[asked_key]["stats"] = _side_stats(asked_lines, logs)
        sides[other_key]["stats"] = _side_stats(other_lines, logs)
        if game_logs:
            sides[asked_key]["game_logs"] = _side_games(asked_lines, logs)
            sides[other_key]["game_logs"] = _side_games(other_lines, logs)
        return {
            "season": season,
            "player": {"name": athlete.display_name, "team": team},
            split.key: {
                **split.header(counts=not split.legacy),
                **sides,
                "games": [
                    {
                        "game_date": line["game_date"].isoformat(),
                        "matchup": _matchup(line, team),
                        "started": line["started"],
                        "minutes": _minutes(line["minutes_cs"]),
                        "closed": line["closed"],
                    }
                    for line in asked_lines
                ],
            },
        }

    def _team_split(
        self, season: str, team: str, with_: Sequence[str], without: Sequence[str], game_logs: bool = False
    ) -> dict[str, Any]:
        split = self._split(season, team, with_, without, None)
        named = split.present.keys() | split.absent.keys()
        sides: dict[int, tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]] = {}
        for game_id in split.asked:
            for player_id, line in split.lines_by_game[game_id].items():
                if player_id not in named:
                    sides.setdefault(player_id, ([], []))[0].append(line)
        for game_id in split.other:
            for player_id, line in split.lines_by_game[game_id].items():
                if player_id in sides:
                    sides[player_id][1].append(line)
        logs = self._logs_by_player(season, team, sides)
        asked_key, other_key = split.labels
        players = []
        for player_id, (asked_lines, other_lines) in sides.items():
            entry = rotation_split(asked_lines, other_lines, labels=split.labels)
            entry[asked_key]["stats"] = _side_stats(asked_lines, logs[player_id])
            entry[other_key]["stats"] = _side_stats(other_lines, logs[player_id])
            if game_logs:
                entry[asked_key]["game_logs"] = _side_games(asked_lines, logs[player_id])
                entry[other_key]["game_logs"] = _side_games(other_lines, logs[player_id])
            players.append({"name": split.names[player_id], **entry})
        players.sort(
            key=lambda player: (
                player["minutes_change"] is None,
                -abs(player["minutes_change"] or 0.0),
                player["name"],
            )
        )
        starters = [
            {
                "name": split.names[player_id],
                f"starts_{asked_key}": sum(1 for line in asked_lines if line["started"]),
                f"starts_{other_key}": sum(1 for line in other_lines if line["started"]),
            }
            for player_id, (asked_lines, other_lines) in sides.items()
            if any(line["started"] for line in asked_lines)
        ]
        starters.sort(key=lambda entry: (-entry[f"starts_{asked_key}"], entry["name"]))
        return {
            "season": season,
            "team": team,
            split.key: {
                **split.header(counts=True),
                "insufficient": len(split.asked) < MIN_WITHOUT_GAMES,
                "starters": starters,
                "players": players,
            },
        }

    def _logs_by_player(
        self, season: str, team: str, player_ids: Iterable[int]
    ) -> dict[int, dict[str, dict[str, Fraction]]]:
        """Each player's game-log rows for ``team``, one batched read under one publication snapshot.

        Read scoped to ``season``: an unscoped read of a season after the
        Published Season serves no logs (crf04/statsplus#484).
        """

        ids = sorted(player_ids)
        games = season_scoped(self.game_logs, season)
        snapshot = games.capture_publication_snapshot(season)
        frames = games.get_players_logs_by_id(ids, season, publication_snapshot=snapshot)
        return {player_id: _log_rows_by_game(frames.get(player_id), team) for player_id in ids}

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

    def _named(
        self,
        season: str,
        team: str,
        parameter: str,
        requested: Sequence[str],
        lines_by_game: Mapping[str, Mapping[int, Mapping[str, Any]]],
        names: Mapping[int, str],
        subject_id: int | None,
    ) -> tuple[dict[int, str], dict[int, TeamWindow | None]]:
        """Named ``with`` or ``without`` (``parameter``) players and their team windows.

        A name on none of the team's box scores resolves through the Athlete
        Catalog, and counts only with membership evidence for a window: an
        injured player who has not played (crf04/statsplus#450).
        """

        if not requested:
            return {}, {}
        targets = {normalize_athlete_name(typed) for typed in requested}
        listed = {normalize_athlete_name(name) for name in names.values()}
        missing = [typed for typed in requested if normalize_athlete_name(typed) not in listed]
        unlisted = self.membership.unlisted(season, missing)
        candidates = [
            player_id
            for player_id, name in {**names, **unlisted}.items()
            if normalize_athlete_name(name) in targets
        ]
        windows = absent_windows(self.repository, season, team, candidates, self.membership, lines_by_game)
        resolvable = {
            **names,
            **{player_id: name for player_id, name in unlisted.items() if windows[player_id] is not None},
        }
        named = self._resolve_named(parameter, requested, resolvable, team, subject_id=subject_id)
        return named, {player_id: windows[player_id] for player_id in named}

    @staticmethod
    def _resolve_named(
        parameter: str,
        requested: Sequence[str],
        names: Mapping[int, str],
        team: str,
        *,
        subject_id: int | None,
    ) -> dict[int, str]:
        """Named ``with`` or ``without`` players as ``{player_id: name}``, in request order."""

        def invalid(message: str, **details: Any) -> InvalidInputError:
            return InvalidInputError(
                message,
                public_details={"parameter": parameter, "parameters": [parameter], **details},
            )

        named: dict[int, str] = {}
        for typed in requested:
            target = normalize_athlete_name(typed)
            matches = sorted(
                (name, player_id)
                for player_id, name in names.items()
                if normalize_athlete_name(name) == target
            )
            if not target or not matches:
                raise invalid(
                    f"The {parameter} player was not found on the team's box scores for the season."
                )
            if len(matches) > 1:
                raise invalid(
                    f"The {parameter} name matches more than one player.",
                    candidates=[{"name": name, "team": team} for name, _ in matches],
                )
            ((name, player_id),) = matches
            if player_id == subject_id:
                raise invalid(f"The {parameter} player cannot be the player_name player.")
            named[player_id] = name
        return named

    # -- helpers --------------------------------------------------------

    def _reported(
        self, season: str, player_id: int, missed: Sequence[Mapping[str, Any]]
    ) -> dict[str, Mapping[str, Any]]:
        """His row on each missed game's stored report, by game; one read, and none when it fails."""

        if not missed or self.injury_reports is None:
            return {}
        try:
            snapshots = self.injury_reports.get_season_mentions(season, [player_id])
        except (SQLAlchemyError, ValueError) as error:
            logger.warning("missed-game injury reports unreadable season=%s: %s", season, error)
            return {}
        by_game = {scope.game_id: snapshot for scope, snapshot in snapshots.items()}
        found = {}
        for game in missed:
            snapshot = by_game.get(game["game_id"])
            entry = (
                None
                if snapshot is None
                else listed_entry(snapshot, player_id, game["team_tricode"], game["game_date"].isoformat())
            )
            if entry is not None:
                found[game["game_id"]] = entry
        return found

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

    def _catalog_departed_ids(self, season: str, team: str) -> set[int]:
        """Ids the season's Athlete Catalog says left ``team``: another team, or off every roster.

        A missing row or a null team is unknown, not a departure.
        """

        if self.athlete_catalog is None:
            return set()
        team_id = NBA_TEAM_TRICODE_TO_ID[team]
        return {
            athlete["player_id"]
            for athlete in self.athlete_catalog.get_catalog(season, active_only=False)
            if athlete.get("team_id") not in (None, team_id)
            or athlete.get("roster_status") in ("inactive", "historical", "tombstone")
        }


@dataclass(frozen=True)
class _Split:
    """A team's games classified for one ``with`` / ``without`` request."""

    lines_by_game: Mapping[str, Mapping[int, Mapping[str, Any]]]
    names: Mapping[int, str]
    present: Mapping[int, str]
    absent: Mapping[int, str]
    asked: Sequence[str]
    other: Sequence[str]
    mixed: int
    #: ``without`` alone: the ``without`` document, sides ``without`` and ``with``.
    legacy: bool

    @property
    def key(self) -> str:
        return "without" if self.legacy else "split"

    @property
    def labels(self) -> tuple[str, str]:
        return ("without", "with") if self.legacy else ("asked", "other")

    def header(self, *, counts: bool) -> dict[str, Any]:
        """The named players, and the game counts when ``counts`` (the ``without`` player view has none)."""

        if self.legacy:
            return {
                "absent": list(self.absent.values()),
                **({"games_without": len(self.asked), "games_with": len(self.other)} if counts else {}),
            }
        return {
            "with": list(self.present.values()),
            "without": list(self.absent.values()),
            "games_asked": len(self.asked),
            "games_other": len(self.other),
            "games_mixed": self.mixed,
        }

    def lines(self, game_ids: Sequence[str], player_id: int) -> list[Mapping[str, Any]]:
        """The player's lines in ``game_ids``, skipping games where he has none."""

        return [self.lines_by_game[g][player_id] for g in game_ids if player_id in self.lines_by_game[g]]


def _season_entries(
    own_rows: Sequence[Mapping[str, Any]], team_games: Callable[[str], Sequence[Mapping[str, Any]]]
) -> list[dict[str, Any]]:
    """His team games for every team he played for in the season, newest first."""

    teams = {row["team_tricode"] for row in own_rows}
    entries = [entry for team in teams for entry in _team_entries(own_rows, team, team_games(team))]
    return sorted(entries, key=lambda entry: (entry["game_date"], entry["game_id"]), reverse=True)


__all__ = [
    "MIN_WITHOUT_GAMES",
    "RotationService",
    "absent_windows",
    "named_in_with_and_without",
    "rotation_split",
    "split_games",
]
