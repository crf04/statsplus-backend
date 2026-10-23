"""The derived shot-type ``PTS`` Defense Sheet row (crf04/statsplus#315).

Fixture: team ``i`` (1..30, in catalog order) allows ``FG2M = i`` and
``FG3M = (7 * i) % 31`` per 48 in every slice, so ``PTS = 2*FG2M + 3*FG3M``
is ``2i + 3*((7i) % 31)``: 23 for ATL (i=1), 132 for CHA (i=30).  The thirty
values are distinct, average 77.5 and have population sigma 32.4733...
ATL ranks 3rd on points but 1st on FG2M and 7th on FG3M, so a row that
copied either stat would fail every assertion below.
"""

from datetime import date

import pytest

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    DEFENSE_SHEET_ROW_METRICS,
    NBA_PUBLICATION_TAXONOMY,
    SHOT_TYPE_SLICES,
    defense_sheet_filter,
)
from app.services.matchup import MatchupService, slice_markets
from app.services.team_filter_rankings import TeamFilterRankingService, team_filter_ranking
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import StoredTeamMatchupFact, TeamMatchupSnapshotScope
from tests.support.publication_stubs import (
    RETRIEVED_AT,
    SEASON,
    StubGovernance,
    StubReader,
    league,
    read,
)

TEAM_INDEX = {tricode: index for index, tricode in enumerate(NBA_TEAM_ID_TO_TRICODE.values(), start=1)}
POINTS = {tricode: 2 * i + 3 * ((7 * i) % 31) for tricode, i in TEAM_INDEX.items()}
STREAM = "grouped_shot_types_opponent_season"


def _per48(tricode):
    i = TEAM_INDEX[tricode]
    per48 = {key: 1.0 for key in NBA_PUBLICATION_TAXONOMY["shot_types"]}
    for slice_key in SHOT_TYPE_SLICES:
        per48[f"{slice_key}_FG2M"] = float(i)
        per48[f"{slice_key}_FG3M"] = float((7 * i) % 31)
    return per48


def _window():
    return TeamMatchupQueryService._publication_base_window(
        SEASON,
        cutoff=date(2026, 1, 10),
        window_games=None,
        base="shot_types",
        rows=league(_per48),
        retrieved_at=RETRIEVED_AT,
    )


def _points(window, tricode, slice_key="catch_and_shoot"):
    team_id = next(t for t, code in NBA_TEAM_ID_TO_TRICODE.items() if code == tricode)
    (metric,) = (m for m in window.team_metrics[team_id] if (m.slice_key, m.stat_key) == (slice_key, "PTS"))
    return metric


def test_the_points_row_is_hand_computed_for_the_team_and_the_league():
    window = _window()

    atl = _points(window, "ATL")
    assert atl.allowed_per_48 == 23.0
    assert atl.rank == 3
    assert atl.percent_vs_league_average == pytest.approx(-70.3225806451613)
    assert atl.sigma_deviation == pytest.approx(-1.67830070958666)

    cha = _points(window, "CHA", "pullups")
    assert (cha.allowed_per_48, cha.rank) == (132.0, 28)
    assert cha.percent_vs_league_average == pytest.approx(70.32258064516128)

    league_rows = {(m.slice_key, m.stat_key): m for m in window.league_metrics if m.stat_key == "PTS"}
    assert set(league_rows) == {(slice_key, "PTS") for slice_key in SHOT_TYPE_SLICES}
    assert league_rows[("less_than_10_ft", "PTS")].average_allowed_per_48 == 77.5
    assert league_rows[("less_than_10_ft", "PTS")].sigma == pytest.approx(32.473322384176626)
    assert league_rows[("less_than_10_ft", "PTS")].team_count == 30


def test_the_row_is_keyed_by_display_slice_and_carries_the_points_markets():
    assert MatchupService._metric_key("shot_types", "catch_and_shoot", "PTS") == "Catch and Shoot:PTS"
    assert MatchupService._metric_key("shot_types", "pullups", "PTS") == "Pullups:PTS"
    assert MatchupService._metric_key("shot_types", "less_than_10_ft", "PTS") == "Less Than 10 ft:PTS"
    assert slice_markets("shot_types", "catch_and_shoot", "PTS") == ("PTS", "PA", "PR", "PRA")
    # The rows that were already there keep their markets.
    assert slice_markets("shot_types", "catch_and_shoot", "FG2M") == ("PTS", "PA", "PR", "PRA")
    assert slice_markets("shot_types", "catch_and_shoot", "FG3M") == ("3PM", "PTS", "PA", "PR", "PRA")


def test_the_publication_taxonomy_does_not_gain_a_points_metric():
    assert not any(key.endswith("_PTS") for key in NBA_PUBLICATION_TAXONOMY["shot_types"])
    assert len(NBA_PUBLICATION_TAXONOMY["shot_types"]) == 12


def test_the_team_filter_ranks_the_same_combined_metric_in_the_matchup_rank_order():
    assert defense_sheet_filter("sheet:shot_types:Catch and Shoot:PTS") == (
        "shot_types",
        "catch_and_shoot_PTS",
    )
    assert "Pullups:PTS" in DEFENSE_SHEET_ROW_METRICS["shot_types"]
    service = TeamFilterRankingService(
        StubReader({STREAM: read(STREAM, league(_per48))}), governance_resolver=StubGovernance()
    )

    ranked = service.ranked_teams("sheet:shot_types:Catch and Shoot:PTS", SEASON)

    # Most allowed first: the exact reverse of the Matchup's ascending rank.
    expected = sorted(POINTS, key=lambda tricode: -POINTS[tricode])
    assert ranked == expected
    window = _window()
    assert [_points(window, tricode).rank for tricode in ranked] == list(range(30, 0, -1))
    assert ranked == service.ranked_teams("C&S PTS", SEASON)


def test_the_filter_definition_sums_two_and_three_point_makes():
    ranking = team_filter_ranking("sheet:shot_types:Pullups:PTS")
    assert dict(ranking.numerator) == {"pullups_FG2M": 2.0, "pullups_FG3M": 3.0}
    assert ranking.denominator is None


def test_a_legacy_fact_window_carries_the_same_points_row():
    facts = [
        StoredTeamMatchupFact(
            team_id=team_id,
            base="shot_types",
            slice_key=slice_key,
            stat_key=stat_key,
            raw_value=value,
            denominator_value=48,
            denominator_unit="minutes",
            provider="nba_stats",
            retrieved_at=RETRIEVED_AT,
        )
        for tricode, team_id in ((c, t) for t, c in NBA_TEAM_ID_TO_TRICODE.items())
        for slice_key in SHOT_TYPE_SLICES
        for stat_key, value in (
            ("FG2M", float(TEAM_INDEX[tricode])),
            ("FG3M", float((7 * TEAM_INDEX[tricode]) % 31)),
        )
    ]
    scope = TeamMatchupSnapshotScope(season=SEASON, as_of=date(2026, 1, 10), window_games=None)
    window = TeamMatchupQueryService(repository=None)._build_window(
        scope, fact_scopes={"shot_types": scope}, facts=facts, observations=[]
    )

    atl = _points(window, "ATL")
    assert (atl.allowed_per_48, atl.rank) == (23.0, 3)
    assert atl.percent_vs_league_average == pytest.approx(-70.3225806451613)
