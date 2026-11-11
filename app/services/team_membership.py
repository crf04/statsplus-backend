"""Membership evidence for named ``without`` players' team windows (crf04/statsplus#450).

A team game before a player's first game for the team counts as without him
only when the stored official injury report for that game lists him for the
team, or he ended the previous season on the team and has played for no other
team before that game (``app.domain.team_window``).  The evidence is read only
for players who have such games: those games' stored reports in one statement,
and the players' previous-season game logs in one batch.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd

from app.domain.team_window import Membership, TeamWindow, team_window
from app.services.injury_snapshot_repository import InjurySnapshotScope
from app.services.research_season import previous_nba_season


def appearances(frame: pd.DataFrame | None) -> list[tuple[date, str]]:
    """A game-log frame's ``(game date, team)`` lines."""

    if frame is None or frame.empty:
        return []
    return list(zip(pd.to_datetime(frame["GAME_DATE"]).dt.date, frame["TEAM_ABBREVIATION"], strict=True))


class TeamMembership:
    def __init__(
        self,
        injury_reports: Any | None,
        logs: Callable[[Sequence[int], str], Mapping[int, pd.DataFrame | None]],
    ) -> None:
        #: The ``injury_snapshots`` repository; ``None`` reads no reports.
        self.injury_reports = injury_reports
        #: Several players' game logs for one season: ``logs(player_ids, season)``.
        self.logs = logs

    def windows(
        self,
        season: str,
        team: str,
        games: Mapping[str, date],
        lines: Mapping[Hashable, Sequence[tuple[date, str]]],
        player_id: Callable[[Any], int] = int,
    ) -> dict[Hashable, TeamWindow | None]:
        """Each player's window for ``team`` from his season's ``(game date, team)`` lines.

        ``games`` are the team's games (id to date) the windows will judge.
        ``lines`` are keyed by player id, or by a key ``player_id`` resolves to
        one when his evidence is read.
        """

        before: dict[Hashable, set[str]] = {}
        for key, rows in lines.items():
            first = min((day for day, played_for in rows if played_for == team), default=None)
            ids = {game_id for game_id, day in games.items() if first is None or day < first}
            if ids:
                before[key] = ids
        evidence: dict[Hashable, Membership] = {}
        if before:
            pending = set().union(*before.values())
            listed = self._listed(season, team, {game_id: games[game_id] for game_id in pending})
            ids = {key: player_id(key) for key in before}
            previous = self.logs(sorted(set(ids.values())), previous_nba_season(season))
            evidence = {
                key: Membership(listed.get(resolved, frozenset()), _last_team(previous.get(resolved)) == team)
                for key, resolved in ids.items()
            }
        return {key: team_window(rows, team, evidence.get(key, Membership())) for key, rows in lines.items()}

    def _listed(self, season: str, team: str, games: Mapping[str, date]) -> dict[int, frozenset[date]]:
        """Each player's days among ``games`` whose stored report lists him for ``team``."""

        if self.injury_reports is None:
            return {}
        stored = self.injury_reports.get_many(
            tuple(InjurySnapshotScope(season, game_id) for game_id in games)
        )
        days: dict[int, set[date]] = {}
        for scope, snapshot in stored.items():
            for entry in snapshot.normalized_entries:
                player_id = entry.get("canonical_player_id")
                if player_id is not None and entry.get("tricode") == team:
                    days.setdefault(int(player_id), set()).add(games[scope.game_id])
        return {player_id: frozenset(found) for player_id, found in days.items()}


def _last_team(frame: pd.DataFrame | None) -> str | None:
    """The team of his last game in a season's logs."""

    return max(appearances(frame), default=(None, None))[1]


__all__ = ["TeamMembership", "appearances"]
