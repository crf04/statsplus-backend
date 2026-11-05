"""Completed games NBA player tracking leaves out for good.

Tracking can trail the box score by a game-day or two and then catch up, so
every gate that compares the two refuses a shortfall as a lag.  A game listed
here is one tracking dropped after it had counted it and never restored: the
gates expect tracking to be short by exactly these games, and still refuse any
other shortfall as a lag (crf04/statsplus-backend#712).

An entry is a reviewed claim.  Add one only once the omission has outlasted
any lag, and remove it if NBA restores the game: tracking that counts a
listed game no longer matches what the gates expect, and is refused too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class TrackingOmittedGame:
    game_id: str
    season: str
    #: The game's date as NBA Stats' DateFrom/DateTo filters read it.
    game_date: date
    team_ids: frozenset[int]


TRACKING_OMITTED_GAMES: tuple[TrackingOmittedGame, ...] = (
    # LAC @ OKC: dropped from tracking around 2026-11-02 13:00-16:40 UTC.
    TrackingOmittedGame("0022600116", "2026-27", date(2026, 10, 25), frozenset({1610612746, 1610612760})),
)
