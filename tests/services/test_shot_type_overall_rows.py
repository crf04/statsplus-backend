"""The derived shot-type ``FGM`` and ``FGA`` Defense Sheet rows (crf04/statsplus#454).

Fixture: team ``i`` (1..30, in catalog order) allows, per 48 in every slice,
``FG2M = i``, ``FG3M = (7i) % 31``, ``FG2A = (11i) % 31 + 20`` and
``FG3A = (5i) % 31 + 10``.  Overall makes average 31.0 (sigma 12.777585) and
attempts 61.0 (sigma 10.757943), thirty distinct values each.  BOS (i=2)
allows 2 + 14 = 16 makes, 4th of 30, and 42 + 20 = 62 attempts, 16th; LAL
(i=11) allows 26 makes, 11th, and 82 attempts, the most.  No single stat
ranks a team where its sum does, so a row that copied one half fails.
"""

from datetime import date

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import NBA_PUBLICATION_TAXONOMY, SHOT_TYPE_SLICES
from app.services.team_filter_rankings import TeamFilterRankingService
from app.services.team_matchup_query import TeamMatchupQueryService
from tests.services import test_matchup_scores as scores
from tests.support.publication_stubs import (
    RETRIEVED_AT,
    SEASON,
    StubGovernance,
    StubReader,
    league,
    read,
)

TEAM_INDEX = {tricode: index for index, tricode in enumerate(NBA_TEAM_ID_TO_TRICODE.values(), start=1)}
STREAM = "grouped_shot_types_opponent_season"
BOS = 1610612738
LAL = 1610612747
SLICE_KEYS = ("Catch and Shoot", "Pullups", "Less Than 10 ft")


def _per48(tricode, scale=1.0):
    i = TEAM_INDEX[tricode]
    per48 = {key: 1.0 for key in NBA_PUBLICATION_TAXONOMY["shot_types"]}
    for slice_key in SHOT_TYPE_SLICES:
        per48[f"{slice_key}_FG2M"] = scale * i
        per48[f"{slice_key}_FG3M"] = scale * ((7 * i) % 31)
        per48[f"{slice_key}_FG2A"] = scale * ((11 * i) % 31 + 20)
        per48[f"{slice_key}_FG3A"] = scale * ((5 * i) % 31 + 10)
    return per48


def _matchup_payload():
    """Serve a Season and a Last-15 window, the latter at twice the Season rates."""

    windows = [
        TeamMatchupQueryService._publication_base_window(
            SEASON,
            cutoff=date(2026, 1, 10),
            window_games=window_games,
            base="shot_types",
            rows=league(lambda tricode, scale=scale: _per48(tricode, scale)),
            retrieved_at=RETRIEVED_AT,
        )
        for window_games, scale in ((None, 1.0), (15, 2.0))
    ]
    service = scores._service(
        markets=("FGM", "FGA"),
        facts=(scores._fact("shot_types", "Catch and Shoot", 1.0, 50),),
        season_metrics=(),
    )
    service.team_matchups = scores._Windows(*windows)
    return service.get_matchup(game_id=scores.GAME_ID)


def _sheet_rows(sheet_owner):
    return {row["key"]: row for row in sheet_owner["defense_sheet"]["shot_types"]}


def test_the_served_matchup_ranks_each_shot_types_overall_makes_row():
    payload = _matchup_payload()
    teams = {team["team_id"]: team for team in payload["teams"]}
    boston, lakers = _sheet_rows(teams[BOS]), _sheet_rows(teams[LAL])
    league_rows = _sheet_rows(payload["league"])

    for slice_key in SLICE_KEYS:
        key = f"{slice_key}:FGM"
        assert boston[key]["label"] == f"{slice_key} FGM"
        assert boston[key]["markets"] == ["FGM"]
        assert boston[key]["season"] == {
            "allowed_per_48": 16.0,
            "percent_vs_league_average": -48.387097,
            "sigma_deviation": -1.173931,
            "rank": 4,
        }
        assert boston[key]["last_15"] == {
            "allowed_per_48": 32.0,
            "percent_vs_league_average": -48.387097,
            "sigma_deviation": -1.173931,
            "rank": 4,
        }
        assert lakers[key]["season"]["allowed_per_48"] == 26.0
        assert lakers[key]["season"]["rank"] == 11
        assert lakers[key]["season"]["percent_vs_league_average"] == -16.129032
        assert league_rows[key]["season"] == {"average_allowed_per_48": 31.0, "sigma": 12.777585}
        assert league_rows[key]["last_15"] == {"average_allowed_per_48": 62.0, "sigma": 25.555169}


def test_the_served_matchup_ranks_each_shot_types_overall_attempts_row():
    payload = _matchup_payload()
    teams = {team["team_id"]: team for team in payload["teams"]}
    boston, lakers = _sheet_rows(teams[BOS]), _sheet_rows(teams[LAL])
    league_rows = _sheet_rows(payload["league"])

    for slice_key in SLICE_KEYS:
        key = f"{slice_key}:FGA"
        assert boston[key]["markets"] == ["FGA"]
        assert boston[key]["season"] == {
            "allowed_per_48": 62.0,
            "percent_vs_league_average": 1.639344,
            "sigma_deviation": 0.092955,
            "rank": 16,
        }
        assert boston[key]["last_15"]["allowed_per_48"] == 124.0
        assert boston[key]["last_15"]["rank"] == 16
        assert lakers[key]["season"] == {
            "allowed_per_48": 82.0,
            "percent_vs_league_average": 34.42623,
            "sigma_deviation": 1.952046,
            "rank": 30,
        }
        assert league_rows[key]["season"] == {"average_allowed_per_48": 61.0, "sigma": 10.757943}


def test_the_half_rows_keep_their_markets_beside_the_overall_rows():
    rows = _sheet_rows({team["team_id"]: team for team in _matchup_payload()["teams"]}[BOS])

    assert rows["Pullups:FG2A"]["markets"] == ["FGA", "FG2A"]
    assert rows["Pullups:FG3M"]["markets"] == ["3PM", "PTS", "FGM", "PA", "PR", "PRA"]
    assert rows["Pullups:PTS"]["markets"] == ["PTS", "PA", "PR", "PRA"]


def test_a_shot_types_attempts_team_filter_ranks_opponents_by_the_overall_attempts_row():
    service = TeamFilterRankingService(
        StubReader({STREAM: read(STREAM, league(_per48))}), governance_resolver=StubGovernance()
    )

    ranked = service.ranked_teams("sheet:shot_types:Catch and Shoot:FGA", SEASON)

    # Most allowed first: LAL's 82 attempts lead, ATL's 46 sit 28th of 30.
    assert ranked[:5] == ["LAL", "CHI", "CHA", "WAS", "OKC"]
    assert ranked.index("BOS") == 30 - 16
    assert ranked.index("ATL") == 30 - 3
    assert service.ranked_teams("sheet:shot_types:Catch and Shoot:FGM", SEASON)[:3] == ["CHA", "UTA", "SAC"]


def test_overall_shot_types_rows_in_the_window_change_no_score():
    """Each derived row is read-only context, never a score input (Acceptance 4).

    The same window is scored with and without ``FGM`` and ``FGA`` rows carrying
    absurd values, for every market a shot type feeds, so a loop that picked
    them up by market or stat key would move a score.
    """

    rows = {
        "catch_and_shoot": (5.0, 5.0, 6.0, 4.0, 10.0, 8.0, 12.0, 6.0),
        "pullups": (4.0, 4.0, 4.0, 6.0, 9.0, 9.0, 7.0, 11.0),
        "less_than_10_ft": (10.0, 0.0, 12.0, 0.0, 20.0, 1.0, 22.0, 1.0),
    }
    without = []
    for slice_key, (lg2m, lg3m, op2m, op3m, lg2a, lg3a, op2a, op3a) in rows.items():
        without += [
            ("shot_types", slice_key, "FG2M", lg2m, op2m),
            ("shot_types", slice_key, "FG3M", lg3m, op3m),
            ("shot_types", slice_key, "FG2A", lg2a, op2a),
            ("shot_types", slice_key, "FG3A", lg3a, op3a),
        ]
    with_overall = [
        *without,
        *(
            ("shot_types", slice_key, stat_key, 1.0, 9999.0)
            for slice_key in rows
            for stat_key in ("FGM", "FGA")
        ),
    ]
    markets = ("PTS", "FGM", "FGA", "FG2M", "FG2A", "3PM", "FG3A")

    scores_without = _scores(markets, without)

    assert _scores(markets, with_overall) == scores_without
    # His attempt mix: C&S 0.75 * 12/10 + 0.25 * 6/8, Pullups 0.2 * 7/9 +
    # 0.8 * 11/9, Less Than 10 ft all twos at 22/20; weighed 0.5/0.3/0.2.
    assert scores_without["FGA"]["season"]["components"] == {
        "shot_types": {
            "value": 0.10375,
            "thin": False,
            "slices": [
                {"key": "Catch and Shoot", "percent_vs_league_average": 8.75},
                {"key": "Pullups", "percent_vs_league_average": 13.333333},
                {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0},
            ],
        }
    }
    # His make mix: C&S 0.6 * 6/5 + 0.4 * 4/5, Pullups 0.2 * 4/4 + 0.8 * 6/4,
    # Less Than 10 ft no league threes so 12/10.
    assert scores_without["FGM"]["season"]["components"] == {
        "shot_types": {
            "value": 0.18,
            "thin": False,
            "slices": [
                {"key": "Catch and Shoot", "percent_vs_league_average": 4.0},
                {"key": "Pullups", "percent_vs_league_average": 40.0},
                {"key": "Less Than 10 ft", "percent_vs_league_average": 20.0},
            ],
        }
    }


def _scores(markets, metrics):
    player = scores._service(
        markets=markets,
        facts=scores._mix_facts(),
        season_metrics=tuple(metrics),
    ).get_matchup(game_id=scores.GAME_ID)["players"][0]
    return scores._computed_scores(player, keep_slices=True)
