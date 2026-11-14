"""Shot-type tracking coverage over the governed games, net of declared untracked games.

Tracking proves it covers a window only through games played.  A shortfall is
lag, and blocks, unless it is exactly explained by declared untracked games
(``app.collector.untracked_games``) that are wholly absent from tracking.  A
declared game is absent when every participant is short for it, and present
when NBA.com restored it.  Anything in between blocks.  Inputs are plain data
the composer gathers from observations and the governed ledger.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.collector.untracked_games import UNTRACKED_GAMES, UntrackedGame, untracked_games


@dataclass(frozen=True)
class DeclaredGameBox:
    """One declared game's governed ledger facts."""

    game: UntrackedGame
    #: Ledger ``team_minutes``: game minutes, 48 in regulation and 53 after one overtime.
    team_minutes: Mapping[int, float]
    #: Players with minutes in the game.
    players: frozenset[int]


@dataclass(frozen=True, order=True)
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


@dataclass(frozen=True)
class PlayerTracking:
    """One player shot-type observation's box-score and tracking games."""

    team_box_gp: Mapping[int, int]
    box_gp: Mapping[int, int]
    box_fga: Mapping[int, int]
    overall_gp: Mapping[int, int]
    #: Each category record's player, box-score games and tracking games.
    category_gp: tuple[tuple[int, int | None, int | None], ...]


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
    readings: Sequence[tuple[Mapping[int, int], Mapping[int, int]]],
    participants: Mapping[str, frozenset[int]],
) -> frozenset[str]:
    """Declared games every participant is short for in every reading.

    A reading pairs box-score games with one tracking source's games, so all
    of a composition's readings share one absent set.
    """

    return frozenset(
        game_id
        for game_id, members in participants.items()
        if members
        and readings
        and all(
            box.get(member, 0) - tracking.get(member, 0) >= 1
            for box, tracking in readings
            for member in members
        )
    )


def _unexplained(
    box_gp: Mapping[int, int], tracking_gp: Mapping[int, int], appearances: Mapping[int, int]
) -> Iterable[Shortfall]:
    for key, games in box_gp.items():
        if games - tracking_gp.get(key, 0) != appearances.get(key, 0):
            yield Shortfall(key, games - appearances.get(key, 0), tracking_gp.get(key, 0))


def _require_explained(shortfalls: Iterable[Shortfall], absent: frozenset[str]) -> frozenset[str]:
    unexplained = tuple(sorted(set(shortfalls)))
    if unexplained:
        raise ShotTypeCoverageBlocked(
            "provider_window_lagging", unexplained=unexplained, declared_absent=absent
        )
    return absent


def cover_teams(
    governed: Mapping[int, frozenset[str]],
    *,
    tracking_gp: Sequence[Mapping[int, int]],
    minutes: Mapping[int, float],
    declared: Iterable[DeclaredGameBox],
) -> dict[int, CoveredTeam]:
    """Each team's tracked games and the box minutes those games played.

    ``tracking_gp`` holds each shot type's tracking games by team.  Every
    shot type must be short by exactly the absent declared games.
    ``minutes`` is the box score's window minutes, in game minutes like the
    ledger's ``team_minutes``, so subtracting an absent game's leaves totals
    and minutes on the same games.
    """

    boxes = {box.game.game_id: box for box in declared}
    box_gp = {team_id: len(game_ids) for team_id, game_ids in governed.items()}
    absent = _absent_games(
        [(box_gp, reading) for reading in tracking_gp],
        {
            game_id: frozenset(team_id for team_id in box.game.team_ids if game_id in governed[team_id])
            for game_id, box in boxes.items()
        },
    )
    appearances = {team_id: len(game_ids & absent) for team_id, game_ids in governed.items()}
    _require_explained(
        (shortfall for reading in tracking_gp for shortfall in _unexplained(box_gp, reading, appearances)),
        absent,
    )
    return {
        team_id: CoveredTeam(
            game_ids=game_ids - absent,
            minutes=minutes[team_id]
            - sum(boxes[game_id].team_minutes[team_id] for game_id in game_ids & absent),
        )
        for team_id, game_ids in governed.items()
    }


def verify_player_coverage(
    governed: Mapping[int, frozenset[str]],
    observations: Sequence[PlayerTracking],
    *,
    declared: Iterable[DeclaredGameBox],
) -> frozenset[str]:
    """Return the declared games absent from player tracking, or block.

    Every observation's box score must cover every governed game.  A declared
    game is absent only when every shooter who appeared in it is short in
    every observation's Overall, so all categories share one absent set.
    Every shooter's Overall games, and every category record's games beside
    its own box games, must then equal the box score's games less the absent
    games the player appeared in.  A shooter who played only in absent games
    has no tracking record at all.
    """

    if any(
        observation.team_box_gp.get(team_id) != len(game_ids)
        for observation in observations
        for team_id, game_ids in governed.items()
    ):
        raise ShotTypeCoverageBlocked("provider_window_lagging", unexplained=(), declared_absent=frozenset())
    readings = [
        (
            {
                player_id: observation.box_gp.get(player_id, 0)
                for player_id, attempts in observation.box_fga.items()
                if attempts > 0
            },
            observation.overall_gp,
        )
        for observation in observations
    ]
    shooters = frozenset().union(*(shooter_gp.keys() for shooter_gp, _ in readings))
    participants = {box.game.game_id: box.players & shooters for box in declared}
    absent = _absent_games(readings, participants)
    appearances = Counter(player_id for game_id in absent for player_id in participants[game_id])
    return _require_explained(
        [
            *(
                shortfall
                for shooter_gp, overall in readings
                for shortfall in _unexplained(shooter_gp, overall, appearances)
            ),
            *(
                Shortfall(player_id, (games_box or 0) - appearances[player_id], games or 0)
                for observation in observations
                for player_id, games_box, games in observation.category_gp
                if games_box is None or games != games_box - appearances[player_id]
            ),
        ],
        absent,
    )
