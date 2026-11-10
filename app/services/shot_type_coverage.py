"""Shot-type tracking coverage over the governed games, net of declared untracked games.

Tracking proves it covers a window only through games played.  A shortfall is
lag, and blocks, unless it is exactly explained by declared untracked games
(``app.collector.untracked_games``) that are wholly absent from tracking.  A
declared game is absent when every participant is short for it, and present
when NBA.com restored it.  Anything in between blocks.  Inputs are plain data
the composer gathers from observations and the governed ledger.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from app.collector.untracked_games import UNTRACKED_GAMES, UntrackedGame, untracked_games

K = TypeVar("K", bound=Hashable)


@dataclass(frozen=True)
class DeclaredGameBox:
    """One declared game's governed ledger facts."""

    game: UntrackedGame
    #: Ledger ``team_minutes``: the team's player minutes, overtime included.
    team_minutes: Mapping[int, float]
    #: Players with minutes in the game.
    players: frozenset[int]


@dataclass(frozen=True)
class Shortfall:
    participant: int
    expected_gp: int
    observed_gp: int


class ShotTypeCoverageBlocked(ValueError):
    def __init__(self, reason: str, *, unexplained: tuple[Shortfall, ...], declared_absent: frozenset[str]):
        super().__init__(reason)
        self.reason = reason
        self.unexplained = unexplained
        self.declared_absent = declared_absent


@dataclass(frozen=True)
class CoveredTeam:
    game_ids: frozenset[str]
    minutes: float


def untracked_exclusions_hold(
    governed: Mapping[int, frozenset[str]], published: Mapping[int, frozenset[str]]
) -> bool:
    """Whether published game sets are the governed ones less wholly absent declared games.

    A published row cannot say which games tracking had, so every reader
    accepts exactly this shape: each excluded game is declared, and it is
    excluded from every team whose window holds it.
    """

    declared = {game.game_id for games in UNTRACKED_GAMES.values() for game in games}
    absent = set().union(*(governed[team_id] - game_ids for team_id, game_ids in published.items()))
    return absent <= declared and all(
        game_ids <= governed[team_id] and not game_ids & absent for team_id, game_ids in published.items()
    )


def declared_in_window(season: str, governed: Mapping[int, frozenset[str]]) -> tuple[UntrackedGame, ...]:
    """Declared games this composition's governed window contains."""

    window = set().union(*governed.values())
    return tuple(game for game in untracked_games(season) if game.game_id in window)


def _absent_games(
    box_gp: Mapping[K, int],
    tracking_gp: Mapping[K, int],
    participants: Mapping[str, frozenset[K]],
) -> frozenset[str]:
    """Decide each declared game absent or present, and require every shortfall be explained."""

    short = {key: games - tracking_gp.get(key, 0) for key, games in box_gp.items()}
    absent = frozenset(
        game_id
        for game_id, members in participants.items()
        if members and all(short.get(member, 0) >= 1 for member in members)
    )
    unexplained = tuple(
        Shortfall(key, box_gp[key] - missing, tracking_gp.get(key, 0))
        for key in sorted(box_gp)
        if short[key] != (missing := sum(key in participants[game_id] for game_id in absent))
    )
    if unexplained:
        raise ShotTypeCoverageBlocked(
            "provider_window_lagging", unexplained=unexplained, declared_absent=absent
        )
    return absent


def cover_teams(
    governed: Mapping[int, frozenset[str]],
    *,
    tracking_gp: Mapping[int, int],
    minutes: Mapping[int, float],
    declared: Iterable[DeclaredGameBox],
) -> dict[int, CoveredTeam]:
    """Each team's tracked games and the box minutes those games played.

    ``minutes`` is the box score's window minutes, in game minutes (about 48
    per game).  An absent game's ledger minutes are a team's five players'
    minutes, so a fifth of them leaves totals and minutes on the same games.
    """

    boxes = {box.game.game_id: box for box in declared}
    absent = _absent_games(
        {team_id: len(game_ids) for team_id, game_ids in governed.items()},
        tracking_gp,
        {
            game_id: frozenset(team_id for team_id in box.game.team_ids if game_id in governed[team_id])
            for game_id, box in boxes.items()
        },
    )
    return {
        team_id: CoveredTeam(
            game_ids=game_ids - absent,
            minutes=minutes[team_id]
            - sum(boxes[game_id].team_minutes[team_id] / 5 for game_id in game_ids & absent),
        )
        for team_id, game_ids in governed.items()
    }


def verify_player_coverage(
    governed: Mapping[int, frozenset[str]],
    *,
    team_box_gp: Mapping[int, int],
    box_gp: Mapping[int, int],
    box_fga: Mapping[int, int],
    overall_gp: Mapping[int, int],
    category_gp: Iterable[tuple[int, int | None, int | None]],
    declared: Iterable[DeclaredGameBox],
) -> frozenset[str]:
    """Return the declared games absent from player tracking, or block.

    The box score must cover every governed game.  Every shooter's tracking
    Overall games, and every category record's games (beside its own box
    games), must equal the box score's games less the absent declared games
    the player appeared in.  A shooter who played only in absent games has no
    tracking record at all.
    """

    if any(team_box_gp.get(team_id) != len(game_ids) for team_id, game_ids in governed.items()):
        raise ShotTypeCoverageBlocked("provider_window_lagging", unexplained=(), declared_absent=frozenset())
    shooters = {
        player_id: box_gp.get(player_id, 0) for player_id, attempts in box_fga.items() if attempts > 0
    }
    participants = {box.game.game_id: box.players & shooters.keys() for box in declared}
    absent = _absent_games(shooters, overall_gp, participants)
    appearances = {
        player_id: sum(player_id in participants[game_id] for game_id in absent) for player_id in shooters
    }
    unexplained = tuple(
        Shortfall(player_id, (games_box or 0) - appearances.get(player_id, 0), games or 0)
        for player_id, games_box, games in category_gp
        if games_box is None or games != games_box - appearances.get(player_id, 0)
    )
    if unexplained:
        raise ShotTypeCoverageBlocked(
            "provider_window_lagging", unexplained=unexplained, declared_absent=absent
        )
    return absent
