"""Membership evidence for named ``without`` players' team windows (crf04/statsplus#450).

A team game before a player's first game for the team counts as without him
only on the evidence ``app.domain.team_window`` ranks: the season's stored
official injury reports, then his team at the end of the previous season.  The
evidence is read only for players who have such games, once per request
whatever their number: the reports that may list them in one statement, and
their previous-season game logs in one batch.  It is optional: when either read
fails, every window opens at his first game for the team, as without it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Hashable, Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from app.domain.team_window import Membership, TeamWindow, team_window
from app.services.research_season import previous_nba_season

logger = logging.getLogger(__name__)


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
        judged: Mapping[Hashable, tuple[Sequence[tuple[date, str]], Mapping[str, Collection[date]]]],
        player_id: Callable[[Any], int] = int,
    ) -> dict[Hashable, dict[str, TeamWindow | None]]:
        """Each player's window for each team, as ``{key: {team: window}}``.

        ``judged[key]`` is his season's ``(game date, team)`` lines and, per
        team, the days of its games the windows will judge.  Keys are player
        ids, or keys ``player_id`` resolves to one when his evidence is read.
        A window that would hold none of those days before his first game for
        the team, and has no game of his, is ``None``.
        """

        needed = {
            key: player_id(key)
            for key, (lines, games) in judged.items()
            if any(_before_first(lines, team, days) for team, days in games.items())
        }
        evidence = self._evidence(season, needed) if needed else {}
        windows: dict[Hashable, dict[str, TeamWindow | None]] = {}
        for key, (lines, games) in judged.items():
            windows[key] = {}
            for team, days in games.items():
                window = team_window(lines, team, evidence.get(key, Membership()))
                if window is not None and window.opens is None and not any(map(window.contains, days)):
                    window = None
                windows[key][team] = window
        return windows

    def _evidence(self, season: str, ids: Mapping[Hashable, int]) -> dict[Hashable, Membership]:
        wanted = sorted(set(ids.values()))
        try:
            listed = self._listed(season, wanted)
            previous = self.logs(wanted, previous_nba_season(season))
        except (SQLAlchemyError, ValueError) as error:
            logger.warning("team membership evidence unreadable season=%s: %s", season, error)
            return {}
        return {
            key: Membership(listed.get(resolved, frozenset()), _last_team(previous.get(resolved)))
            for key, resolved in ids.items()
        }

    def _listed(self, season: str, player_ids: Collection[int]) -> dict[int, frozenset[tuple[date, str]]]:
        """Each player's ``(game day, team)`` listings on the season's stored reports."""

        if self.injury_reports is None:
            return {}
        wanted = set(player_ids)
        found: dict[int, set[tuple[date, str]]] = {}
        for snapshot in self.injury_reports.get_season_mentions(season, wanted).values():
            for entry in snapshot.normalized_entries:
                player = entry.get("canonical_player_id")
                if player is None or int(player) not in wanted or not entry.get("game_date"):
                    continue
                found.setdefault(int(player), set()).add(
                    (date.fromisoformat(entry["game_date"]), entry["tricode"])
                )
        return {player: frozenset(days) for player, days in found.items()}


def _before_first(lines: Sequence[tuple[date, str]], team: str, days: Collection[date]) -> bool:
    """Whether any of ``days`` comes before his first game for ``team`` (or he has none)."""

    first = min((day for day, played_for in lines if played_for == team), default=None)
    return any(first is None or day < first for day in days)


def _last_team(frame: pd.DataFrame | None) -> str | None:
    """The team of his last game in a season's logs."""

    return max(appearances(frame), default=(None, None))[1]


__all__ = ["TeamMembership", "appearances"]
