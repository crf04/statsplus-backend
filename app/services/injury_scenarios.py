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

from app.services.research_season import previous_nba_season, schedule_season

#: Statuses that get a scenario; Probable and Available get none.
SCENARIO_STATUSES = ("Out", "Doubtful", "Questionable")
#: Statuses with both a "plays" and a "sits" branch.
UNCERTAIN_STATUSES = frozenset({"Doubtful", "Questionable"})

MIN_GAMES_WITHOUT = 3
MIN_OFF_FLOOR_MINUTES = 150
ROTATION_MINUTES_PER_GAME = 15.0
FLAG_SHIFT = 0.10

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


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _shift(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None or baseline <= 0:
        return None
    return round(value / baseline - 1, 4)


def _agree(first: float | None, second: float | None) -> bool:
    if first is None or second is None:
        return False
    return (first >= FLAG_SHIFT and second >= FLAG_SHIFT) or (
        first <= -FLAG_SHIFT and second <= -FLAG_SHIFT
    )


class _Logs:
    """One player's stored logs for one season, as plain rows."""

    def __init__(self, frame: pd.DataFrame | None) -> None:
        self.frame = frame if frame is not None and not frame.empty else None

    def absent_frame(self) -> pd.DataFrame:
        """Frame for the absence rule; an empty one when he has no rows."""

        if self.frame is None:
            return pd.DataFrame({"GAME_ID": [], "TEAM_ABBREVIATION": []})
        return self.frame

    def teams(self) -> set[str]:
        return set() if self.frame is None else set(self.frame["TEAM_ABBREVIATION"])

    def team_rows(self, team: str) -> pd.DataFrame | None:
        if self.frame is None:
            return None
        rows = self.frame[self.frame["TEAM_ABBREVIATION"] == team]
        return None if rows.empty else rows


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

    # -- entry point -------------------------------------------------------

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

    # -- one injured player -------------------------------------------------

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
        seasons = [season, previous_nba_season(season)]
        logs = {s: _Logs(self._logs(injured_id, s)) for s in seasons}
        injured_logs = logs
        on_off: dict[str, dict[str, Any]] = {}
        for s in seasons:
            if s != season and not self._stayed(injured_logs[s], injured_logs[season], team):
                continue
            on_off[s] = {
                "off": self.on_off.get(team, s, off=[injured_id]),
                "on": self.on_off.get(team, s, on=[injured_id]),
            }
        candidates: dict[int, str] = {}
        for sides in on_off.values():
            for response in sides.values():
                for entry in response["players"]:
                    if entry["player_id"] != injured_id:
                        candidates.setdefault(entry["player_id"], entry["name"])
        pool_lines = self._pool_lines(season, game_id)
        teammates = []
        for teammate_id, name in sorted(candidates.items()):
            built = self._teammate(
                teammate_id, name, injured_id, team, season, seasons,
                injured_logs, on_off, pool_lines,
            )
            if built is not None:
                teammates.append(built)
        return teammates

    @staticmethod
    def _stayed(prior: _Logs, current: _Logs, team: str) -> bool:
        """Whether a player played last season only for ``team`` and never left it."""

        return bool(prior.teams()) and prior.teams() == {team} and current.teams() <= {team}

    # -- one teammate ---------------------------------------------------------

    def _teammate(
        self,
        teammate_id: int,
        name: str,
        injured_id: int,
        team: str,
        season: str,
        seasons: Sequence[str],
        injured_logs: Mapping[str, _Logs],
        on_off: Mapping[str, Mapping[str, Any]],
        pool_lines: Mapping[int, Mapping[str, Mapping[str, float]]],
    ) -> "_Teammate | None":
        mates = {s: _Logs(self._logs(teammate_id, s)) for s in seasons}
        # Games where both were on ``team`` together, season by season.  The
        # current season starts when the later of the two joined the team; a
        # team change resets the pair, so last season counts only if neither
        # player left ``team`` in it or since.
        window: list[tuple[str, pd.DataFrame, set[str]]] = []
        for s in seasons:
            rows = mates[s].team_rows(team)
            if rows is None:
                continue
            if s == season:
                injured_rows = injured_logs[s].team_rows(team)
                if injured_logs[s].frame is not None and injured_rows is None:
                    continue
                start = rows["GAME_DATE"].min()
                if injured_rows is not None:
                    start = max(start, injured_rows["GAME_DATE"].min())
                rows = rows[rows["GAME_DATE"] >= start]
            elif not (
                self._stayed(injured_logs[s], injured_logs[season], team)
                and self._stayed(mates[s], mates[season], team)
            ):
                continue
            without = self.games.games_without_player(rows, injured_logs[s].absent_frame())
            window.append((s, rows, without))
        if not window:
            return None
        all_rows = pd.concat([rows for _, rows, _ in window])
        if float(all_rows["MIN"].mean()) < ROTATION_MINUTES_PER_GAME:
            return None
        return _Teammate(
            teammate_id=teammate_id,
            name=name,
            window=window,
            on_off=on_off,
            lines=pool_lines.get(teammate_id, {}),
            seasons=[s for s, _, _ in window],
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


class _Teammate:
    """One rotation teammate's evidence, rendered per branch."""

    def __init__(
        self,
        *,
        teammate_id: int,
        name: str,
        window: list[tuple[str, pd.DataFrame, set[str]]],
        on_off: Mapping[str, Mapping[str, Any]],
        lines: Mapping[str, Mapping[str, float]],
        seasons: list[str],
    ) -> None:
        self.teammate_id = teammate_id
        self.name = name
        self.window = window
        self.on_off = on_off
        self.lines = lines
        self.seasons = seasons

    # -- full-game evidence -------------------------------------------------

    def _games(self, with_player: bool) -> tuple[pd.DataFrame, list[str]]:
        frames, spans = [], []
        for season, rows, without in self.window:
            ids = rows["GAME_ID"].astype(str)
            chosen = rows[~ids.isin(without)] if with_player else rows[ids.isin(without)]
            if not chosen.empty:
                frames.append(chosen)
                spans.append(season)
        if not frames:
            return pd.DataFrame(columns=["GAME_ID", "MIN"]), []
        return pd.concat(frames), spans

    # -- on/off evidence ----------------------------------------------------

    def _floor(self, side: str) -> dict[str, Any]:
        """Totals, minutes and usage for one lineup state over the window."""

        totals: dict[str, int] = {}
        minutes = 0
        usage_weight = 0.0
        usage_possessions = 0
        spans: list[str] = []
        unavailable = stale = False
        for season in self.seasons:
            response = self.on_off.get(season, {}).get(side)
            if response is None:
                continue
            if response["status"] != "ok":
                unavailable = True
                continue
            stale = stale or bool(response["stale"])
            entry = next(
                (p for p in response["players"] if p["player_id"] == self.teammate_id),
                None,
            )
            if entry is None:
                continue
            spans.append(season)
            minutes += entry["minutes"]
            for key, value in entry["totals"].items():
                totals[key] = totals.get(key, 0) + value
            if entry["usage"] is not None:
                usage_weight += entry["usage"] * entry["possessions"]
                usage_possessions += entry["possessions"]
        return {
            "totals": totals,
            "minutes": minutes,
            "usage": round(usage_weight / usage_possessions, 2) if usage_possessions else None,
            "span": _season_span(spans),
            "unavailable": unavailable,
            "stale": stale,
        }

    @staticmethod
    def _per36(floor: Mapping[str, Any], keys: Sequence[str]) -> float | None:
        if not floor["minutes"] or not floor["totals"]:
            return None
        return round(sum(floor["totals"][key] for key in keys) * 36 / floor["minutes"], 2)

    # -- rendering ------------------------------------------------------------

    def rows(self, branch: str, stale_report: bool) -> list[dict[str, Any]]:
        with_games, with_spans = self._games(True)
        without_games, without_spans = self._games(False)
        on_floor = self._floor("on")
        off_floor = self._floor("off")
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
            "avg": _mean(with_games[column].tolist()) if column in with_games else None,
            "games": len(with_games),
            "per36": self._per36(on_floor, keys) if keys else None,
            "minutes": on_floor["minutes"],
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
        without_avg = _mean(values)
        off_per36 = self._per36(off_floor, keys)
        row["without"] = {
            "avg": without_avg, "games": len(without_games),
            "season_span": _season_span(without_spans),
        }
        row["off_floor"] = {
            "per36": off_per36, "minutes": off_floor["minutes"],
            "season_span": off_floor["span"],
        }
        limits = row["limits"]
        if off_floor["unavailable"]:
            limits.append("on_off_unavailable")
        elif off_floor["stale"]:
            limits.append("on_off_stale")
        enough_games = len(without_games) >= MIN_GAMES_WITHOUT
        enough_minutes = off_floor["minutes"] >= MIN_OFF_FLOOR_MINUTES
        if not enough_games or (not off_floor["unavailable"] and not enough_minutes):
            limits.insert(0, "insufficient")
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
        if "insufficient" in limits or "on_off_unavailable" in limits:
            return row
        full_shift = _shift(without_avg, row["baseline"]["avg"])
        on_off_shift = _shift(off_per36, row["baseline"]["per36"])
        if full_shift is None or on_off_shift is None:
            limits.append("no_baseline")
        row["shift"] = {"full_game": full_shift, "on_off_per36": on_off_shift}
        row["flagged"] = _agree(full_shift, on_off_shift)
        return row

    def _context(
        self, market, branch, with_games, with_spans, without_games, without_spans,
        on_floor, off_floor,
    ) -> dict[str, Any]:
        column = {"MIN": "MIN", "FGA": "FGA"}.get(market)
        keys = ("fga",) if market == "FGA" else ()
        if market == "USG":
            baseline = {"usage": on_floor["usage"], "minutes": on_floor["minutes"],
                        "season_span": on_floor["span"]}
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
                "avg": _mean(without_games[column].tolist()) if column in without_games else None,
                "games": len(without_games),
                "season_span": _season_span(without_spans),
            }
        row["off_floor"] = (
            {"usage": off_floor["usage"]} if market == "USG"
            else {"per36": self._per36(off_floor, keys)} if keys else None
        )
        if row["off_floor"] is not None:
            row["off_floor"].update(
                {"minutes": off_floor["minutes"], "season_span": off_floor["span"]}
            )
        if off_floor["unavailable"] and row["off_floor"] is not None:
            row["limits"].append("on_off_unavailable")
        return row


__all__ = ["InjuryScenarioService", "SCENARIO_STATUSES"]
