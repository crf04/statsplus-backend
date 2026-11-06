"""A named ``without`` player's team window (crf04/statsplus#449).

His window for team T runs from his first game for T in the season until his
first later game for another team, open-ended if he never moves.  A team game
inside it that he has no line in is a game he sat; a game outside it is
neither with nor without him.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TeamWindow:
    opens: date
    #: His first game for another team after ``opens``, excluded; ``None`` while he stays.
    closes: date | None

    def contains(self, day: date) -> bool:
        return self.opens <= day and (self.closes is None or day < self.closes)


def team_window(appearances: Iterable[tuple[date, str]], team: str) -> TeamWindow | None:
    """His window for ``team`` from his season's ``(game date, team)`` lines.

    ``None`` when he never played for ``team`` that season.
    """

    games = sorted(appearances)
    opens = next((day for day, played_for in games if played_for == team), None)
    if opens is None:
        return None
    closes = next((day for day, played_for in games if day > opens and played_for != team), None)
    return TeamWindow(opens, closes)
