"""Injury Scenarios behind ``GET /api/injury_scenarios`` (crf04/statsplus#220).

For each player the stored report lists as Questionable, Doubtful or Out, this
combines three kinds of evidence about every rotation teammate:

* the full-game split in games the injured player missed, by the game-log
  ``players_off`` absence rule (``GameService.games_without_player``);
* the per-36 split while the injured player is off the floor, from
  ``OnOffService``;
* the teammate's record against today's posted line in those missed games.

It reads only through the services behind ``/api/injuries``, ``/api/on_off``
and the game-log ``players_off`` filter, so its numbers are what those return
for the same inputs.  Nothing is stored: a scenario is rebuilt from the stored
injury snapshot on every request, so it follows the snapshot as it changes.

Evidence windows
----------------
A season's games join a pair's window only after the later of the two
players' last appearance for a different team (a team change resets the pair).
With no team change, the without games are exactly those the game-log
``players_off`` filter returns.  On/off totals cover a whole team season and
cannot be sliced at a trade date, so a season in which either player changed
teams contributes full-game evidence only (``on_off_unsliceable``).  Requests
read the on/off cache only (``OnOffService.get_cached``); the warm fills it.

Baseline
--------
Each kind of evidence is compared with its own "with him" figure: the
full-game split with the teammate's average in games the injured player
played, the on/off split with the teammate's per-36 while the injured player
is on the floor (``on[]``).  The "plays" branch reports those baselines only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.services.research_season import previous_nba_season, schedule_season

#: Statuses that get a scenario; Probable and Available get none.
SCENARIO_STATUSES = ("Out", "Doubtful", "Questionable")
#: Statuses with both a "plays" and a "sits" branch.
UNCERTAIN_STATUSES = frozenset({"Doubtful", "Questionable"})

MIN_GAMES_WITHOUT = 3
MIN_OFF_FLOOR_MINUTES = 150
ROTATION_MINUTES_PER_GAME = 15.0
FLAG_SHIFT = 0.10
#: Float slack so exactly 10% flags but 9.996% does not.
FLAG_TOLERANCE = 1e-9

#: Scored market -> (game-log column, on/off total keys summed).
SCORED_MARKETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "PTS": ("PTS", ("pts",)),
    "REB": ("REB", ("reb",)),
    "AST": ("AST", ("ast",)),
    "PRA": ("PRA", ("pts", "reb", "ast")),
    "3PM": ("FG3M", ("fg3m",)),
}
#: Supporting context, never scored or flagged.
CONTEXT_MARKETS = ("MIN", "FGA", "USG")

#: Posted-line providers in the order a line is taken from.
LINE_PROVIDER_PREFERENCE = ("prizepicks",)


def _season_span(seasons: Iterable[str]) -> str | None:
    ordered = sorted(set(seasons))
    if not ordered:
        return None
    return ordered[0] if len(ordered) == 1 else f"{ordered[0]} to {ordered[-1]}"


def _raw_mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def _shift(value: float | None, baseline: float | None) -> float | None:
    """Unrounded relative change; rounding is for output only."""

    if value is None or baseline is None or baseline <= 0:
        return None
    return value / baseline - 1


def _agree(first: float | None, second: float | None) -> bool:
    if first is None or second is None:
        return False
    reach = FLAG_SHIFT - FLAG_TOLERANCE
    return (first >= reach and second >= reach) or (
        first <= -reach and second <= -reach
    )




class _Logs:
    """One player's stored logs for one season."""

    def __init__(self, frame: pd.DataFrame | None) -> None:
        self.frame = frame if frame is not None and not frame.empty else None

    def absent_frame(self) -> pd.DataFrame:
        """Frame for the absence rule; an empty one when he has no rows."""

        if self.frame is None:
            return pd.DataFrame({"GAME_ID": [], "TEAM_ABBREVIATION": []})
        return self.frame

    def team_rows(self, team: str) -> pd.DataFrame | None:
        if self.frame is None:
            return None
        rows = self.frame[self.frame["TEAM_ABBREVIATION"] == team]
        return None if rows.empty else rows

    def other_team_rows(self, team: str) -> pd.DataFrame | None:
        if self.frame is None:
            return None
        rows = self.frame[self.frame["TEAM_ABBREVIATION"] != team]
        return None if rows.empty else rows

    def name(self) -> str | None:
        if self.frame is None or "PLAYER_NAME" not in self.frame:
            return None
        return str(self.frame["PLAYER_NAME"].iloc[0])


class _OnOffReads:
    """Cache-only on/off reads for one injured player, one per season and side."""

    def __init__(self, on_off: Any, team: str, injured_id: int) -> None:
        self.on_off = on_off
        self.team = team
        self.injured_id = injured_id
        self._memo: dict[tuple[str, str], Mapping[str, Any]] = {}

    def get(self, season: str, side: str) -> Mapping[str, Any]:
        key = (season, side)
        if key not in self._memo:
            self._memo[key] = self.on_off.get_cached(
                self.team, season, **{side: [self.injured_id]}
            )
        return self._memo[key]


class _WindowSeason:
    """One season of a pair's window."""

    def __init__(
        self, season: str, rows: pd.DataFrame, without: set[str], sliceable: bool
    ) -> None:
        self.season = season
        self.rows = rows
        self.without = without
        #: Whether on/off totals for this season match the window's games: false
        #: when either player changed teams in it.
        self.sliceable = sliceable


class InjuryScenarioService:
    def __init__(
        self,
        *,
        injuries: Any,
        on_off: Any,
        games: Any,
        player_pool: Any | None,
        event_catalog: Any,
        settings: Any,
    ) -> None:
        self.injuries = injuries
        self.on_off = on_off
        self.games = games
        self.player_pool = player_pool
        self.event_catalog = event_catalog
        self.settings = settings

    # -- entry points --------------------------------------------------------

    def scenarios(
        self,
        requested_date: str | None,
        game_id: str | None = None,
        player: str | None = None,
    ) -> dict[str, Any]:
        listing = self.injuries.list_injuries(requested_date, None)
        slate_date = datetime.strptime(listing["date"], "%Y-%m-%d").date()
        season = schedule_season(self.settings, self.event_catalog, slate_date)
        wanted = str(player).strip().casefold() if player else None
        scenarios = []
        for row in listing["injuries"]:
            if row["status"] not in SCENARIO_STATUSES:
                continue
            if game_id and row["game_id"] != game_id:
                continue
            if wanted and wanted not in (
                str(row["player_id"]), row["player"].casefold()
            ):
                continue
            scenarios.append(self._scenario(row, season))
        return {"date": listing["date"], "scenarios": scenarios}

    def prior_season_eligible(self, row: Mapping[str, Any], prior_season: str) -> bool:
        """Whether the listed player's last season can join his window.

        He needs a game for this team in it; the warm uses this to cache the
        prior season's splits that a scenario will read.
        """

        if row.get("player_id") is None:
            return False
        logs = _Logs(self._logs(int(row["player_id"]), prior_season))
        return logs.team_rows(row["team"]) is not None

    # -- one injured player ----------------------------------------------------

    def _scenario(self, row: Mapping[str, Any], season: str) -> dict[str, Any]:
        limits = ["stale_report"] if row["stale"] else []
        scenario: dict[str, Any] = {
            "injured": {
                "player": row["player"],
                "player_id": row["player_id"],
                "team": row["team"],
                "game_id": row["game_id"],
                "status": row["status"],
                "report_time": row["report_time"],
            },
            "branches": [],
            "limits": limits,
        }
        if row["player_id"] is None:
            limits.append("player_unresolved")
            return scenario
        branches = ["plays", "sits"] if row["status"] in UNCERTAIN_STATUSES else ["sits"]
        evidence = self._teammates(int(row["player_id"]), row["team"], season, row["game_id"])
        for branch in branches:
            scenario["branches"].append({
                "branch": branch,
                "rows": [
                    market_row
                    for teammate in evidence
                    for market_row in teammate.rows(branch, bool(row["stale"]))
                ],
            })
        return scenario

    def _teammates(
        self, injured_id: int, team: str, season: str, game_id: str
    ) -> list["_Teammate"]:
        seasons = [previous_nba_season(season), season]  # chronological
        injured_logs = {s: _Logs(self._logs(injured_id, s)) for s in seasons}
        reads = _OnOffReads(self.on_off, team, injured_id)
        team_id = NBA_TEAM_TRICODE_TO_ID[team]
        roster: set[int] = set()
        for s in seasons:
            roster |= self.games.get_team_player_ids(team_id, s)
        roster.discard(injured_id)
        pool_lines = self._pool_lines(season, game_id)
        teammates = []
        for teammate_id in sorted(roster):
            built = self._teammate(
                teammate_id, injured_id, team, seasons, injured_logs, reads, pool_lines
            )
            if built is not None:
                teammates.append(built)
        return teammates

    def _teammate(
        self,
        teammate_id: int,
        injured_id: int,
        team: str,
        seasons: Sequence[str],
        injured_logs: Mapping[str, _Logs],
        reads: _OnOffReads,
        pool_lines: Mapping[int, Mapping[str, Mapping[str, float]]],
    ) -> "_Teammate | None":
        mates = {s: _Logs(self._logs(teammate_id, s)) for s in seasons}
        # A team change resets the pair: the window starts after either
        # player's last appearance for a different team.  That also drops a
        # teammate whose latest game was for another team (he has left).
        others = [
            logs.other_team_rows(team)
            for logs in (*injured_logs.values(), *mates.values())
        ]
        last_other = max(
            (rows["GAME_DATE"].max() for rows in others if rows is not None),
            default=None,
        )
        window: list[_WindowSeason] = []
        for s in seasons:
            rows = mates[s].team_rows(team)
            if rows is None:
                continue
            # Last season counts only with evidence he was on this team then.
            if s != seasons[-1] and injured_logs[s].team_rows(team) is None:
                continue
            if last_other is not None:
                rows = rows[rows["GAME_DATE"] > last_other]
            if rows.empty:
                continue
            changed = (
                injured_logs[s].other_team_rows(team) is not None
                or mates[s].other_team_rows(team) is not None
            )
            window.append(_WindowSeason(
                s, rows,
                self.games.games_without_player(rows, injured_logs[s].absent_frame()),
                sliceable=not changed,
            ))
        if not window:
            return None
        all_rows = pd.concat([w.rows for w in window])
        if float(all_rows["MIN"].mean()) < ROTATION_MINUTES_PER_GAME:
            return None
        name = next((mates[s].name() for s in reversed(seasons) if mates[s].name()), None)
        return _Teammate(
            teammate_id=teammate_id,
            name=name or str(teammate_id),
            window=window,
            reads=reads,
            lines=pool_lines.get(teammate_id, {}),
        )

    # -- reads ---------------------------------------------------------------

    def _logs(self, player_id: int, season: str) -> pd.DataFrame | None:
        return self.games.get_player_logs_by_id(player_id, season)

    def _pool_lines(
        self, season: str, game_id: str
    ) -> dict[int, dict[str, dict[str, float]]]:
        if self.player_pool is None:
            return {}
        pool = self.player_pool.get_pool_for_game(season=season, game_id=game_id)
        if pool is None:
            return {}
        return {
            int(player.canonical_player_id): {
                provider: dict(lines) for provider, lines in player.lines.items()
            }
            for player in pool.players
        }


def _posted_line(
    lines: Mapping[str, Mapping[str, float]], market: str
) -> tuple[float, str] | None:
    providers = [p for p in LINE_PROVIDER_PREFERENCE if p in lines]
    providers += sorted(p for p in lines if p not in providers)
    for provider in providers:
        if market in lines[provider]:
            return float(lines[provider][market]), provider
    return None


class _Floor:
    """One lineup state's on/off evidence, summed over the seasons it covers."""

    def __init__(self) -> None:
        self.totals: dict[str, int] = {}
        self.minutes = 0
        self.covered: list[str] = []
        self.issues: set[str] = set()
        self.stale = False
        self._usage_weight = 0.0
        self._usage_possessions = 0

    @property
    def span(self) -> str | None:
        return _season_span(self.covered)

    @property
    def usage(self) -> float | None:
        if not self._usage_possessions:
            return None
        return round(self._usage_weight / self._usage_possessions, 2)

    def per36(self, keys: Sequence[str]) -> float | None:
        if not self.minutes or not self.totals:
            return None
        return sum(self.totals[key] for key in keys) * 36 / self.minutes

    def add(self, season: str, response: Mapping[str, Any], teammate_id: int) -> None:
        if response["status"] != "ok":
            self.issues.add(response["status"])
            return
        self.covered.append(season)
        self.stale = self.stale or bool(response["stale"])
        entry = next(
            (p for p in response["players"] if p["player_id"] == teammate_id), None
        )
        if entry is None:
            return
        self.minutes += entry["minutes"]
        for key, value in entry["totals"].items():
            self.totals[key] = self.totals.get(key, 0) + value
        if entry["usage"] is not None:
            self._usage_weight += entry["usage"] * entry["possessions"]
            self._usage_possessions += entry["possessions"]


class _Teammate:
    """One rotation teammate's evidence, rendered per branch."""

    def __init__(
        self,
        *,
        teammate_id: int,
        name: str,
        window: list[_WindowSeason],
        reads: _OnOffReads,
        lines: Mapping[str, Mapping[str, float]],
    ) -> None:
        self.teammate_id = teammate_id
        self.name = name
        self.window = window
        self.reads = reads
        self.lines = lines

    # -- full-game evidence -------------------------------------------------

    def _games(self, with_player: bool) -> tuple[pd.DataFrame, list[str]]:
        frames, spans = [], []
        for w in self.window:
            ids = w.rows["GAME_ID"].astype(str)
            chosen = w.rows[~ids.isin(w.without)] if with_player else w.rows[ids.isin(w.without)]
            if not chosen.empty:
                frames.append(chosen)
                spans.append(w.season)
        if not frames:
            return pd.DataFrame(columns=["GAME_ID", "MIN"]), []
        return pd.concat(frames), spans

    # -- on/off evidence ----------------------------------------------------

    def _floor(self, side: str) -> _Floor:
        floor = _Floor()
        for w in self.window:
            if w.sliceable:
                floor.add(w.season, self.reads.get(w.season, side), self.teammate_id)
        return floor

    def _on_off_limits(self, on: _Floor, off: _Floor) -> tuple[list[str], bool]:
        """Limits the on/off evidence carries and whether it is complete.

        Complete means both states cover exactly the seasons whose totals can
        be sliced to the window, so the two per-36 rates are comparable.
        """

        needed = [w.season for w in self.window if w.sliceable]
        limits: list[str] = []
        if len(needed) != len(self.window):
            limits.append("on_off_unsliceable")
        issues = on.issues | off.issues
        if "unavailable" in issues:
            limits.append("on_off_unavailable")
        if "not_warmed" in issues:
            limits.append("on_off_not_warmed")
        if on.stale or off.stale:
            limits.append("on_off_stale")
        if on.covered != off.covered:
            limits.append("on_off_seasons_differ")
        complete = bool(needed) and on.covered == off.covered == needed
        return limits, complete

    # -- rendering ------------------------------------------------------------

    def rows(self, branch: str, stale_report: bool) -> list[dict[str, Any]]:
        with_games, with_spans = self._games(True)
        without_games, without_spans = self._games(False)
        on_floor, off_floor = self._floor("on"), self._floor("off")
        rendered = [
            self._scored(market, branch, with_games, with_spans, without_games,
                         without_spans, on_floor, off_floor, stale_report)
            for market in SCORED_MARKETS
        ]
        rendered += [
            self._context(market, branch, with_games, with_spans, without_games,
                          without_spans, on_floor, off_floor)
            for market in CONTEXT_MARKETS
        ]
        return rendered

    def _baseline(self, column, keys, with_games, with_spans, on_floor) -> dict[str, Any]:
        return {
            "avg": _round(_raw_mean(with_games[column].tolist())) if column in with_games else None,
            "games": len(with_games),
            "per36": _round(on_floor.per36(keys)) if keys else None,
            "minutes": on_floor.minutes,
            "season_span": _season_span(with_spans),
        }

    def _scored(
        self, market, branch, with_games, with_spans, without_games, without_spans,
        on_floor, off_floor, stale_report,
    ) -> dict[str, Any]:
        column, keys = SCORED_MARKETS[market]
        row: dict[str, Any] = {
            "player": self.name,
            "player_id": self.teammate_id,
            "market": market,
            "kind": "scored",
            "baseline": self._baseline(column, keys, with_games, with_spans, on_floor),
            "flagged": False,
            "limits": [],
        }
        if branch == "plays":
            return row
        values = without_games[column].tolist() if column in without_games else []
        without_avg = _raw_mean(values)
        with_avg = _raw_mean(with_games[column].tolist()) if column in with_games else None
        off_per36 = off_floor.per36(keys)
        row["without"] = {
            "avg": _round(without_avg), "games": len(without_games),
            "season_span": _season_span(without_spans),
        }
        row["off_floor"] = {
            "per36": _round(off_per36), "minutes": off_floor.minutes,
            "season_span": off_floor.span,
        }
        limits = row["limits"]
        on_off_limits, complete = self._on_off_limits(on_floor, off_floor)
        enough_games = len(without_games) >= MIN_GAMES_WITHOUT
        short_minutes = bool(off_floor.covered) and off_floor.minutes < MIN_OFF_FLOOR_MINUTES
        insufficient = not enough_games or short_minutes
        if insufficient:
            limits.append("insufficient")
        limits.extend(on_off_limits)
        posted = _posted_line(self.lines, market)
        if posted is None:
            limits.append("no_line")
            row["line"] = None
        else:
            value, source = posted
            row["line"] = {
                "value": value, "source": source,
                "beat": sum(1 for stat in values if stat > value),
                "of": len(values),
            }
        if stale_report:
            limits.append("stale_report")
        if insufficient or not complete:
            return row
        full_shift = _shift(without_avg, with_avg)
        on_off_shift = _shift(off_per36, on_floor.per36(keys))
        if full_shift is None or on_off_shift is None:
            limits.append("no_baseline")
        row["shift"] = {
            "full_game": _round(full_shift, 4), "on_off_per36": _round(on_off_shift, 4),
        }
        row["flagged"] = _agree(full_shift, on_off_shift)
        return row

    def _context(
        self, market, branch, with_games, with_spans, without_games, without_spans,
        on_floor, off_floor,
    ) -> dict[str, Any]:
        column = {"MIN": "MIN", "FGA": "FGA"}.get(market)
        keys = ("fga",) if market == "FGA" else ()
        if market == "USG":
            baseline = {"usage": on_floor.usage, "minutes": on_floor.minutes,
                        "season_span": on_floor.span}
        else:
            baseline = self._baseline(column, keys, with_games, with_spans, on_floor)
        row: dict[str, Any] = {
            "player": self.name,
            "player_id": self.teammate_id,
            "market": market,
            "kind": "context",
            "baseline": baseline,
            "flagged": False,
            "limits": [],
        }
        if branch == "plays":
            return row
        if column is not None:
            row["without"] = {
                "avg": _round(_raw_mean(without_games[column].tolist())) if column in without_games else None,
                "games": len(without_games),
                "season_span": _season_span(without_spans),
            }
        row["off_floor"] = (
            {"usage": off_floor.usage} if market == "USG"
            else {"per36": _round(off_floor.per36(keys))} if keys else None
        )
        if row["off_floor"] is not None:
            row["off_floor"].update(
                {"minutes": off_floor.minutes, "season_span": off_floor.span}
            )
            row["limits"], _ = self._on_off_limits(on_floor, off_floor)
        return row


__all__ = ["InjuryScenarioService", "SCENARIO_STATUSES"]
