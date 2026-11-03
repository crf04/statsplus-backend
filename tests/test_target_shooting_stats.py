"""Targets on FTA, FTM, FGM and FG2M (crf04/statsplus#370).

A Target is saved through ``UserService`` and backtested over a real composed
``player_game_logs`` Publication, so the Average Qualifier, the season
averages and each game's value all come from stored box scores.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from tests import test_target_backtest as bt
from tests.services import test_backtest_seasons as seasons

LAST = seasons.LAST
#: LeBron's (FGM, FG3M, FTM, FTA) per game, oldest first; the first two are
#: against OKC. FG2M is 7, 2, 8, 3 and 7.
BOX_BY_GAME = [(10, 3, 4, 6), (6, 4, 1, 2), (9, 1, 6, 7), (8, 5, 2, 2), (7, 0, 5, 8)]


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'shooting.sqlite3'}"))
    rows = seasons._season_logs(LAST, okc_games=2)
    lebron = [row for row in rows if row["player_id"] == bt.LEBRON]
    for row, (fgm, fg3m, ftm, fta) in zip(lebron, BOX_BY_GAME, strict=True):
        row.update(
            field_goals_made=fgm,
            three_pointers_made=fg3m,
            free_throws_made=ftm,
            free_throws_attempted=fta,
        )
    built.compose("player_game_logs", LAST, {"rows": rows})
    for stream, *_rest in seasons.DIET_STREAMS.values():
        built.publish_diet(stream, LAST, corner_three=0.42)
    yield built
    built.engine.dispose()


@pytest.mark.parametrize(
    ("stat", "season_average", "okc_games_newest_first"),
    [
        ("FTA", 5.0, [2.0, 6.0]),
        ("FTM", 3.6, [1.0, 4.0]),
        ("FGM", 8.0, [6.0, 10.0]),
        ("FG2M", 5.4, [2.0, 7.0]),
    ],
)
def test_a_target_saves_and_backtests_on_each_shooting_stat(
    world, stat, season_average, okc_games_newest_first
):
    target = world.users.create_target(
        seasons.OWNER,
        opponent="OKC",
        qualifiers=[
            seasons.CORNER_THREE,
            {
                "base": "season_average",
                "stat": stat,
                "comparator": "at_or_above",
                "threshold": season_average,
            },
        ],
        note=None,
        stat_preferences={"columns": [stat], "graded_by": stat},
    )

    body, _ = world.backtests().backtest(seasons.OWNER, target["id"])

    assert target["qualifiers"][1]["stat"] == stat
    assert target["stat_preferences"] == {"columns": [stat], "graded_by": stat}
    assert body["stat_columns"] == ["PTS", "PTS/36", "3PA", "3PA/36", stat]
    (player,) = body["players"]
    assert player["canonical_id"] == bt.LEBRON
    assert player["season_averages"][stat] == season_average
    assert [game["stats"][stat] for game in player["games"]] == okc_games_newest_first


@pytest.mark.parametrize(("stat", "season_average"), [("FG2M", 5.4), ("FTA", 5.0)])
def test_an_average_qualifier_just_above_the_season_average_leaves_him_out(world, stat, season_average):
    target = world.users.create_target(
        seasons.OWNER,
        opponent="OKC",
        qualifiers=[
            seasons.CORNER_THREE,
            {
                "base": "season_average",
                "stat": stat,
                "comparator": "at_or_above",
                "threshold": season_average + 0.1,
            },
        ],
        note=None,
    )

    body, _ = world.backtests().backtest(seasons.OWNER, target["id"])

    assert body["players"] == []
