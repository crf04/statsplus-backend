"""Derive per-player rotations from one game's NBA LiveData documents.

A pure function from a final LiveData box score and its play-by-play to every
box-score player's starter flag, minutes, closing status, minutes by period and
on-court stints (crf04/statsplus#223).  Q1 starts with the box-score starters;
substitutions, in feed order, move players on and off the floor, and a player
on court at the end of a period stays on court into the next one (a change
between periods is a substitution at the start of that period).

The result is a gate, not a best effort.  A game whose derived stints leave any
player more than one second from his box-score minutes, or a team with other
than five players on court at any moment, raises :class:`RotationRejected`; a
partial or guessed rotation is never returned.

All times are integer centiseconds so that sums compare exactly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CENTISECONDS = 100
REGULATION_PERIODS = 4
REGULATION_PERIOD_CS = 12 * 60 * CENTISECONDS
OVERTIME_PERIOD_CS = 5 * 60 * CENTISECONDS
ON_COURT = 5
#: A player may be this far from his box-score minutes (one second).
MINUTES_TOLERANCE_CS = CENTISECONDS
#: Period keys of the minutes-by-period split; ``OT`` sums every overtime.
PERIOD_KEYS = ("Q1", "Q2", "Q3", "Q4", "OT")
REGULATION_END_CS = REGULATION_PERIODS * REGULATION_PERIOD_CS

_CLOCK = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?\Z")


class RotationRejected(ValueError):
    """The LiveData documents do not yield a rotation that can be trusted."""


@dataclass(frozen=True, slots=True)
class Stint:
    """One continuous stay on court, in the feed's period and clock terms."""

    in_period: int
    in_clock_cs: int
    out_period: int
    out_clock_cs: int
    minutes_cs: int


@dataclass(frozen=True, slots=True)
class PlayerRotation:
    player_id: int
    name: str
    team_tricode: str
    started: bool
    #: Box-score minutes; derived stint minutes agree within one second.
    minutes_cs: int
    closed: bool
    #: Centiseconds on court in each of :data:`PERIOD_KEYS`.
    period_cs: Mapping[str, int]
    stints: tuple[Stint, ...]


@dataclass(frozen=True, slots=True)
class GameRotation:
    game_id: str
    game_date: str
    home_tricode: str
    away_tricode: str
    periods: int
    players: tuple[PlayerRotation, ...]


def period_length_cs(period: int) -> int:
    return REGULATION_PERIOD_CS if period <= REGULATION_PERIODS else OVERTIME_PERIOD_CS


def period_start_cs(period: int) -> int:
    return sum(period_length_cs(index) for index in range(1, period))


def period_key(period: int) -> str:
    return f"Q{period}" if period <= REGULATION_PERIODS else "OT"


def parse_clock_cs(value: object) -> int:
    """``PT06M08.00S`` as centiseconds."""

    match = _CLOCK.match(str(value))
    if match is None or not any(match.groups()):
        raise RotationRejected(f"malformed LiveData clock {value!r}")
    hours, minutes, seconds = match.groups()
    return round((int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)) * CENTISECONDS)


def derive_rotation(box: Mapping[str, Any], play_by_play: Mapping[str, Any]) -> GameRotation:
    """Rebuild every box-score player's rotation, or raise ``RotationRejected``."""

    game = box.get("game") if isinstance(box, Mapping) else None
    pbp_game = play_by_play.get("game") if isinstance(play_by_play, Mapping) else None
    if not isinstance(game, Mapping) or not isinstance(pbp_game, Mapping):
        raise RotationRejected("LiveData documents have no game")
    game_id = str(game.get("gameId"))
    if str(pbp_game.get("gameId")) != game_id:
        raise RotationRejected("play-by-play belongs to a different game")
    if game.get("gameStatus") != 3:
        raise RotationRejected("game is not final")
    periods = game.get("period")
    if not isinstance(periods, int) or periods < REGULATION_PERIODS:
        raise RotationRejected("final box score has no valid period count")
    actions = pbp_game.get("actions")
    if not isinstance(actions, list):
        raise RotationRejected("play-by-play has no actions")

    teams: dict[int, str] = {}
    names: dict[int, str] = {}
    box_minutes: dict[int, int] = {}
    starters: set[int] = set()
    tricodes = []
    for side in ("homeTeam", "awayTeam"):
        team = game.get(side)
        if not isinstance(team, Mapping) or not team.get("teamTricode"):
            raise RotationRejected("box score is missing a team")
        tricode = str(team["teamTricode"])
        tricodes.append(tricode)
        for player in team.get("players") or ():
            player_id = int(player["personId"])
            if player_id in teams:
                raise RotationRejected(f"player {player_id} is on both rosters")
            teams[player_id] = tricode
            names[player_id] = str(player.get("name") or "")
            statistics = player.get("statistics")
            if not isinstance(statistics, Mapping):
                raise RotationRejected(f"player {player_id} has no statistics")
            box_minutes[player_id] = parse_clock_cs(statistics.get("minutes"))
            if str(player.get("starter")) == "1":
                starters.add(player_id)

    for tricode in tricodes:
        count = sum(1 for player_id in starters if teams[player_id] == tricode)
        if count != ON_COURT:
            raise RotationRejected(f"{tricode} has {count} box-score starters, not {ON_COURT}")

    end = period_start_cs(periods) + period_length_cs(periods)
    open_since: dict[int, tuple[int, int, int]] = {
        player_id: (0, 1, REGULATION_PERIOD_CS) for player_id in starters
    }
    spans: dict[int, list[tuple[int, int, int, int, int, int]]] = {}

    def close(player_id: int, at: int, period: int, clock: int) -> None:
        began, in_period, in_clock = open_since.pop(player_id)
        spans.setdefault(player_id, []).append((began, at, in_period, in_clock, period, clock))

    def require_lineups(action: Mapping[str, Any]) -> None:
        for tricode in tricodes:
            count = sum(1 for player_id in open_since if teams[player_id] == tricode)
            if count != ON_COURT:
                where = (
                    f"{action['actionType']} action {action['actionNumber']}"
                    if "actionNumber" in action
                    else "the end of the game"
                )
                raise RotationRejected(f"{tricode} has {count} on court at {where}, not {ON_COURT}")

    for action in actions:
        if action.get("actionType") != "substitution":
            # Consecutive substitutions may pass through 4 or 6 on court, but
            # every other action happens with exactly five a side.
            require_lineups(action)
            continue
        player_id = int(action["personId"])
        period = int(action["period"])
        clock = parse_clock_cs(action["clock"])
        if player_id not in teams:
            raise RotationRejected(f"substitution names unknown player {player_id}")
        if period < 1 or period > periods or clock > period_length_cs(period):
            raise RotationRejected("substitution outside the game clock")
        at = period_start_cs(period) + period_length_cs(period) - clock
        direction = action.get("subType")
        if direction == "out":
            if player_id not in open_since:
                raise RotationRejected(f"player {player_id} left the floor while off it")
            if period > 1 and clock == period_length_cs(period):
                # A change at the start of a period ends the stint at the end
                # of the previous one.
                period, clock = period - 1, 0
            close(player_id, at, period, clock)
        elif direction == "in":
            if player_id in open_since:
                raise RotationRejected(f"player {player_id} entered while on the floor")
            open_since[player_id] = (at, period, clock)
        else:
            raise RotationRejected(f"unknown substitution type {direction!r}")
    require_lineups({})
    for player_id in list(open_since):
        close(player_id, end, periods, 0)

    rotations = []
    by_team: dict[str, list[tuple[int, int]]] = {tricode: [] for tricode in tricodes}
    for player_id in sorted(teams):
        stints = _merged_stints(spans.get(player_id, ()))
        total = sum(stint.minutes_cs for _, _, stint in stints)
        if abs(total - box_minutes[player_id]) > MINUTES_TOLERANCE_CS:
            raise RotationRejected(
                f"player {player_id} stints total {total} cs, box score {box_minutes[player_id]} cs"
            )
        period_cs = {key: 0 for key in PERIOD_KEYS}
        closed = False
        for begin, finish, stint in stints:
            by_team[teams[player_id]].append((begin, finish))
            for period in range(1, periods + 1):
                overlap = min(finish, period_start_cs(period) + period_length_cs(period)) - max(
                    begin, period_start_cs(period)
                )
                if overlap > 0:
                    period_cs[period_key(period)] += overlap
            closed = closed or begin < REGULATION_END_CS <= finish
        rotations.append(
            PlayerRotation(
                player_id=player_id,
                name=names[player_id],
                team_tricode=teams[player_id],
                started=player_id in starters,
                minutes_cs=box_minutes[player_id],
                closed=closed,
                period_cs=period_cs,
                stints=tuple(stint for _, _, stint in stints),
            )
        )
    for tricode, intervals in by_team.items():
        _require_five_on_court(tricode, intervals, end)
    return GameRotation(
        game_id=game_id,
        game_date=_game_date(game),
        home_tricode=tricodes[0],
        away_tricode=tricodes[1],
        periods=periods,
        players=tuple(rotations),
    )


def _merged_stints(spans) -> list[tuple[int, int, Stint]]:
    """Chronological non-empty stints, joining those that touch."""

    merged: list[list[int]] = []
    for began, finish, in_period, in_clock, out_period, out_clock in sorted(spans):
        if finish <= began:
            continue
        if merged and merged[-1][1] == began:
            merged[-1][1], merged[-1][4], merged[-1][5] = finish, out_period, out_clock
        else:
            merged.append([began, finish, in_period, in_clock, out_period, out_clock])
    return [
        (
            began,
            finish,
            Stint(in_period, in_clock, out_period, out_clock, finish - began),
        )
        for began, finish, in_period, in_clock, out_period, out_clock in merged
    ]


def _require_five_on_court(tricode: str, intervals: list[tuple[int, int]], end: int) -> None:
    changes: dict[int, int] = {}
    for begin, finish in intervals:
        changes[begin] = changes.get(begin, 0) + 1
        changes[finish] = changes.get(finish, 0) - 1
    moments = sorted(changes)
    if not moments or moments[0] != 0 or moments[-1] != end:
        raise RotationRejected(f"{tricode} has no one on court for part of the game")
    count = 0
    for moment in moments[:-1]:
        count += changes[moment]
        if count != ON_COURT:
            raise RotationRejected(f"{tricode} has {count} on court at {moment} cs, not {ON_COURT}")


def _game_date(game: Mapping[str, Any]) -> str:
    """The Eastern calendar date of tip-off, ``YYYY-MM-DD``."""

    tip = str(game.get("gameEt") or "")
    if not re.match(r"\d{4}-\d{2}-\d{2}", tip):
        raise RotationRejected("box score has no Eastern game date")
    return tip[:10]


def clock_text(clock_cs: int) -> str:
    """Game clock as ``M:SS`` (whole seconds, rounded down)."""

    seconds = clock_cs // CENTISECONDS
    return f"{seconds // 60}:{seconds % 60:02d}"


__all__ = [
    "PERIOD_KEYS",
    "GameRotation",
    "PlayerRotation",
    "RotationRejected",
    "Stint",
    "clock_text",
    "derive_rotation",
    "parse_clock_cs",
]
