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

from collections.abc import Iterable, Mapping
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


def tracked_game_ids_by_team(game_ids_by_team: Mapping[int, Iterable[str]]) -> dict[int, frozenset[str]]:
    """Each team's governed games that player tracking covers."""

    omitted = {game.game_id for game in TRACKING_OMITTED_GAMES}
    return {team_id: frozenset(game_ids) - omitted for team_id, game_ids in game_ids_by_team.items()}


def omitted_game_ids(game_ids: Iterable[str]) -> frozenset[str]:
    """The games among ``game_ids`` that player tracking leaves out."""

    return frozenset(game_ids) & {game.game_id for game in TRACKING_OMITTED_GAMES}


def omitted_game_dates(season: str, team_id: int, *, before: date) -> list[str]:
    """NBA Stats dates (MM/DD/YYYY) of one team's omitted games played before ``before``."""

    return [
        played.strftime("%m/%d/%Y")
        for played in sorted(
            game.game_date
            for game in TRACKING_OMITTED_GAMES
            if game.season == season and team_id in game.team_ids and game.game_date < before
        )
    ]
