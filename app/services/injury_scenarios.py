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
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.services.posted_lines import posted_line
from app.services.research_season import previous_nba_season, schedule_season
from app.services.rotation_service import classify_games, rotation_split

#: Statuses that get a scenario; Probable and Available get none.
SCENARIO_STATUSES = ("Out", "Doubtful", "Questionable")
#: Statuses with both a "plays" and a "sits" branch.
UNCERTAIN_STATUSES = frozenset({"Doubtful", "Questionable"})

MIN_GAMES_WITHOUT = 3
MIN_OFF_FLOOR_MINUTES = 150
ROTATION_MINUTES_PER_GAME = 15.0
#: A teammate under the rotation line is a minutes jump at this change or more.
MINUTES_JUMP = 6.0
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
    return (first >= reach and second >= reach) or (first <= -reach and second <= -reach)


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
            self._memo[key] = self.on_off.get_cached(self.team, season, **{side: [self.injured_id]})
        return self._memo[key]


class _WindowSeason:
    """One season of a pair's window."""

    def __init__(self, season: str, rows: pd.DataFrame, without: set[str], sliceable: bool) -> None:
        self.season = season
        self.rows = rows
        self.without = without
        #: Whether on/off totals for this season match the window's games: false
        #: when either player changed teams in it.
        self.sliceable = sliceable


class _RequestReads:
    """Everything one ``scenarios()`` request reads, each at most once.

    Scenarios on one team re-read the same roster and teammate logs; this
    memoizes the roster, the log frame per ``(player, season)`` (loaded for a
    whole roster in one batched read per season), the catalog and each team
    season's stored rotation lines.  Nothing outlives the request.
    """

    def __init__(self, service: InjuryScenarioService) -> None:
        self._service = service
        self._logs: dict[tuple[int, str], pd.DataFrame | None] = {}
        self._rosters: dict[tuple[int, str], set[int]] = {}
        self._catalog: dict[str, dict[int, int]] = {}
        self._rotation_lines: dict[tuple[str, str], dict[str, dict[int, dict[str, Any]]]] = {}

    def roster(self, team_id: int, season: str) -> set[int]:
        key = (team_id, season)
        if key not in self._rosters:
            self._rosters[key] = set(self._service.games.get_team_player_ids(team_id, season))
        return self._rosters[key]

    def catalog_teams(self, season: str) -> dict[int, int]:
        if season not in self._catalog:
            self._catalog[season] = self._service._catalog_teams(season)
        return self._catalog[season]

    def load_logs(self, player_ids: Iterable[int], season: str) -> None:
        """Read the logs of every player not yet read for the season in one batch."""

        missing = {int(pid) for pid in player_ids if (int(pid), season) not in self._logs}
        if not missing:
            return
        games = self._service.games
        batch = getattr(games, "get_players_logs_by_id", None)
        if not callable(batch):
            return  # the per-player read in ``logs`` serves a games seam without one
        frames = batch(missing, season)
        for pid in missing:
            self._logs[(pid, season)] = frames.get(pid)

    def logs(self, player_id: int, season: str) -> pd.DataFrame | None:
        key = (int(player_id), season)
        if key not in self._logs:
            self._logs[key] = self._service._logs(*key)
        return self._logs[key]

    def rotation_lines(
        self, rotations: Any | None, team: str, season: str
    ) -> dict[str, dict[int, dict[str, Any]]]:
        key = (team, season)
        if key not in self._rotation_lines:
            lines: dict[str, dict[int, dict[str, Any]]] = {}
            if rotations is not None:
                for row in rotations.player_games(season, team=team):
                    lines.setdefault(row["game_id"], {})[row["player_id"]] = row
            self._rotation_lines[key] = lines
        return self._rotation_lines[key]


class _RotationReads:
    """One injured player's stored rotation lines for his team, read once per season."""

    def __init__(self, rotations: Any | None, team: str, injured_id: int, request: _RequestReads) -> None:
        self.rotations = rotations
        self.team = team
        self.injured_id = injured_id
        self.request = request
        self._memo: dict[str, tuple[dict[str, dict[int, dict[str, Any]]], list[str], list[str]]] = {}

    def season(self, season: str) -> tuple[dict[str, dict[int, dict[str, Any]]], list[str], list[str]]:
        """Lines by game then player (newest game first), then his without and with game ids."""

        if season not in self._memo:
            lines = self.request.rotation_lines(self.rotations, self.team, season)
            without, played = classify_games(lines, [self.injured_id])
            self._memo[season] = (lines, without, played)
        return self._memo[season]


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
        rotations: Any | None = None,
    ) -> None:
        self.injuries = injuries
        self.on_off = on_off
        self.games = games
        self.player_pool = player_pool
        self.event_catalog = event_catalog
        self.settings = settings
        #: The rotation repository (``player_games``); ``None`` reads no rotations.
        self.rotations = rotations

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
        selected = []
        for row in listing["injuries"]:
            if row["status"] not in SCENARIO_STATUSES:
                continue
            if game_id and row["game_id"] != game_id:
                continue
            if wanted and wanted not in (str(row["player_id"]), row["player"].casefold()):
                continue
            selected.append(row)
        reads = _RequestReads(self)
        self._prefetch(selected, season, reads)
        return {
            "date": listing["date"],
            "scenarios": [self._scenario(row, season, reads) for row in selected],
        }

    def _prefetch(self, rows: Sequence[Mapping[str, Any]], season: str, reads: _RequestReads) -> None:
        """Read every log the request needs: one batch per season, whatever the scenarios."""

        seasons = [previous_nba_season(season), season]
        wanted: set[int] = set()
        for row in rows:
            if row["player_id"] is None:
                continue
            injured_id = int(row["player_id"])
            wanted.add(injured_id)
            wanted |= self._roster(injured_id, row["team"], seasons, season, reads)
        for s in seasons:
            reads.load_logs(wanted, s)

    def _roster(
        self, injured_id: int, team: str, seasons: Sequence[str], season: str, reads: _RequestReads
    ) -> set[int]:
        """The teammates a scenario considers: the team's rosters, less those the catalog places elsewhere."""

        team_id = NBA_TEAM_TRICODE_TO_ID[team]
        roster: set[int] = set()
        for s in seasons:
            roster |= reads.roster(team_id, s)
        roster.discard(injured_id)
        current_team = reads.catalog_teams(season)
        return {pid for pid in roster if current_team.get(pid) in (None, team_id)}

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

    def _scenario(self, row: Mapping[str, Any], season: str, reads: _RequestReads) -> dict[str, Any]:
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
            "rotation_starts": None,
            "replacement_starters": [],
            "minutes_jumps": [],
        }
        if row["player_id"] is None:
            limits.append("player_unresolved")
            return scenario
        branches = ["plays", "sits"] if row["status"] in UNCERTAIN_STATUSES else ["sits"]
        injured_id = int(row["player_id"])
        evidence, rotation_starts = self._teammates(injured_id, row["team"], season, row["game_id"], reads)
        for branch in branches:
            scenario["branches"].append(
                {
                    "branch": branch,
                    "rows": [
                        market_row
                        for teammate in evidence
                        if teammate.in_rotation
                        for market_row in teammate.rows(branch, bool(row["stale"]))
                    ],
                }
            )
        scenario["rotation_starts"] = rotation_starts
        scenario["replacement_starters"] = _replacement_starters(evidence)
        scenario["minutes_jumps"] = _minutes_jumps(evidence)
        return scenario

    def _teammates(
        self, injured_id: int, team: str, season: str, game_id: str, request: _RequestReads
    ) -> tuple[list[_Teammate], dict[str, int]]:
        """Every teammate in the pair window (rotation or not) and the injured player's starts."""

        seasons = [previous_nba_season(season), season]  # chronological
        injured_logs = {s: _Logs(request.logs(injured_id, s)) for s in seasons}
        reads = _OnOffReads(self.on_off, team, injured_id)
        rotation_reads = _RotationReads(self.rotations, team, injured_id, request)
        roster = self._roster(injured_id, team, seasons, season, request)
        pool_lines = self._pool_lines(season, game_id)
        teammates = []
        for teammate_id in sorted(roster):
            built = self._teammate(
                teammate_id,
                injured_id,
                team,
                seasons,
                injured_logs,
                reads,
                rotation_reads,
                pool_lines,
                request,
            )
            if built is not None:
                teammates.append(built)
        return teammates, self._rotation_starts(team, seasons, injured_logs, rotation_reads)

    @staticmethod
    def _rotation_starts(
        team: str,
        seasons: Sequence[str],
        injured_logs: Mapping[str, _Logs],
        rotation_reads: _RotationReads,
    ) -> dict[str, int]:
        """His starts over his own with games, after his own last team change."""

        others = [
            rows
            for rows in (logs.other_team_rows(team) for logs in injured_logs.values())
            if rows is not None
        ]
        after = _as_date(max((rows["GAME_DATE"].max() for rows in others), default=None))
        starts = games = 0
        for s in seasons:
            lines, _, played = rotation_reads.season(s)
            for game_id in played:
                line = lines[game_id][rotation_reads.injured_id]
                if after is None or line["game_date"] > after:
                    games += 1
                    starts += bool(line["started"])
        return {"starts": starts, "games": games}

    def _teammate(
        self,
        teammate_id: int,
        injured_id: int,
        team: str,
        seasons: Sequence[str],
        injured_logs: Mapping[str, _Logs],
        reads: _OnOffReads,
        rotation_reads: _RotationReads,
        pool_lines: Mapping[int, Mapping[str, Mapping[str, float]]],
        request: _RequestReads,
    ) -> _Teammate | None:
        mates = {s: _Logs(request.logs(teammate_id, s)) for s in seasons}
        # A team change resets the pair: the window starts after either
        # player's last appearance for a different team.  That also drops a
        # teammate whose latest game was for another team (he has left).
        others = [logs.other_team_rows(team) for logs in (*injured_logs.values(), *mates.values())]
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
            window.append(
                _WindowSeason(
                    s,
                    rows,
                    self.games.games_without_player(rows, injured_logs[s].absent_frame()),
                    sliceable=not changed,
                )
            )
        if not window:
            return None
        all_rows = pd.concat([w.rows for w in window])
        in_rotation = float(all_rows["MIN"].mean()) >= ROTATION_MINUTES_PER_GAME
        name = next((mates[s].name() for s in reversed(seasons) if mates[s].name()), None)
        return _Teammate(
            teammate_id=teammate_id,
            name=name or str(teammate_id),
            window=window,
            reads=reads,
            lines=pool_lines.get(teammate_id, {}),
            in_rotation=in_rotation,
            rotation=self._rotation_split(teammate_id, window, _as_date(last_other), rotation_reads),
        )

    @staticmethod
    def _rotation_split(
        teammate_id: int,
        window: Sequence[_WindowSeason],
        after: Any,
        rotation_reads: _RotationReads,
    ) -> dict[str, Any] | None:
        """His stored rotation split with the injured player absent, over the pair window."""

        without_lines: list[Mapping[str, Any]] = []
        with_lines: list[Mapping[str, Any]] = []
        contributing: list[str] = []
        missing = 0
        for w in window:
            lines, without_ids, with_ids = rotation_reads.season(w.season)
            missing += sum(1 for game_id in w.without if game_id not in lines)
            mine = (
                [lines[g][teammate_id] for g in ids if teammate_id in lines[g]]
                for ids in (without_ids, with_ids)
            )
            season_without, season_with = (
                [line for line in side if after is None or line["game_date"] > after] for side in mine
            )
            if season_without or season_with:
                contributing.append(w.season)
            without_lines += season_without
            with_lines += season_with
        if not without_lines and not with_lines:
            return None
        return {
            **rotation_split(without_lines, with_lines),
            "seasons": _season_span(contributing),
            "missing_games": missing,
        }

    # -- reads ---------------------------------------------------------------

    def _catalog_teams(self, season: str) -> dict[int, int]:
        """Current team id by player from the Athlete Catalog.

        A player the catalog does not list, or a catalog that cannot be read,
        is simply absent: the game-log rule decides for him.
        """

        catalog = getattr(self.games, "athlete_catalog", None)
        if catalog is None:
            return {}
        try:
            rows = catalog.get_catalog(season, active_only=False)
        except (SQLAlchemyError, ValueError):
            return {}
        return {int(row["player_id"]): int(row["team_id"]) for row in rows if row.get("team_id") is not None}

    def _logs(self, player_id: int, season: str) -> pd.DataFrame | None:
        return self.games.get_player_logs_by_id(player_id, season)

    def _pool_lines(self, season: str, game_id: str) -> dict[int, dict[str, dict[str, float]]]:
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


def _as_date(value: Any) -> Any:
    """A game-log date (string or timestamp) as a ``date``; ``None`` stays ``None``."""

    return None if value is None else pd.Timestamp(value).date()


def _replacement_starters(teammates: Sequence[_Teammate]) -> list[dict[str, Any]]:
    """Teammates who started more often without him than with him, most starts first."""

    found = []
    for teammate in teammates:
        split = teammate.rotation
        if split is None or split["insufficient"]:
            continue
        without, played = split["without"], split["with"]
        if without["starts"] < 1:
            continue
        # starts/games compared exactly; a side with no games has rate 0.
        with_numerator, with_games = played["starts"], max(played["games"], 1)
        if without["starts"] * with_games <= with_numerator * without["games"]:
            continue
        found.append(
            {
                "name": teammate.name,
                "starts_without": without["starts"],
                "games_without": without["games"],
                "starts_with": played["starts"],
                "games_with": played["games"],
            }
        )
    found.sort(key=lambda entry: (-entry["starts_without"], entry["name"]))
    return found


def _minutes_jumps(teammates: Sequence[_Teammate]) -> list[dict[str, Any]]:
    """Under-the-line teammates whose minutes rise by ``MINUTES_JUMP`` or more without him."""

    jumps = [
        teammate
        for teammate in teammates
        if not teammate.in_rotation
        and teammate.rotation is not None
        and not teammate.rotation["insufficient"]
        and teammate.rotation["minutes_change"] is not None
        and teammate.rotation["minutes_change"] >= MINUTES_JUMP
    ]
    jumps.sort(key=lambda teammate: (-teammate.rotation["minutes_change"], teammate.name))
    return [{"name": teammate.name, "rotation": teammate.rotation} for teammate in jumps]


class _Floor:
    """One lineup state's on/off evidence, summed over the seasons it covers."""

    def __init__(self) -> None:
        self.totals: dict[str, int] = {}
        self.minutes = 0
        self.covered: list[str] = []
        #: Seasons that actually contributed minutes; the span it labels.
        self.contributed: list[str] = []
        self.issues: set[str] = set()
        self.stale = False
        self._usage_weight = 0.0
        self._usage_possessions = 0

    @property
    def span(self) -> str | None:
        return _season_span(self.contributed)

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
        entry = next((p for p in response["players"] if p["player_id"] == teammate_id), None)
        if entry is None:
            return
        self.minutes += entry["minutes"]
        if entry["minutes"] > 0:
            self.contributed.append(season)
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
        in_rotation: bool = True,
        rotation: dict[str, Any] | None = None,
    ) -> None:
        self.teammate_id = teammate_id
        #: Whether he is in the 15+ MPG teammate set that gets scored rows.
        self.in_rotation = in_rotation
        self.rotation = rotation
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
        # Any unsliceable season in the window blocks the flag: the full-game
        # evidence would span games the on/off totals do not.
        complete = bool(needed) and len(needed) == len(self.window) and on.covered == off.covered == needed
        return limits, complete

    # -- rendering ------------------------------------------------------------

    def rows(self, branch: str, stale_report: bool) -> list[dict[str, Any]]:
        with_games, with_spans = self._games(True)
        without_games, without_spans = self._games(False)
        on_floor, off_floor = self._floor("on"), self._floor("off")
        rendered = [
            self._scored(
                market,
                branch,
                with_games,
                with_spans,
                without_games,
                without_spans,
                on_floor,
                off_floor,
                stale_report,
            )
            for market in SCORED_MARKETS
        ]
        rendered += [
            self._context(
                market, branch, with_games, with_spans, without_games, without_spans, on_floor, off_floor
            )
            for market in CONTEXT_MARKETS
        ]
        for rendered_row in rendered:
            rendered_row["rotation"] = self.rotation
        return rendered

    def _baseline(self, column, keys, with_games, with_spans, on_floor) -> dict[str, Any]:
        return {
            "avg": _round(_raw_mean(with_games[column].tolist())) if column in with_games else None,
            "games": len(with_games),
            "per36": _round(on_floor.per36(keys)) if keys else None,
            "minutes": on_floor.minutes,
            "season_span": _season_span(with_spans),
            "on_floor_season_span": on_floor.span,
        }

    def _scored(
        self,
        market,
        branch,
        with_games,
        with_spans,
        without_games,
        without_spans,
        on_floor,
        off_floor,
        stale_report,
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
            "avg": _round(without_avg),
            "games": len(without_games),
            "season_span": _season_span(without_spans),
        }
        row["off_floor"] = {
            "per36": _round(off_per36),
            "minutes": off_floor.minutes,
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
        posted = posted_line(self.lines, market)
        if posted is None:
            limits.append("no_line")
            row["line"] = None
        else:
            value, source = posted
            row["line"] = {
                "value": value,
                "source": source,
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
            "full_game": _round(full_shift, 4),
            "on_off_per36": _round(on_off_shift, 4),
            "season_span": {
                "full_game": row["without"]["season_span"],
                "on_off": off_floor.span,
            },
        }
        row["flagged"] = _agree(full_shift, on_off_shift)
        return row

    def _context(
        self,
        market,
        branch,
        with_games,
        with_spans,
        without_games,
        without_spans,
        on_floor,
        off_floor,
    ) -> dict[str, Any]:
        column = {"MIN": "MIN", "FGA": "FGA"}.get(market)
        keys = ("fga",) if market == "FGA" else ()
        if market == "USG":
            baseline = {"usage": on_floor.usage, "minutes": on_floor.minutes, "season_span": on_floor.span}
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
            {"usage": off_floor.usage}
            if market == "USG"
            else {"per36": _round(off_floor.per36(keys))}
            if keys
            else None
        )
        if row["off_floor"] is not None:
            row["off_floor"].update({"minutes": off_floor.minutes, "season_span": off_floor.span})
            row["limits"], _ = self._on_off_limits(on_floor, off_floor)
        return row


__all__ = ["SCENARIO_STATUSES", "InjuryScenarioService"]
