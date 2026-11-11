"""A named ``without`` player's team window (crf04/statsplus#449, #450).

His window for team T runs from his first game for T in the season until his
first later game for another team, open-ended if he never moves.  A team game
before his first game for T (any team game, when he has none) is in it only
when membership evidence places him on T for that game: the stored official
injury report for the game lists him for T, or he ended the previous season on
T and has played for no other team before that game.  A team game inside the
window that he has no line in is a game he sat; a game outside it is neither
with nor without him.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Membership:
    """Evidence that places him on a team before his first game for it."""

    #: Team game days whose stored official injury report lists him for the team, any status.
    listed: frozenset[date] = frozenset()
    #: His last stored game before the season was for the team.
    carried_over: bool = False


@dataclass(frozen=True)
class TeamWindow:
    #: His first game for the team; ``None`` when he has none.
    opens: date | None
    #: His first game for another team after ``opens``, excluded; ``None`` while he stays.
    closes: date | None
    #: Days before ``opens`` whose stored report lists him for the team.
    listed: frozenset[date] = frozenset()
    #: Before ``opens``, the days before this one count: he ended last season on
    #: the team, until his first game for another team (``date.max`` while none).
    carried_until: date | None = None

    def contains(self, day: date) -> bool:
        if self.opens is not None and self.opens <= day:
            return self.closes is None or day < self.closes
        return day in self.listed or (self.carried_until is not None and day < self.carried_until)


def team_window(
    appearances: Iterable[tuple[date, str]], team: str, membership: Membership
) -> TeamWindow | None:
    """His window for ``team`` from his season's ``(game date, team)`` lines.

    ``None`` when he never played for ``team`` that season and no evidence
    places him on it.
    """

    games = sorted(appearances)
    opens = next((day for day, played_for in games if played_for == team), None)
    closes = None
    if opens is not None:
        closes = next((day for day, played_for in games if day > opens and played_for != team), None)
    carried_until = None
    if membership.carried_over:
        carried_until = next((day for day, played_for in games if played_for != team), date.max)
    if opens is None and not membership.listed and carried_until is None:
        return None
    return TeamWindow(opens, closes, membership.listed, carried_until)
