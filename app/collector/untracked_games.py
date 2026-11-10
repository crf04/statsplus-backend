"""Completed games NBA.com tracking will never cover, declared after review.

Tracking trails the box score for hours after a game, and the shot-type
streams refuse to publish until it catches up.  A game tracking never
reaches would hold them back forever, and no aggregate can tell "never" from
"not yet", so such a game is declared here, with the evidence, in a reviewed
change.  Only the shot-type streams read this list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

_EVIDENCE = (
    "stats.nba.com leaguedashplayerptshot has no rows for either team on that date "
    "while leaguedashplayerstats does (checked 2026-11-10)"
)


@dataclass(frozen=True)
class UntrackedGame:
    game_id: str
    game_date: date
    home_team_id: int
    away_team_id: int
    evidence: str

    @property
    def team_ids(self) -> frozenset[int]:
        return frozenset({self.home_team_id, self.away_team_id})


UNTRACKED_GAMES: Mapping[str, tuple[UntrackedGame, ...]] = {
    "2026-27": (
        # LAC @ OKC
        UntrackedGame("0022600116", date(2026, 10, 25), 1610612760, 1610612746, _EVIDENCE),
        # DEN @ IND
        UntrackedGame("0022600189", date(2026, 11, 7), 1610612754, 1610612743, _EVIDENCE),
    ),
}


def untracked_games(season: str) -> tuple[UntrackedGame, ...]:
    return UNTRACKED_GAMES.get(season, ())


def untracked_game_count(season: str, team_id: int, *, date_from: str | None, date_to: str | None) -> int:
    """Count ``team_id``'s declared games inside an NBA ``MM/DD/YYYY`` window."""

    first = datetime.strptime(date_from, "%m/%d/%Y").date() if date_from else date.min
    last = datetime.strptime(date_to, "%m/%d/%Y").date() if date_to else date.max
    return sum(
        team_id in game.team_ids and first <= game.game_date <= last for game in untracked_games(season)
    )
