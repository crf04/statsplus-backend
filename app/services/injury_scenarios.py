"""Injury Scenarios behind ``GET /api/injury_scenarios`` (crf04/statsplus#220).

For each player the stored report lists as Questionable, Doubtful or Out, this
combines three kinds of evidence about every rotation teammate:

* the full-game split in games the injured player missed, by the game-log
  ``players_off`` absence rule (``GameService.games_without_and_with_player``);
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
teams contributes full-game evidence only (``on_off_unsliceable``).  Each
season is read scoped to itself, so the Slate's season counts even before it
is the Published Season; every row's ``pair_window`` names where the window
starts and how many of the teammate's games for the team it leaves out
(crf04/statsplus#467).  A request
without ``player`` reads the on/off cache only (``OnOffService.get_cached``);
the warm fills it.  A request naming ``player`` fetches that player's missing
or expired splits during the request (``OnOffService.get`` waiting until a request-wide deadline for the
upstream slot, within the caller's upstream budget), so its rows never carry
``on_off_not_warmed``.

Baseline
--------
Each kind of evidence is compared with its own "with him" figure: the
full-game split with the teammate's average in games the injured player
played, the on/off split with the teammate's per-36 while the injured player
is on the floor (``on[]``).  The "plays" branch reports those baselines only.

Seasons
-------
Every sample is also reported per season (``seasons``), and a flag is computed
on one season only (crf04/statsplus#485): the Slate season when the teammate
has ``MIN_GAMES_WITHOUT`` games on each side in it, otherwise last season.
That flag is the ``seasons`` entry for ``flag_season``.  For one release the
row's own figures, ``shift``, ``flagged`` and ``limits`` stay pooled across the
window's seasons under the old rule, so a reader that predates the change
prints what it printed before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.errors import InvalidInputError
from app.services.posted_lines import posted_line
from app.services.research_season import previous_nba_season, schedule_season, season_scoped
from app.services.rotation_service import absent_windows, classify_games, rotation_split

#: Statuses that get a scenario; Probable and Available get none.
SCENARIO_STATUSES = ("Out", "Doubtful", "Questionable")
#: Statuses with both a "plays" and a "sits" branch.
UNCERTAIN_STATUSES = frozenset({"Doubtful", "Questionable"})
#: A report reason with this prefix is a G League or two-way assignment, not an
#: injury: it opens no scenario unless the request names the player.
G_LEAGUE_REASON = "G League"

MIN_GAMES_WITHOUT = 3
MIN_OFF_FLOOR_MINUTES = 150
#: A season's baseline (with him) needs this much to flag (crf04/statsplus#485).
MIN_GAMES_WITH = 3
MIN_ON_FLOOR_MINUTES = 150
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


def _on_off_provenance(*floors: _Floor) -> dict[str, Any]:
    """Where a row's on/off splits came from: ``on_off_sources`` (sorted,
    distinct) and ``on_off_games_missing`` (their union, oldest first)."""

    missing: dict[str, Mapping[str, Any]] = {}
    for floor in floors:
        missing.update(floor.games_missing)
    return {
        "on_off_sources": sorted(set().union(*(floor.sources for floor in floors))),
        "on_off_games_missing": sorted(
            (dict(game) for game in missing.values()),
            key=lambda game: (game.get("game_date") or "", game["game_id"]),
        ),
    }


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
            return pd.DataFrame({"GAME_ID": [], "GAME_DATE": [], "TEAM_ABBREVIATION": []})
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
    """On/off reads for one injured player, one per season and side.

    Cache-only, unless the request named a player: then a missing or expired
    split is fetched, once, for ``user_id``'s upstream budget and before the
    request's one on/off deadline.
    """

    def __init__(self, on_off: Any, team: str, injured_id: int, request: _RequestReads) -> None:
        self.on_off = on_off
        self.team = team
        self.injured_id = injured_id
        self._fetch = request.fetch_on_off
        self._user_id = request.user_id
        self._deadline = request.on_off_deadline
        self._memo: dict[tuple[str, str], Mapping[str, Any]] = {}

    def get(self, season: str, side: str) -> Mapping[str, Any]:
        key = (season, side)
        if key not in self._memo:
            splits = {side: [self.injured_id]}
            if self._fetch:
                self._memo[key] = self.on_off.get(
                    self.team, season, **splits, user_id=self._user_id, deadline=self._deadline
                )
            else:
                self._memo[key] = self.on_off.get_cached(self.team, season, **splits)
        return self._memo[key]


class _WindowSeason:
    """One season of a pair's window."""

    def __init__(
        self, season: str, rows: pd.DataFrame, without: set[str], played: set[str], sliceable: bool
    ) -> None:
        self.season = season
        self.rows = rows
        self.without = without
        #: Games inside the injured player's team window that he played.
        self.played = played
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

    def __init__(
        self, service: InjuryScenarioService, *, fetch_on_off: bool = False, user_id: str | None = None
    ) -> None:
        self._service = service
        #: A named-player request fetches the splits it lacks; see ``_OnOffReads``.
        self.fetch_on_off = fetch_on_off
        self.user_id = user_id
        #: One deadline for all of the request's fetches, however many splits.
        self.on_off_deadline = service.on_off.request_deadline() if fetch_on_off else None
        self._logs: dict[tuple[int, str], pd.DataFrame | None] = {}
        self._rosters: dict[tuple[int, str], set[int]] = {}
        self._snapshots: dict[str, Any] = {}
        self._catalog: dict[str, dict[int, int]] = {}
        self._rotation_lines: dict[tuple[str, str], dict[str, dict[int, dict[str, Any]]]] = {}
        self._games: dict[str, Any] = {}

    def games(self, season: str) -> Any:
        """The game service scoped to ``season``.

        The Slate's own season can be after the Published Season (2026-27
        while 2025-26 is published); an unscoped read of it serves no logs at
        all, so every teammate's current-season games would silently drop out
        (crf04/statsplus#467).
        """

        if season not in self._games:
            self._games[season] = season_scoped(self._service.games, season)
        return self._games[season]

    def snapshot(self, season: str) -> Any:
        """The season's publication snapshot, captured once and shared by every read."""

        if season not in self._snapshots:
            capture = getattr(self.games(season), "capture_publication_snapshot", None)
            self._snapshots[season] = capture(season) if callable(capture) else None
        return self._snapshots[season]

    def roster(self, team_id: int, season: str) -> set[int]:
        key = (team_id, season)
        if key not in self._rosters:
            self._rosters[key] = set(
                self.games(season).get_team_player_ids(
                    team_id, season, publication_snapshot=self.snapshot(season)
                )
            )
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
        batch = getattr(self.games(season), "get_players_logs_by_id", None)
        if not callable(batch):
            return  # the per-player read in ``logs`` serves a games seam without one
        frames = batch(missing, season, publication_snapshot=self.snapshot(season))
        for pid in missing:
            self._logs[(pid, season)] = frames.get(pid)

    def logs(self, player_id: int, season: str) -> pd.DataFrame | None:
        key = (int(player_id), season)
        if key not in self._logs:
            self._logs[key] = self.games(season).get_player_logs_by_id(
                *key, publication_snapshot=self.snapshot(season)
            )
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
            windows = absent_windows(self.rotations, season, self.team, [self.injured_id]) if lines else {}
            without, played = classify_games(lines, windows)
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
        *,
        teammate: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        listing = self.injuries.list_injuries(requested_date, None)
        slate_date = datetime.strptime(listing["date"], "%Y-%m-%d").date()
        season = schedule_season(self.settings, self.event_catalog, slate_date)
        wanted = str(player).strip().casefold() if player else None
        selected = []
        g_league_skipped = 0
        for row in listing["injuries"]:
            if row["status"] not in SCENARIO_STATUSES:
                continue
            if game_id and row["game_id"] != game_id:
                continue
            if wanted:
                if wanted not in (str(row["player_id"]), row["player"].casefold()):
                    continue
            elif row["reason"].startswith(G_LEAGUE_REASON):
                g_league_skipped += 1
                continue
            selected.append(row)
        listed_out: dict[str, set[int]] = {}
        for row in listing["injuries"]:
            if row["status"] == "Out" and row["player_id"] is not None:
                listed_out.setdefault(row["team"], set()).add(int(row["player_id"]))
        reads = _RequestReads(self, fetch_on_off=bool(wanted), user_id=user_id)
        self._prefetch(selected, season, reads)
        teammate_ids = None
        if teammate:
            # With no scenario selected, the name is still checked, against every team on the report.
            on_report = selected or listing["injuries"]
            teammate_ids = self._teammate_ids(teammate, on_report, season, reads)
        return {
            "date": listing["date"],
            "g_league_skipped": g_league_skipped,
            "scenarios": [
                self._scenario(row, season, reads, listed_out.get(row["team"], set()), teammate_ids)
                for row in selected
            ],
        }

    @staticmethod
    def _teammate_ids(
        teammate: str, rows: Sequence[Mapping[str, Any]], season: str, reads: _RequestReads
    ) -> set[int]:
        """The id ``teammate`` (an id or full name, case-insensitive) names on the rows' team rosters."""

        wanted = str(teammate).strip().casefold()
        teams = sorted({row["team"] for row in rows})
        found: dict[int, dict[str, Any]] = {}
        for s in (previous_nba_season(season), season):
            roster = {pid: team for team in teams for pid in reads.roster(NBA_TEAM_TRICODE_TO_ID[team], s)}
            reads.load_logs(roster, s)
            for pid, team in roster.items():
                name = _Logs(reads.logs(pid, s)).name() or ""
                if wanted in (str(pid), name.casefold()):
                    found[pid] = {"player_id": pid, "name": name, "team": team}
        if not found:
            raise InvalidInputError(
                "The injury scenarios teammate is not on a listed player's team.",
                public_details={"parameters": ["teammate"]},
            )
        if len(found) > 1:
            raise InvalidInputError(
                "The injury scenarios teammate matches more than one player; name him by player_id.",
                public_details={
                    "parameter": "teammate",
                    "parameters": ["teammate"],
                    "candidates": [found[pid] for pid in sorted(found)],
                },
            )
        return set(found)

    def _prefetch(self, rows: Sequence[Mapping[str, Any]], season: str, reads: _RequestReads) -> None:
        """Read every log the request needs: one batch per season, whatever the scenarios.

        Both seasons' whole team rosters, so each season's team games are known.
        """

        seasons = [previous_nba_season(season), season]
        wanted: set[int] = set()
        for row in rows:
            if row["player_id"] is None:
                continue
            wanted.add(int(row["player_id"]))
            for s in seasons:
                wanted |= reads.roster(NBA_TEAM_TRICODE_TO_ID[row["team"]], s)
        for s in seasons:
            reads.load_logs(wanted, s)

    def _roster(
        self, injured_id: int, team: str, season: str, reads: _RequestReads, listed_out: set[int]
    ) -> set[int]:
        """The teammates a scenario considers (crf04/statsplus#485).

        Once the team has played in the Slate season, those with a game for it
        in that season, less any the Athlete Catalog has since placed on
        another team.  Before its first game the catalog decides among last
        season's roster (an unreadable or empty catalog leaves the roster as
        the game logs have it).  Teammates listed Out on the same report are
        left out.
        """

        team_id = NBA_TEAM_TRICODE_TO_ID[team]
        current_team = reads.catalog_teams(season)
        roster = reads.roster(team_id, season)
        if roster:
            roster = {pid for pid in roster if current_team.get(pid) in (None, team_id)}
        else:
            roster = reads.roster(team_id, previous_nba_season(season))
            if current_team:
                roster = {pid for pid in roster if current_team.get(pid) == team_id}
        return roster - {injured_id} - listed_out

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

    def _scenario(
        self,
        row: Mapping[str, Any],
        season: str,
        reads: _RequestReads,
        listed_out: set[int],
        teammate_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        limits = ["stale_report"] if row["stale"] else []
        scenario: dict[str, Any] = {
            "injured": {
                "player": row["player"],
                "player_id": row["player_id"],
                "team": row["team"],
                "game_id": row["game_id"],
                "status": row["status"],
                "reason": row["reason"],
                "report_time": row["report_time"],
                "seasons": [],
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
        scenario["injured"]["seasons"] = self._injured_seasons(injured_id, row["team"], season, reads)
        evidence, rotation_starts = self._teammates(
            injured_id, row["team"], season, row["game_id"], reads, listed_out
        )
        if teammate_ids is not None:
            evidence = [teammate for teammate in evidence if teammate.teammate_id in teammate_ids]
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
        self,
        injured_id: int,
        team: str,
        season: str,
        game_id: str,
        request: _RequestReads,
        listed_out: set[int],
    ) -> tuple[list[_Teammate], dict[str, int]]:
        """Every teammate in the pair window (rotation or not) and the injured player's starts."""

        seasons = [previous_nba_season(season), season]  # chronological
        injured_logs = {s: _Logs(request.logs(injured_id, s)) for s in seasons}
        reads = _OnOffReads(self.on_off, team, injured_id, request)
        rotation_reads = _RotationReads(self.rotations, team, injured_id, request)
        roster = self._roster(injured_id, team, season, request, listed_out)
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

    def _injured_seasons(
        self, injured_id: int, team: str, season: str, request: _RequestReads
    ) -> list[dict[str, Any]]:
        """His team's games he missed and played, per season, the Slate season first.

        A team game is one any of that season's team roster played for it; the
        game-log absence rule (his team window) sorts it.
        """

        team_id = NBA_TEAM_TRICODE_TO_ID[team]
        counts = []
        for s in (season, previous_nba_season(season)):
            frames = [
                rows
                for pid in sorted(request.roster(team_id, s))
                if (rows := _Logs(request.logs(pid, s)).team_rows(team)) is not None
            ]
            missed: set[str] = set()
            played: set[str] = set()
            if frames:
                team_games = pd.concat(frames).drop_duplicates("GAME_ID")
                missed, played = self.games.games_without_and_with_player(
                    team_games, _Logs(request.logs(injured_id, s)).absent_frame()
                )
            counts.append({"season": s, "missed": len(missed), "played": len(played)})
        return counts

    @staticmethod
    def _rotation_starts(
        team: str,
        seasons: Sequence[str],
        injured_logs: Mapping[str, _Logs],
        rotation_reads: _RotationReads,
    ) -> dict[str, Any]:
        """His starts over his own with games, after his own last team change, in all and per season."""

        others = [
            rows
            for rows in (logs.other_team_rows(team) for logs in injured_logs.values())
            if rows is not None
        ]
        after = _as_date(max((rows["GAME_DATE"].max() for rows in others), default=None))
        by_season = []
        for s in reversed(seasons):
            lines, _, played = rotation_reads.season(s)
            mine = [lines[game_id][rotation_reads.injured_id] for game_id in played]
            counted = [line for line in mine if after is None or line["game_date"] > after]
            by_season.append(
                {"season": s, "starts": sum(bool(line["started"]) for line in counted), "games": len(counted)}
            )
        return {
            "starts": sum(entry["starts"] for entry in by_season),
            "games": sum(entry["games"] for entry in by_season),
            "by_season": by_season,
        }

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
            without, played = self.games.games_without_and_with_player(rows, injured_logs[s].absent_frame())
            window.append(_WindowSeason(s, rows, without, played, sliceable=not changed))
        if not window:
            return None
        pair_window = _pair_window(team, seasons, mates, window)
        all_rows = pd.concat([w.rows for w in window])
        in_rotation = float(all_rows["MIN"].mean()) >= ROTATION_MINUTES_PER_GAME
        name = next((mates[s].name() for s in reversed(seasons) if mates[s].name()), None)
        return _Teammate(
            teammate_id=teammate_id,
            name=name or str(teammate_id),
            window=window,
            reads=reads,
            lines=pool_lines.get(teammate_id, {}),
            slate_season=seasons[-1],
            in_rotation=in_rotation,
            pair_window=pair_window,
            rotation=self._rotation_split(teammate_id, window, _as_date(last_other), rotation_reads),
        )

    @staticmethod
    def _rotation_split(
        teammate_id: int,
        window: Sequence[_WindowSeason],
        after: Any,
        rotation_reads: _RotationReads,
    ) -> dict[str, Any] | None:
        """His stored rotation split with the injured player absent, over the pair window.

        ``by_season`` holds the split of each season that supplied games, the
        Slate season (the newest) first.
        """

        without_lines: list[Mapping[str, Any]] = []
        with_lines: list[Mapping[str, Any]] = []
        contributing: list[str] = []
        by_season: list[dict[str, Any]] = []
        missing = 0
        for w in window:
            lines, without_ids, with_ids = rotation_reads.season(w.season)
            season_missing = sum(1 for game_id in w.without if game_id not in lines)
            missing += season_missing
            mine = (
                [lines[g][teammate_id] for g in ids if teammate_id in lines[g]]
                for ids in (without_ids, with_ids)
            )
            season_without, season_with = (
                [line for line in side if after is None or line["game_date"] > after] for side in mine
            )
            if season_without or season_with:
                contributing.append(w.season)
                by_season.insert(
                    0,
                    {
                        "season": w.season,
                        **rotation_split(season_without, season_with),
                        "missing_games": season_missing,
                    },
                )
            without_lines += season_without
            with_lines += season_with
        if not without_lines and not with_lines:
            return None
        return {
            **rotation_split(without_lines, with_lines),
            "seasons": _season_span(contributing),
            "missing_games": missing,
            "by_season": by_season,
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

    def _logs(self, player_id: int, season: str, publication_snapshot: Any = None) -> pd.DataFrame | None:
        return self.games.get_player_logs_by_id(player_id, season, publication_snapshot=publication_snapshot)

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


def _pair_window(
    team: str, seasons: Sequence[str], mates: Mapping[str, _Logs], window: Sequence[_WindowSeason]
) -> dict[str, Any]:
    """Where the pair's samples start and what they leave out.

    ``from`` is the first game on either side; ``excluded_games`` counts the
    teammate's games for ``team`` in the seasons read that are on neither
    side, because the two were not teammates then.
    """

    counted = {game_id for w in window for game_id in (*w.without, *w.played)}
    days = [
        day
        for w in window
        for game_id, day in zip(w.rows["GAME_ID"].astype(str), w.rows["GAME_DATE"], strict=True)
        if game_id in counted
    ]
    team_games = {
        game_id
        for s in seasons
        if (rows := mates[s].team_rows(team)) is not None
        for game_id in rows["GAME_ID"].astype(str)
    }
    return {
        "from": _as_date(min(days)).isoformat() if days else None,
        "excluded_games": len(team_games - counted),
    }


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
                "by_season": [
                    {
                        "season": entry["season"],
                        "starts_without": entry["without"]["starts"],
                        "games_without": entry["without"]["games"],
                        "starts_with": entry["with"]["starts"],
                        "games_with": entry["with"]["games"],
                    }
                    for entry in split["by_season"]
                ],
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
        #: The ``source`` of every response summed, and their ``games_missing``.
        self.sources: set[str] = set()
        self.games_missing: dict[str, Mapping[str, Any]] = {}
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
        self.sources.add(response["source"])
        for game in response.get("games_missing", ()):
            self.games_missing[game["game_id"]] = game
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


class _Evidence:
    """A teammate's full-game and on/off samples over some of his window's seasons."""

    def __init__(self, teammate: _Teammate, window: Sequence[_WindowSeason]) -> None:
        self.window = list(window)
        self.with_games, self.with_spans = teammate._games(self.window, True)
        self.without_games, self.without_spans = teammate._games(self.window, False)
        self.on_floor = teammate._floor(self.window, "on")
        self.off_floor = teammate._floor(self.window, "off")

    def values(self, column: str | None, with_player: bool) -> list[float]:
        games = self.with_games if with_player else self.without_games
        return games[column].tolist() if column in games else []

    def on_off_limits(self) -> tuple[list[str], bool]:
        """Limits the on/off evidence carries and whether it is complete.

        Complete means both states cover exactly the seasons whose totals can
        be sliced to the window, so the two per-36 rates are comparable.
        """

        needed = [w.season for w in self.window if w.sliceable]
        on, off = self.on_floor, self.off_floor
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
        # Any unsliceable season blocks the flag: the full-game evidence would
        # span games the on/off totals do not.
        complete = bool(needed) and len(needed) == len(self.window) and on.covered == off.covered == needed
        return limits, complete


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
        slate_season: str,
        in_rotation: bool = True,
        pair_window: dict[str, Any] | None = None,
        rotation: dict[str, Any] | None = None,
    ) -> None:
        self.teammate_id = teammate_id
        #: The pair window's first game and his team games it leaves out.
        self.pair_window = pair_window
        #: Whether he is in the 15+ MPG teammate set that gets scored rows.
        self.in_rotation = in_rotation
        self.rotation = rotation
        self.name = name
        self.window = window
        self.reads = reads
        self.lines = lines
        self.slate_season = slate_season

    # -- full-game evidence -------------------------------------------------

    @staticmethod
    def _games(window: Sequence[_WindowSeason], with_player: bool) -> tuple[pd.DataFrame, list[str]]:
        frames, spans = [], []
        for w in window:
            ids = w.rows["GAME_ID"].astype(str)
            chosen = w.rows[ids.isin(w.played if with_player else w.without)]
            if not chosen.empty:
                frames.append(chosen)
                spans.append(w.season)
        if not frames:
            return pd.DataFrame(columns=["GAME_ID", "MIN"]), []
        return pd.concat(frames), spans

    # -- on/off evidence ----------------------------------------------------

    def _floor(self, window: Sequence[_WindowSeason], side: str) -> _Floor:
        floor = _Floor()
        for w in window:
            if w.sliceable:
                floor.add(w.season, self.reads.get(w.season, side), self.teammate_id)
        return floor

    # -- the season a flag is computed on (crf04/statsplus#485) ----------------

    def flag_season(self) -> str:
        """The Slate season when it has ``MIN_GAMES_WITHOUT`` games on each side, otherwise last season."""

        current = next((w for w in self.window if w.season == self.slate_season), None)
        if current is not None and min(len(current.without), len(current.played)) >= MIN_GAMES_WITHOUT:
            return self.slate_season
        return next((w.season for w in self.window if w.season != self.slate_season), self.slate_season)

    def _by_season(self) -> list[_Evidence]:
        """One evidence per window season, the Slate season first."""

        ordered = sorted(self.window, key=lambda w: w.season != self.slate_season)
        return [_Evidence(self, [w]) for w in ordered]

    # -- rendering ------------------------------------------------------------

    def rows(self, branch: str, stale_report: bool) -> list[dict[str, Any]]:
        pooled = _Evidence(self, self.window)
        seasons = self._by_season()
        flag_season = self.flag_season()
        rendered = [
            self._scored(market, branch, pooled, seasons, flag_season, stale_report)
            for market in SCORED_MARKETS
        ]
        rendered += [self._context(market, branch, pooled, seasons) for market in CONTEXT_MARKETS]
        for rendered_row in rendered:
            rendered_row["rotation"] = self.rotation
            rendered_row["pair_window"] = self.pair_window
        return rendered

    @staticmethod
    def _baseline(column, keys, evidence: _Evidence) -> dict[str, Any]:
        return {
            "avg": _round(_raw_mean(evidence.values(column, True))),
            "games": len(evidence.with_games),
            "per36": _round(evidence.on_floor.per36(keys)) if keys else None,
            "minutes": evidence.on_floor.minutes,
        }

    def _scored(
        self,
        market: str,
        branch: str,
        pooled: _Evidence,
        seasons: Sequence[_Evidence],
        flag_season: str,
        stale_report: bool,
    ) -> dict[str, Any]:
        column, keys = SCORED_MARKETS[market]
        on_floor, off_floor = pooled.on_floor, pooled.off_floor
        row: dict[str, Any] = {
            "player": self.name,
            "player_id": self.teammate_id,
            "market": market,
            "kind": "scored",
            "baseline": {
                **self._baseline(column, keys, pooled),
                "season_span": _season_span(pooled.with_spans),
                "on_floor_season_span": on_floor.span,
            },
            "flagged": False,
            "limits": [],
            **_on_off_provenance(on_floor),
        }
        if branch == "plays":
            row["seasons"] = [
                {"season": e.window[0].season, "baseline": self._baseline(column, keys, e)} for e in seasons
            ]
            return row
        row.update(_on_off_provenance(on_floor, off_floor))
        values = pooled.values(column, False)
        row["without"] = {
            "avg": _round(_raw_mean(values)),
            "games": len(pooled.without_games),
            "season_span": _season_span(pooled.without_spans),
        }
        row["off_floor"] = {
            "per36": _round(off_floor.per36(keys)),
            "minutes": off_floor.minutes,
            "season_span": off_floor.span,
        }
        posted = posted_line(self.lines, market)
        if posted is None:
            row["line"] = None
        else:
            value, source = posted
            row["line"] = {
                "value": value,
                "source": source,
                "beat": sum(1 for stat in values if stat > value),
                "of": len(values),
            }
        row["seasons"] = [self._season_scored(column, keys, e, posted) for e in seasons]
        row["flag_season"] = flag_season
        limits, shift, flagged = self._judge(column, keys, pooled, baseline_floor=False)
        row["limits"] = [limit for limit in limits if limit != "no_baseline"]
        if posted is None:
            row["limits"].append("no_line")
        if stale_report:
            row["limits"].append("stale_report")
        if "no_baseline" in limits:
            row["limits"].append("no_baseline")
        if shift is not None:
            row["shift"] = {
                **shift,
                "season_span": {"full_game": row["without"]["season_span"], "on_off": off_floor.span},
            }
        row["flagged"] = flagged
        return row

    def _season_scored(self, column, keys, evidence: _Evidence, posted) -> dict[str, Any]:
        """One season's samples, and whether they alone would flag the market."""

        values = evidence.values(column, False)
        off_floor = evidence.off_floor
        limits, shift, flagged = self._judge(column, keys, evidence, baseline_floor=True)
        return {
            "season": evidence.window[0].season,
            "baseline": self._baseline(column, keys, evidence),
            "without": {"avg": _round(_raw_mean(values)), "games": len(evidence.without_games)},
            "off_floor": {"per36": _round(off_floor.per36(keys)), "minutes": off_floor.minutes},
            "line": None
            if posted is None
            else {"beat": sum(1 for stat in values if stat > posted[0]), "of": len(values)},
            "shift": shift,
            "flagged": flagged,
            "limits": limits,
        }

    @staticmethod
    def _judge(
        column, keys, evidence: _Evidence, *, baseline_floor: bool
    ) -> tuple[list[str], dict[str, float | None] | None, bool]:
        """The limits, shift and flag of one sample.

        The per-season rule (crf04/statsplus#485) also needs a baseline of
        ``MIN_GAMES_WITH`` games and ``MIN_ON_FLOOR_MINUTES``; the pooled row
        keeps the rule it had before, for one release.
        """

        on_floor, off_floor = evidence.on_floor, evidence.off_floor
        limits: list[str] = []
        on_off_limits, complete = evidence.on_off_limits()
        insufficient = len(evidence.without_games) < MIN_GAMES_WITHOUT or (
            bool(off_floor.covered) and off_floor.minutes < MIN_OFF_FLOOR_MINUTES
        )
        thin_baseline = baseline_floor and (
            len(evidence.with_games) < MIN_GAMES_WITH
            or (bool(on_floor.covered) and on_floor.minutes < MIN_ON_FLOOR_MINUTES)
        )
        if insufficient:
            limits.append("insufficient")
        if thin_baseline:
            limits.append("insufficient_baseline")
        limits.extend(on_off_limits)
        if insufficient or thin_baseline or not complete:
            return limits, None, False
        full_shift = _shift(
            _raw_mean(evidence.values(column, False)), _raw_mean(evidence.values(column, True))
        )
        on_off_shift = _shift(off_floor.per36(keys), on_floor.per36(keys))
        if full_shift is None or on_off_shift is None:
            limits.append("no_baseline")
        shift = {"full_game": _round(full_shift, 4), "on_off_per36": _round(on_off_shift, 4)}
        return limits, shift, _agree(full_shift, on_off_shift)

    def _context(
        self, market: str, branch: str, pooled: _Evidence, seasons: Sequence[_Evidence]
    ) -> dict[str, Any]:
        on_floor = pooled.on_floor
        row: dict[str, Any] = {
            "player": self.name,
            "player_id": self.teammate_id,
            "market": market,
            "kind": "context",
            "baseline": self._context_baseline(market, pooled, spans=True),
            "flagged": False,
            "limits": [],
            **_on_off_provenance(on_floor),
        }
        if branch == "plays":
            row["seasons"] = [
                {"season": e.window[0].season, "baseline": self._context_baseline(market, e)} for e in seasons
            ]
            return row
        without = self._context_without(market, pooled)
        if without is not None:
            row["without"] = {**without, "season_span": _season_span(pooled.without_spans)}
        off = self._context_off_floor(market, pooled)
        row["off_floor"] = off
        if off is not None:
            off["season_span"] = pooled.off_floor.span
            row["limits"], _ = pooled.on_off_limits()
            row.update(_on_off_provenance(on_floor, pooled.off_floor))
        row["seasons"] = []
        for e in seasons:
            entry: dict[str, Any] = {
                "season": e.window[0].season,
                "baseline": self._context_baseline(market, e),
            }
            if without is not None:
                entry["without"] = self._context_without(market, e)
            entry["off_floor"] = self._context_off_floor(market, e)
            entry["limits"] = e.on_off_limits()[0] if off is not None else []
            row["seasons"].append(entry)
        return row

    def _context_baseline(self, market: str, evidence: _Evidence, *, spans: bool = False) -> dict[str, Any]:
        on_floor = evidence.on_floor
        if market == "USG":
            baseline = {"usage": on_floor.usage, "minutes": on_floor.minutes}
            if spans:
                baseline["season_span"] = on_floor.span
            return baseline
        column = {"MIN": "MIN", "FGA": "FGA"}[market]
        keys = ("fga",) if market == "FGA" else ()
        baseline = self._baseline(column, keys, evidence)
        if spans:
            baseline.update(
                {"season_span": _season_span(evidence.with_spans), "on_floor_season_span": on_floor.span}
            )
        return baseline

    @staticmethod
    def _context_without(market: str, evidence: _Evidence) -> dict[str, Any] | None:
        column = {"MIN": "MIN", "FGA": "FGA"}.get(market)
        if column is None:
            return None
        values = evidence.values(column, False)
        return {"avg": _round(_raw_mean(values)), "games": len(evidence.without_games)}

    @staticmethod
    def _context_off_floor(market: str, evidence: _Evidence) -> dict[str, Any] | None:
        off_floor = evidence.off_floor
        if market == "USG":
            return {"usage": off_floor.usage, "minutes": off_floor.minutes}
        if market == "FGA":
            return {"per36": _round(off_floor.per36(("fga",))), "minutes": off_floor.minutes}
        return None


__all__ = ["SCENARIO_STATUSES", "InjuryScenarioService"]
