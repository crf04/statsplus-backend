"""A named ``without`` player's team window (crf04/statsplus#449, #450).

His window for team T runs from his first game for T in the season until his
first later game for another team, open-ended if he never moves.  A team game
before his first game for T (any team game, when he has none) is in it only on
membership evidence, report evidence first:

1. the stored official injury report for the game lists him for T: in;
2. a stored report on that day or any earlier day this season lists him for
   another team: out, and carry-over ends there;
3. he ended the previous season on T and has played for no other team before
   the game: in;
4. otherwise: out.

A team game inside the window that he has no line in is a game he sat; a game
outside it is neither with nor without him.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Membership:
    """Evidence that places him on a team before his first game for it."""

    #: ``(game day, team)`` for each stored official report this season that lists him, any status.
    listed: frozenset[tuple[date, str]] = frozenset()
    #: His team in his last game of the previous season.
    previous_team: str | None = None


@dataclass(frozen=True)
class TeamWindow:
    #: His first game for the team; ``None`` when he has none.
    opens: date | None
    #: His first game for another team after ``opens``, excluded; ``None`` while he stays.
    closes: date | None
    #: Days before ``opens`` whose stored report lists him for the team.
    listed: frozenset[date] = frozenset()
    #: Before ``opens``, the days before this one count: he ended last season on
    #: the team, until his first game or report for another team (``date.max`` while none).
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
    listed = frozenset(day for day, listed_for in membership.listed if listed_for == team)
    carried_until = None
    if membership.previous_team == team:
        elsewhere = [day for day, played_for in games if played_for != team]
        elsewhere += [day for day, listed_for in membership.listed if listed_for != team]
        carried_until = min(elsewhere, default=date.max)
    if opens is None and not listed and carried_until is None:
        return None
    return TeamWindow(opens, closes, listed, carried_until)
