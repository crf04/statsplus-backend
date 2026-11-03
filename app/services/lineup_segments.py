"""Lineup segments from one game's NBA LiveData documents (crf04/statsplus#410).

A pure function from the final box score and play-by-play that
:func:`~app.services.rotation_derivation.derive_rotation` accepted to each
team's segments: stretches of one period with the same five on the floor.
On/off splits are sums over segments, so the rules here are what make them
match PBP Stats WOWY (the prototype in the parent issue measured them):

* Every action counts in the segment current at its place in the feed,
  except free throws, which count in the lineup on the floor at the foul
  that caused them (substitutions often come between the foul and the
  shots).
* An offensive possession is credited to the offense's segment at the event
  that ends it: a made field goal that is not an and-one, a defensive
  rebound, a turnover, the last free throw of a trip made, or the end of the
  period.  Free throws that let the shooting team keep the ball (technical,
  flagrant, away-from-play, clear-path, inbound) end nothing, and neither does
  the dead-ball rebound after one of them missed.  A possession that starts
  and ends in the last two seconds of a period counts only when something
  scored in it (PBP Stats's two-second rule).
* Heaves: before :data:`HEAVES_EXCLUDED_SEASON` the feed's ``heave`` actions
  are markers with no shooter, counted only as the team's player-less heaves.
  From that season a heave names its shooter and counts as his missed FGA and
  3PA; a heave with no shooter is still a player-less heave.

Only the five on-floor players get counts; a count credited to a player not
in the segment's lineup is dropped, as a WOWY split drops it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.providers.nba_live_data import HEAVES_EXCLUDED_SEASON
from app.services.rotation_derivation import (
    ON_COURT,
    RotationRejected,
    parse_clock_cs,
    period_length_cs,
    period_start_cs,
)

#: The per-player counts each segment row carries, in column order.
COUNTS = ("pts", "reb", "ast", "fg3m", "fga", "fg3a", "tov", "fta")
#: Free-throw description words for trips after which the shooter's team keeps the ball.
_KEEPS_BALL = (
    "technical",
    "flagrant",
    "away-from-play",
    "away from play",
    "inbound",
    "clear path",
    "clear-path",
)
#: The two-second rule's window, in centiseconds of clock remaining.
_LAST_SECONDS_CS = 200
#: How far past a made shot the and-one free throw is looked for.
_AND_ONE_LOOKAHEAD = 7
_SHOTS = ("2pt", "3pt", "heave")


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch with the same five on the floor for one team."""

    team_id: int
    seg: int
    #: The five on-floor player ids, ascending.
    players: tuple[int, ...]
    duration_cs: int
    #: Each on-floor player's counts, keyed as :data:`COUNTS`.
    counts: Mapping[int, Mapping[str, int]]
    team_fga: int
    team_fta: int
    team_tov: int
    team_heaves: int
    off_poss: int


@dataclass(frozen=True, slots=True)
class GameSegments:
    game_id: str
    home_team_id: int
    away_team_id: int
    #: Box-score names of every player on either team, for roster slots.
    names: Mapping[int, str]
    segments: tuple[Segment, ...]


@dataclass(slots=True)
class _Open:
    team_id: int
    start_cs: int
    players: frozenset[int]
    end_cs: int | None = None
    counts: dict[int, dict[str, int]] = field(default_factory=dict)
    team: dict[str, int] = field(default_factory=dict)
    off_poss: int = 0

    def count(self, player_id: int, key: str, value: int = 1) -> None:
        if player_id in self.players:
            stats = self.counts.setdefault(player_id, {})
            stats[key] = stats.get(key, 0) + value

    def count_team(self, key: str) -> None:
        self.team[key] = self.team.get(key, 0) + 1


def keeps_ball(free_throw: Mapping[str, Any]) -> bool:
    """Whether the shooting team keeps the ball after this free throw."""

    description = str(free_throw.get("description") or "").lower()
    return any(word in description for word in _KEEPS_BALL)


def derive_segments(box: Mapping[str, Any], play_by_play: Mapping[str, Any], season: str) -> GameSegments:
    """Both teams' segments for a game ``derive_rotation`` accepted."""

    game = box["game"]
    actions: Sequence[Mapping[str, Any]] = play_by_play["game"]["actions"]
    team_of: dict[int, int] = {}
    names: dict[int, str] = {}
    on: dict[int, set[int]] = {}
    sides = []
    for side in ("homeTeam", "awayTeam"):
        team = game[side]
        team_id = int(team["teamId"])
        sides.append(team_id)
        on[team_id] = set()
        for player in team.get("players") or ():
            player_id = int(player["personId"])
            team_of[player_id] = team_id
            names[player_id] = str(player.get("name") or "")
            if str(player.get("starter")) == "1":
                on[team_id].add(player_id)
    home, away = sides
    other = {home: away, away: home}
    heave_shooters_count = season >= HEAVES_EXCLUDED_SEASON

    segments: dict[int, list[_Open]] = {team_id: [] for team_id in sides}
    current: dict[int, _Open] = {}

    def open_segment(team_id: int, start_cs: int) -> None:
        players = frozenset(on[team_id])
        if len(players) != ON_COURT:
            raise RotationRejected(f"team {team_id} has {len(players)} on court at {start_cs} cs")
        current[team_id] = _Open(team_id, start_cs, players)
        segments[team_id].append(current[team_id])

    for team_id in sides:
        open_segment(team_id, 0)
    changed_at = {team_id: 0 for team_id in sides}
    period = 1
    #: The segments current at each action, and at the latest foul before it.
    at_action: list[dict[int, _Open]] = []
    at_foul: list[dict[int, _Open] | None] = []
    foul_lineups: dict[int, _Open] | None = None
    for action in actions:
        action_type = action["actionType"]
        action_period = int(action["period"])
        moment = (
            period_start_cs(action_period) + period_length_cs(action_period) - parse_clock_cs(action["clock"])
        )
        if action_type == "substitution":
            player_id = int(action["personId"])
            team_id = team_of[player_id]
            if action["subType"] == "out":
                on[team_id].discard(player_id)
            else:
                on[team_id].add(player_id)
            changed_at[team_id] = moment
            at_action.append(dict(current))
            at_foul.append(foul_lineups)
            continue
        if action_type == "period" and action.get("subType") == "start" and action_period != period:
            for team_id in sides:
                current[team_id].end_cs = period_start_cs(action_period)
                open_segment(team_id, period_start_cs(action_period))
            period = action_period
        for team_id in sides:
            if frozenset(on[team_id]) != current[team_id].players:
                current[team_id].end_cs = changed_at[team_id]
                open_segment(team_id, changed_at[team_id])
        lineups = dict(current)
        if action_type == "foul":
            foul_lineups = lineups
        at_action.append(lineups)
        at_foul.append(foul_lineups)
        _count(action, lineups, foul_lineups, heave_shooters_count)
    end = period_start_cs(period) + period_length_cs(period)
    for team_id in sides:
        current[team_id].end_cs = end

    for index, offense in _counted_possessions(actions, other):
        lineups = at_action[index]
        if actions[index]["actionType"] == "freethrow" and at_foul[index] is not None:
            lineups = at_foul[index]
        lineups[offense].off_poss += 1

    stored = []
    for team_id in sides:
        seq = 0
        for segment in segments[team_id]:
            duration = segment.end_cs - segment.start_cs
            if not (duration > 0 or segment.counts or segment.team or segment.off_poss):
                continue
            stored.append(
                Segment(
                    team_id=team_id,
                    seg=seq,
                    players=tuple(sorted(segment.players)),
                    duration_cs=duration,
                    counts={
                        player_id: {key: segment.counts.get(player_id, {}).get(key, 0) for key in COUNTS}
                        for player_id in sorted(segment.players)
                    },
                    team_fga=segment.team.get("fga", 0),
                    team_fta=segment.team.get("fta", 0),
                    team_tov=segment.team.get("tov", 0),
                    team_heaves=segment.team.get("heave", 0),
                    off_poss=segment.off_poss,
                )
            )
            seq += 1
    return GameSegments(
        game_id=str(game["gameId"]),
        home_team_id=home,
        away_team_id=away,
        names=names,
        segments=tuple(stored),
    )


def _count(
    action: Mapping[str, Any],
    lineups: Mapping[int, _Open],
    foul_lineups: Mapping[int, _Open] | None,
    heave_shooters_count: bool,
) -> None:
    action_type = action["actionType"]
    team_id = action.get("teamId")
    if not team_id:
        return
    if action_type == "freethrow" and foul_lineups is not None:
        segment = foul_lineups.get(int(team_id))
    else:
        segment = lineups.get(int(team_id))
    if segment is None:
        return
    player_id = int(action.get("personId") or 0)
    if action_type == "heave" and not (player_id and heave_shooters_count):
        segment.count_team("heave")
    elif action_type in _SHOTS and player_id:
        three = action_type != "2pt"
        segment.count(player_id, "fga")
        segment.count_team("fga")
        if three:
            segment.count(player_id, "fg3a")
        if action.get("shotResult") == "Made":
            segment.count(player_id, "pts", 3 if three else 2)
            if three:
                segment.count(player_id, "fg3m")
            if action.get("assistPersonId"):
                segment.count(int(action["assistPersonId"]), "ast")
    elif action_type == "freethrow":
        segment.count(player_id, "fta")
        segment.count_team("fta")
        if action.get("shotResult") == "Made":
            segment.count(player_id, "pts")
    elif action_type == "rebound":
        if player_id:
            segment.count(player_id, "reb")
    elif action_type == "turnover":
        segment.count_team("tov")
        if player_id:
            segment.count(player_id, "tov")


def _counted_possessions(
    actions: Sequence[Mapping[str, Any]], other: Mapping[int, int]
) -> list[tuple[int, int]]:
    """``(action index, offense team)`` of each counted possession's ending event."""

    plays = [index for index, action in enumerate(actions) if action["actionType"] != "substitution"]
    # The feed's own ``possession`` field, a defensive rebound keeping the
    # previous offense: only for a period whose first end is its last event.
    offense: list[int] = []
    previous = 0
    for action in actions:
        holder = int(action.get("possession") or 0)
        if action["actionType"] == "rebound" and action.get("subType") == "defensive":
            holder = previous
        offense.append(holder)
        previous = holder

    ends: list[tuple[int, int]] = []
    for position, index in enumerate(plays):
        action = actions[index]
        action_type = action["actionType"]
        sub_type = action.get("subType")
        team_id = int(action.get("teamId") or 0)
        if action_type in _SHOTS and action.get("shotResult") == "Made":
            if not _and_one_follows(actions, plays[position + 1 : position + 1 + _AND_ONE_LOOKAHEAD], action):
                ends.append((index, team_id))
        elif action_type == "rebound" and sub_type == "defensive":
            before = actions[plays[position - 1]] if position else {}
            if not (before.get("actionType") == "freethrow" and keeps_ball(before)):
                ends.append((index, other.get(team_id, 0)))
        elif action_type == "turnover":
            ends.append((index, team_id))
        elif action_type == "freethrow" and action.get("shotResult") == "Made":
            if _last_of_trip(sub_type) and not keeps_ball(action):
                ends.append((index, team_id))
        elif action_type == "period" and sub_type == "end":
            # The period's last event ends a possession for the team that got
            # the ball at the previous end; the two-second rule usually drops it.
            in_period = [holder for at, holder in ends if actions[at]["period"] == action["period"]]
            holder = other.get(in_period[-1], 0) if in_period else (offense[index - 1] if index else 0)
            if holder:
                ends.append((index, holder))

    counted = []
    last: int | None = None
    for index, holder in ends:
        if not holder:
            continue
        action = actions[index]
        keep = True
        if last is not None and actions[last]["period"] == action["period"]:
            started_cs = parse_clock_cs(actions[last]["clock"])
            if parse_clock_cs(action["clock"]) <= _LAST_SECONDS_CS and started_cs <= _LAST_SECONDS_CS:
                keep = any(_scores(actions[between]) for between in range(last + 1, index + 1))
        if keep:
            counted.append((index, holder))
        last = index
    return counted


def _and_one_follows(
    actions: Sequence[Mapping[str, Any]], following: Sequence[int], shot: Mapping[str, Any]
) -> bool:
    """A 1-of-1 free throw by the shooter's team at the same clock: the team keeps
    shooting, so the make does not end the possession.  A flagrant 1-of-1 counts
    too (the team keeps the ball after it), other ball-keeping ones do not."""

    for index in following:
        action = actions[index]
        if (
            action["actionType"] == "freethrow"
            and action.get("subType") == "1 of 1"
            and action["clock"] == shot["clock"]
            and action.get("teamId") == shot.get("teamId")
            and (not keeps_ball(action) or "flagrant" in str(action.get("description") or "").lower())
        ):
            return True
    return False


def _last_of_trip(sub_type: Any) -> bool:
    text = str(sub_type or "")
    if " of " not in text:
        return False
    shot, trip = text.split(" of ", 1)
    return shot == trip


def _scores(action: Mapping[str, Any]) -> bool:
    return action["actionType"] == "freethrow" or (
        action["actionType"] in _SHOTS and action.get("shotResult") == "Made"
    )


__all__ = ["COUNTS", "GameSegments", "Segment", "derive_segments", "keeps_ball"]
