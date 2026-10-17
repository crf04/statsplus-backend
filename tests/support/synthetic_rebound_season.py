"""A small synthetic season whose REB matchup answer is known (#234, #447).

Thirty teams play every day on a circle-method schedule.  Each team fields four
guards and four bigs at 28 minutes and two scrubs at 8, so a game is 240 player
minutes (48 team minutes).  Only the *mix* of the rebounds an opponent allows
varies; its total is the same for every team:

    guard   3PT-miss rebounds 4 + t, 2PT-miss rebounds 1
    big     2PT-miss rebounds 6 - t
    scrub   2PT-miss rebounds 1

where ``t = team_id % 5 - 2`` is the *opponent's* tendency.  The opponent's
total allowed is 4*(5+t) + 4*(6-t) + 2 = 46 for every t, so the flat
``OPP_REB`` score is constant, while the 3PT slice allowed is 16 + 4t and the
2PT slice 30 - 4t.  A guard-heavy diet therefore sees a different score for
each opponent and the rebounds he actually collects follow it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.services.canonical_game_ledger import (
    CanonicalGame,
    PlayerGameFact,
    TeamGameFact,
)

SEASON = "2025-26"
FIRST_DAY = date(2025, 10, 22)
TEAMS = tuple(range(1, 31))
GUARDS, BIGS, SCRUBS = 4, 4, 2
_RETRIEVED = datetime(2026, 6, 30, tzinfo=UTC)


def tendency(team_id: int) -> int:
    return team_id % 5 - 2


def _pairings(day_index: int) -> list[tuple[int, int]]:
    """Circle-method round ``day_index``: every team plays exactly once."""

    ring = list(TEAMS[1:])
    shift = day_index % len(ring)
    ring = ring[-shift:] + ring[:-shift] if shift else ring
    order = [TEAMS[0], *ring]
    half = len(order) // 2
    pairs = list(zip(order[:half], reversed(order[half:]), strict=False))
    return [pair if (day_index + i) % 2 else pair[::-1] for i, pair in enumerate(pairs)]


def _player(
    team_id,
    kind,
    slot,
    opponent,
    *,
    outlier,
    big_offensive,
    flip,
    scrub_minutes,
    scrub_rebounds,
    varying_total,
):
    t = flip * tendency(opponent)
    off_two = big_offensive if kind == "big" else 0
    minutes = scrub_minutes[slot] if kind == "scrub" else 28.0
    if kind == "guard":
        two, three = (3 + t if varying_total else 1), 4 + t
    elif kind == "big":
        two, three = 6 - t, 0
    else:
        two, three = scrub_rebounds, 0
    two += outlier
    return PlayerGameFact(
        player_id=team_id * 100 + {"guard": 0, "big": 10, "scrub": 20}[kind] + slot,
        player_name=f"{kind}{team_id}-{slot}",
        team_id=team_id,
        team_tricode=f"T{team_id:02d}",
        minutes=minutes,
        points=0,
        field_goals_made=0,
        field_goals_attempted=0,
        two_pointers_made=0,
        two_pointers_attempted=0,
        three_pointers_made=0,
        three_pointers_attempted=0,
        free_throws_made=0,
        free_throws_attempted=0,
        offensive_rebounds=off_two,
        defensive_rebounds=two + three,
        rebounds=two + three + off_two,
        assists=0,
        turnovers=0,
        steals=0,
        blocks=0,
        personal_fouls=0,
        def_two_pt_rebounds=two,
        def_three_pt_rebounds=three,
        ft_def_rebounds=0,
        off_two_pt_rebounds=off_two,
        off_three_pt_rebounds=0,
        ft_off_rebounds=0,
    )


def _team(team_id, opponent, players, *, home):
    mine = [p for p in players if p.team_id == team_id]
    total = sum(p.rebounds for p in mine)
    return TeamGameFact(
        offensive_rebounds=sum(p.offensive_rebounds for p in mine),
        team_id=team_id,
        team_tricode=f"T{team_id:02d}",
        opponent_team_id=opponent,
        opponent_team_tricode=f"T{opponent:02d}",
        is_home=home,
        defensive_rebounds=sum(p.defensive_rebounds for p in mine),
        rebounds=total,
        team_minutes=48.0,
        def_two_pt_rebounds=sum(p.def_two_pt_rebounds for p in mine),
        def_three_pt_rebounds=sum(p.def_three_pt_rebounds for p in mine),
        ft_def_rebounds=0,
        off_two_pt_rebounds=sum(p.off_two_pt_rebounds for p in mine),
        off_three_pt_rebounds=0,
        ft_off_rebounds=0,
    )


def synthetic_season(
    days: int = 40,
    *,
    outlier_from: date | None = None,
    outlier_rebounds: int = 50,
    big_offensive: int = 0,
    shift_from: date | None = None,
    scrub_minutes: tuple[float, float] = (8.0, 8.0),
    scrub_rebounds: int = 1,
    varying_total: bool = False,
) -> tuple[CanonicalGame, ...]:
    """``days`` rounds of 15 games.  Games dated ``outlier_from`` or later give
    every player ``outlier_rebounds`` extra two-point-miss rebounds.  Each big
    also collects ``big_offensive`` offensive rebounds off two-point misses,
    the same against every opponent (the flat total just rises).

    ``shift_from`` flips every opponent's tendency from that date on, so a
    Season window and a Last 15 window disagree.  ``scrub_minutes`` sets the
    two scrubs' minutes (they must still add to 16) and ``scrub_rebounds`` their
    two-point-miss rebounds (0 leaves them with no rebound diet).
    ``varying_total`` gives guards 3 + t two-point-miss rebounds, so an
    opponent's total allowed is no longer flat."""

    games = []
    for day_index in range(days):
        day = FIRST_DAY + timedelta(days=day_index)
        outlier = outlier_rebounds if outlier_from and day >= outlier_from else 0
        for number, (home, away) in enumerate(_pairings(day_index)):
            players = []
            for team_id, opponent in ((home, away), (away, home)):
                for kind, count in (("guard", GUARDS), ("big", BIGS), ("scrub", SCRUBS)):
                    for slot in range(count):
                        players.append(
                            _player(
                                team_id,
                                kind,
                                slot,
                                opponent,
                                outlier=outlier,
                                big_offensive=big_offensive,
                                flip=-1 if shift_from and day >= shift_from else 1,
                                scrub_minutes=scrub_minutes,
                                scrub_rebounds=scrub_rebounds,
                                varying_total=varying_total,
                            )
                        )
            games.append(
                CanonicalGame(
                    game_id=f"g{day_index:03d}-{number:02d}",
                    season=SEASON,
                    game_date=day,
                    home_team_id=home,
                    home_team_tricode=f"T{home:02d}",
                    away_team_id=away,
                    away_team_tricode=f"T{away:02d}",
                    team_facts=(
                        _team(home, away, players, home=True),
                        _team(away, home, players, home=False),
                    ),
                    player_facts=tuple(players),
                    source_observation_id=f"obs-{day_index}-{number}",
                    retrieved_at=_RETRIEVED,
                    participant_ids_by_team=(),
                )
            )
    return tuple(games)
