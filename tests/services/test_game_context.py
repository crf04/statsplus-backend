"""Stored team context before each game (crf04/statsplus#466)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.models.canonical_game_ledger import CanonicalGameLedgerTeamFact
from app.services.game_context import GameContextRepository
from tests.support.game_context_world import final, migrated_engine, postpone, schedule

SEASON = "2026-27"
NOP = (1610612740, "NOP")
LAC = (1610612746, "LAC")
LAL = (1610612747, "LAL")
BOS = (1610612738, "BOS")
FIRST_RUN = datetime(2026, 10, 25, 14, 15, tzinfo=UTC)
LATER_RUN = datetime(2026, 10, 25, 16, 15, tzinfo=UTC)
UPCOMING = "0022600005"


def _world(tmp_path):
    return seed_week(migrated_engine(tmp_path))


def seed_week(engine):
    """A preseason game, three finals, a postponed game and NOP's upcoming back-to-back."""

    schedule(
        engine, "0012600001", "2026-10-10T23:30:00+00:00", LAC, NOP, season=SEASON, classification="Preseason"
    )
    schedule(engine, "0022600001", "2026-10-21T23:30:00+00:00", LAC, NOP, season=SEASON)
    # 22:30 Eastern on 2026-10-23: the UTC date is the next day.
    schedule(engine, "0022600002", "2026-10-24T02:30:00+00:00", NOP, LAL, season=SEASON)
    schedule(engine, "0022600003", "2026-10-24T23:30:00+00:00", LAL, NOP, season=SEASON)
    schedule(engine, "0022600004", "2026-10-24T23:30:00+00:00", BOS, LAC, season=SEASON, postponed=True)
    schedule(engine, UPCOMING, "2026-10-25T23:30:00+00:00", LAC, NOP, season=SEASON)
    final(engine, "0022600001", "2026-10-21", (*LAC, 100, 98.0), (*NOP, 110, 100.0), season=SEASON)
    final(
        engine,
        "0022600002",
        "2026-10-23",
        (*NOP, 105, 102.0),
        (*LAL, 120, 101.0),
        season=SEASON,
        minutes=53.0,
    )
    # A LiveData game without NOP's possessions: it counts as a result, not in the ratings.
    final(engine, "0022600003", "2026-10-24", (*LAL, 101, 97.0), (*NOP, 99, None), season=SEASON)
    return engine


EMPTY_RATINGS = {
    "games": 0,
    "possession_games": 0,
    "pace": None,
    "offensive_rating": None,
    "defensive_rating": None,
    "net_rating": None,
}

UPCOMING_CONTEXT = {
    "status": "available",
    "computed_at": "2026-10-25T14:15:00+00:00",
    "teams": [
        {
            "team_id": 1610612746,
            "tricode": "LAC",
            "is_home": False,
            "season": "2026-27",
            "through_game_date": "2026-10-21",
            "rest": {"previous_game_date": "2026-10-21", "rest_days": 3, "back_to_back": False},
            "ratings": {
                "season": {
                    "games": 1,
                    "possession_games": 1,
                    "pace": 99.0,
                    "offensive_rating": 102.0,
                    "defensive_rating": 110.0,
                    "net_rating": -8.0,
                },
                "last_10": {
                    "games": 1,
                    "possession_games": 1,
                    "pace": 99.0,
                    "offensive_rating": 102.0,
                    "defensive_rating": 110.0,
                    "net_rating": -8.0,
                },
            },
            "last_5": [
                {
                    "game_date": "2026-10-21",
                    "opponent_tricode": "NOP",
                    "is_home": False,
                    "points": 100,
                    "opponent_points": 110,
                    "result": "L",
                    "margin": -10,
                }
            ],
        },
        {
            "team_id": 1610612740,
            "tricode": "NOP",
            "is_home": True,
            "season": "2026-27",
            "through_game_date": "2026-10-24",
            "rest": {"previous_game_date": "2026-10-24", "rest_days": 0, "back_to_back": True},
            "ratings": {
                "season": {
                    "games": 3,
                    "possession_games": 2,
                    "pace": 95.3,
                    "offensive_rating": 106.4,
                    "defensive_rating": 110.6,
                    "net_rating": -4.1,
                },
                "last_10": {
                    "games": 3,
                    "possession_games": 2,
                    "pace": 95.3,
                    "offensive_rating": 106.4,
                    "defensive_rating": 110.6,
                    "net_rating": -4.1,
                },
            },
            "last_5": [
                {
                    "game_date": "2026-10-24",
                    "opponent_tricode": "LAL",
                    "is_home": True,
                    "points": 99,
                    "opponent_points": 101,
                    "result": "L",
                    "margin": -2,
                },
                {
                    "game_date": "2026-10-23",
                    "opponent_tricode": "LAL",
                    "is_home": False,
                    "points": 105,
                    "opponent_points": 120,
                    "result": "L",
                    "margin": -15,
                },
                {
                    "game_date": "2026-10-21",
                    "opponent_tricode": "LAC",
                    "is_home": True,
                    "points": 110,
                    "opponent_points": 100,
                    "result": "W",
                    "margin": 10,
                },
            ],
        },
    ],
}


def test_an_upcoming_game_reads_each_teams_rest_ratings_and_results_from_earlier_games(tmp_path):
    engine = _world(tmp_path)
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)

    summary = repository.refresh(SEASON)

    assert summary == {
        "season": "2026-27",
        "games": 4,
        "ledger_games": 3,
        "inserted": 4,
        "updated": 0,
        "removed": 0,
        "unchanged": 0,
    }
    assert repository.read(UPCOMING) == UPCOMING_CONTEXT


def test_a_played_game_never_counts_itself_and_a_first_game_has_no_rest_or_ratings(tmp_path):
    engine = _world(tmp_path)
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)
    repository.refresh(SEASON)

    opener = repository.read("0022600001")["teams"]
    assert [
        (team["tricode"], team["through_game_date"], team["rest"], team["last_5"]) for team in opener
    ] == [
        ("LAC", None, {"previous_game_date": None, "rest_days": None, "back_to_back": False}, []),
        ("NOP", None, {"previous_game_date": None, "rest_days": None, "back_to_back": False}, []),
    ]
    assert [team["ratings"] for team in opener] == [{"season": EMPTY_RATINGS, "last_10": EMPTY_RATINGS}] * 2
    # NOP's 2026-10-24 game counts the 10-21 and 10-23 games, not its own result.
    nop = repository.read("0022600003")["teams"][1]
    assert (nop["through_game_date"], [game["game_date"] for game in nop["last_5"]]) == (
        "2026-10-23",
        ["2026-10-23", "2026-10-21"],
    )
    assert nop["rest"] == {"previous_game_date": "2026-10-23", "rest_days": 0, "back_to_back": True}


def test_last_10_and_last_5_keep_the_newest_games(tmp_path):
    engine = migrated_engine(tmp_path)
    for day in range(1, 13):
        game_id = f"00226001{day:02d}"
        schedule(engine, game_id, f"2026-11-{day:02d}T23:30:00+00:00", LAL, NOP, season=SEASON)
        # NOP scores 100 + day on 100 possessions; LAL scores 100 on 100.
        final(
            engine, game_id, f"2026-11-{day:02d}", (*LAL, 100, 100.0), (*NOP, 100 + day, 100.0), season=SEASON
        )
    schedule(engine, "0022600200", "2026-11-14T23:30:00+00:00", LAL, NOP, season=SEASON)
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)
    repository.refresh(SEASON)

    nop = repository.read("0022600200")["teams"][1]

    assert nop["ratings"]["season"] == {
        "games": 12,
        "possession_games": 12,
        "pace": 100.0,
        "offensive_rating": 106.5,
        "defensive_rating": 100.0,
        "net_rating": 6.5,
    }
    # Games of 2026-11-03 through 2026-11-12: 103 through 112 points.
    assert nop["ratings"]["last_10"] == {
        "games": 10,
        "possession_games": 10,
        "pace": 100.0,
        "offensive_rating": 107.5,
        "defensive_rating": 100.0,
        "net_rating": 7.5,
    }
    assert [(game["game_date"], game["margin"]) for game in nop["last_5"]] == [
        ("2026-11-12", 12),
        ("2026-11-11", 11),
        ("2026-11-10", 10),
        ("2026-11-09", 9),
        ("2026-11-08", 8),
    ]
    assert nop["rest"] == {"previous_game_date": "2026-11-12", "rest_days": 1, "back_to_back": False}


def test_a_second_run_changes_nothing_and_keeps_computed_at(tmp_path):
    engine = _world(tmp_path)
    GameContextRepository(engine, clock=lambda: FIRST_RUN).refresh(SEASON)
    repository = GameContextRepository(engine, clock=lambda: LATER_RUN)

    summary = repository.refresh(SEASON)

    assert (summary["inserted"], summary["updated"], summary["removed"], summary["unchanged"]) == (0, 0, 0, 4)
    assert repository.read(UPCOMING) == UPCOMING_CONTEXT


def test_a_new_result_rewrites_only_the_games_it_moves(tmp_path):
    engine = _world(tmp_path)
    GameContextRepository(engine, clock=lambda: FIRST_RUN).refresh(SEASON)
    # A correction: the 10-24 game now carries NOP's possessions.
    with engine.begin() as connection:
        connection.execute(
            update(CanonicalGameLedgerTeamFact.__table__)
            .where(CanonicalGameLedgerTeamFact.game_id == "0022600003")
            .where(CanonicalGameLedgerTeamFact.team_id == NOP[0])
            .values(offensive_possessions=99.0)
        )
    repository = GameContextRepository(engine, clock=lambda: LATER_RUN)

    summary = repository.refresh(SEASON)

    assert (summary["inserted"], summary["updated"], summary["removed"], summary["unchanged"]) == (0, 1, 0, 3)
    context = repository.read(UPCOMING)
    assert context["computed_at"] == "2026-10-25T16:15:00+00:00"
    # 314 points on 301 possessions; 321 allowed on 296; 48 x 298.5 / 149 minutes.
    assert context["teams"][1]["ratings"]["season"] == {
        "games": 3,
        "possession_games": 3,
        "pace": 96.2,
        "offensive_rating": 104.3,
        "defensive_rating": 108.4,
        "net_rating": -4.1,
    }
    assert repository.read("0022600001")["computed_at"] == "2026-10-25T14:15:00+00:00"


def test_a_postponed_game_loses_its_stored_context(tmp_path):
    engine = _world(tmp_path)
    GameContextRepository(engine, clock=lambda: FIRST_RUN).refresh(SEASON)
    postpone(engine, UPCOMING)
    repository = GameContextRepository(engine, clock=lambda: LATER_RUN)

    summary = repository.refresh(SEASON)

    assert (summary["games"], summary["removed"]) == (3, 1)
    assert repository.read(UPCOMING) == {"status": "unavailable", "computed_at": None, "teams": []}


@pytest.mark.parametrize("game_id", ["0012600001", "0022600004", "0022699999"])
def test_preseason_postponed_and_unknown_games_have_no_context(tmp_path, game_id):
    engine = _world(tmp_path)
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)
    repository.refresh(SEASON)

    assert repository.read(game_id) == {"status": "unavailable", "computed_at": None, "teams": []}


def test_rest_reads_the_schedule_not_the_ledger_and_skips_all_star_games(tmp_path):
    engine = migrated_engine(tmp_path)
    schedule(engine, "0022600001", "2026-10-21T23:30:00+00:00", LAC, NOP, season=SEASON)
    # Played but not yet in the ledger: it still counts for rest.
    schedule(engine, "0022600002", "2026-10-23T23:30:00+00:00", BOS, LAC, season=SEASON)
    schedule(
        engine, "0032600001", "2026-10-24T23:30:00+00:00", LAC, BOS, season=SEASON, classification="All-Star"
    )
    # Empty stored evidence is no postponement.
    schedule(
        engine, UPCOMING, "2026-10-25T23:30:00+00:00", LAC, NOP, season=SEASON, postponement_evidence="{}"
    )
    # LAC's only ledger game has no possessions on its side.
    final(engine, "0022600001", "2026-10-21", (*LAC, 100, None), (*NOP, 110, 100.0), season=SEASON)
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)

    assert repository.refresh(SEASON)["games"] == 3

    lac = repository.read(UPCOMING)["teams"][0]
    assert (lac["tricode"], lac["through_game_date"], lac["rest"]) == (
        "LAC",
        "2026-10-21",
        {"previous_game_date": "2026-10-23", "rest_days": 1, "back_to_back": False},
    )
    assert lac["ratings"]["season"] == {
        "games": 1,
        "possession_games": 0,
        "pace": None,
        "offensive_rating": None,
        "defensive_rating": None,
        "net_rating": None,
    }


def test_a_season_refresh_reads_and_keeps_only_its_own_season(tmp_path):
    engine = _world(tmp_path)
    schedule(engine, "0022501200", "2026-04-10T23:30:00+00:00", LAL, NOP, season="2025-26")
    schedule(engine, "0022501230", "2026-04-12T23:30:00+00:00", NOP, LAL, season="2025-26")
    final(engine, "0022501200", "2026-04-10", (*LAL, 90, 100.0), (*NOP, 130, 100.0), season="2025-26")
    GameContextRepository(engine, clock=lambda: FIRST_RUN).refresh("2025-26")
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)

    summary = repository.refresh(SEASON)

    assert (summary["games"], summary["ledger_games"], summary["removed"]) == (4, 3, 0)
    assert repository.read(UPCOMING) == UPCOMING_CONTEXT
    last_season = repository.read("0022501230")["teams"][0]
    assert (last_season["tricode"], last_season["season"], last_season["last_5"][0]["margin"]) == (
        "NOP",
        "2025-26",
        40,
    )
