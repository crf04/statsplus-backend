"""A named ``without`` player's team window (crf04/statsplus#449, #450).

His window for team T opens at T's first game of the season when his first
game of the season is for T (he has played for no other team since last
season), and otherwise at his first game for T: a team change opens it there.
A player with no game in the season has a window for T only when roster
evidence places him on T, and it then covers the whole season.  The window
closes at his first later game for another team, open-ended if he never
moves.  A team game inside it that he has no line in is a game he sat; a game
outside it is neither with nor without him.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TeamWindow:
    #: His first game for the team; ``None`` when the window opens at the team's first game.
    opens: date | None
    #: His first game for another team after ``opens``, excluded; ``None`` while he stays.
    closes: date | None

    def contains(self, day: date) -> bool:
        return (self.opens is None or self.opens <= day) and (self.closes is None or day < self.closes)


def team_window(
    appearances: Iterable[tuple[date, str]], team: str, *, rostered: bool = False
) -> TeamWindow | None:
    """His window for ``team`` from his season's ``(game date, team)`` lines.

    ``rostered`` is the roster evidence that places a player with no line on
    ``team``.  ``None`` when he never played for ``team`` that season and is
    not rostered there.
    """

    games = sorted(appearances)
    if not games:
        return TeamWindow(None, None) if rostered else None
    first_for_team = next((day for day, played_for in games if played_for == team), None)
    if first_for_team is None:
        return None
    closes = next((day for day, played_for in games if day > first_for_team and played_for != team), None)
    return TeamWindow(None if games[0][1] == team else first_for_team, closes)
