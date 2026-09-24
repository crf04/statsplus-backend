"""Defense Sheet row Team Filters (``sheet:<base>:<row key>``, #308).

Every guarantee here is proved against the Defense Sheet built from the very
publication rows the Team Filter ranks, so the two can never be checked
against different evidence.
"""

from datetime import date

import pytest

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    DEFENSE_SHEET_BASES,
    DEFENSE_SHEET_ROW_METRICS,
    LEDGER_DEFENSE_SHEET_METRICS,
    NBA_PUBLICATION_STREAMS,
    NBA_PUBLICATION_TAXONOMY,
    defense_sheet_filter,
)
from app.models.catalogs import SUPPORTED_TEAM_FILTERS
from app.services.matchup import DEFENSE_BASES, MatchupService
from app.services.team_filter_rankings import (
    TEAM_FILTER_RANKINGS,
    TeamFilterRankingService,
    team_filter_ranking,
)
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.traditional_opponent_publications import (
    normalize_traditional_opponent_window,
)
from tests.support.publication_stubs import (
    RETRIEVED_AT,
    SEASON,
    StubGovernance,
    StubReader,
    league,
    read,
    traditional_per48,
)

TEAM_COUNT = len(NBA_TEAM_ID_TO_TRICODE)
TEAM_INDEX = {
    tricode: index
    for index, tricode in enumerate(NBA_TEAM_ID_TO_TRICODE.values(), start=1)
}
STREAM_BY_BASE = {
    "traditional": "traditional_opponent_season",
    "assist_locations": "assist_locations_season",
    **{
        base: template.format(window="season")
        for base, template in NBA_PUBLICATION_STREAMS.items()
    },
}


def _distinct(tricode, position):
    """A value per team that differs across all thirty teams for one metric.

    Thirty-one is prime, so multiplying the team index 1..30 by any factor
    1..30 permutes 1..30: every metric orders the league differently.
    """

    return float((TEAM_INDEX[tricode] * (position + 1)) % 31)


def _per48_builder(base):
    if base == "traditional":
        def per48(tricode):
            values = {
                metric: _distinct(tricode, position) + 1.0
                for position, metric in enumerate(
                    ("turnovers", "steals", "blocks")
                )
            }
            return traditional_per48(
                offensive_rebounds=_distinct(tricode, 3),
                defensive_rebounds=3.0,
                **values,
            )
        return per48
    if base == "assist_locations":
        keys = tuple(LEDGER_DEFENSE_SHEET_METRICS[base].values())
    else:
        keys = tuple(sorted(NBA_PUBLICATION_TAXONOMY[base]))

    def per48(tricode):
        return {
            key: _distinct(tricode, position) for position, key in enumerate(keys)
        }
    return per48


def _rows(base, per48=None):
    return league(per48 or _per48_builder(base))


def _service(base, rows):
    return TeamFilterRankingService(
        StubReader({STREAM_BY_BASE[base]: read(STREAM_BY_BASE[base], rows)}),
        governance_resolver=StubGovernance(),
    )


def _sheet_ranks(base, rows):
    """The Defense Sheet's Season rank per row key, per team tricode.

    The Sheet is built exactly as the Matchup builds it from a publication:
    the traditional family normalized first, then the publication window
    projected, and each row keyed as ``defense_sheet[<base>][].key``.
    """

    if base == "traditional":
        rows = normalize_traditional_opponent_window(
            rows, stream_key=STREAM_BY_BASE[base]
        ).teams
    window = TeamMatchupQueryService._publication_base_window(
        SEASON,
        cutoff=date(2026, 1, 10),
        window_games=None,
        base=base,
        rows=rows,
        retrieved_at=RETRIEVED_AT,
    )
    ranks = {}
    for team_id, metrics in window.team_metrics.items():
        for metric in metrics:
            key = MatchupService._metric_key(
                metric.base, metric.slice_key, metric.stat_key
            )
            ranks.setdefault(key, {})[NBA_TEAM_ID_TO_TRICODE[team_id]] = metric.rank
    return ranks


# --- vocabulary --------------------------------------------------------------


def test_the_sheet_filter_bases_are_the_matchup_sheet_bases():
    assert DEFENSE_BASES == DEFENSE_SHEET_BASES
    assert set(DEFENSE_SHEET_ROW_METRICS) == set(DEFENSE_SHEET_BASES)


@pytest.mark.parametrize("base", DEFENSE_SHEET_BASES)
def test_every_sheet_row_key_is_a_filter_and_every_filter_a_sheet_row(base):
    """The filter vocabulary is exactly the rows the Sheet publishes."""

    assert set(_sheet_ranks(base, _rows(base))) == set(
        DEFENSE_SHEET_ROW_METRICS[base]
    )


def test_contract_examples_resolve_to_their_publication_metrics():
    assert defense_sheet_filter("sheet:play_types:PRBallHandler:PTS") == (
        "play_types",
        "PRBallHandler_PTS",
    )
    assert defense_sheet_filter("sheet:shot_zones:Restricted Area:FGM") == (
        "shot_zones",
        "Restricted Area_FGM",
    )
    assert defense_sheet_filter("sheet:shot_types:Catch and Shoot:FG3M") == (
        "shot_types",
        "catch_and_shoot_FG3M",
    )
    assert defense_sheet_filter("sheet:traditional:OPP_REB") == (
        "traditional",
        "rebounds",
    )
    assert defense_sheet_filter("sheet:assist_locations:Corner3Assists") == (
        "assist_locations",
        "corner3_assists",
    )


@pytest.mark.parametrize(
    "value",
    [
        "sheet:",
        "sheet:play_types",
        "sheet:play_types:",
        "sheet:play_types:Transition",
        "sheet:play_types:Transition:PPP",
        "sheet:unknown:Transition:PTS",
        "sheet:shot_zones:Paint:FGM",
        "sheet:shot_types:catch_and_shoot:FG3M",
        "sheet:traditional:OPP_PTS",
        "Sheet:play_types:Transition:PTS",
        "play_types:Transition:PTS",
    ],
)
def test_unknown_sheet_references_are_not_filters(value):
    assert defense_sheet_filter(value) is None
    assert team_filter_ranking(value) is None
    with pytest.raises(ValueError, match="Unsupported team filter"):
        TeamFilterRankingService(StubReader({})).rank_all((value,), SEASON)


def test_sheet_references_never_join_the_named_vocabulary():
    assert not any(name.startswith("sheet:") for name in SUPPORTED_TEAM_FILTERS)
    assert not any(name.startswith("sheet:") for name in TEAM_FILTER_RANKINGS)


# --- the 31 - r guarantee, per base ------------------------------------------


@pytest.mark.parametrize("base", DEFENSE_SHEET_BASES)
def test_a_sheet_filter_ranks_every_team_at_31_minus_its_sheet_rank(base):
    rows = _rows(base)
    service = _service(base, rows)
    sheet = _sheet_ranks(base, rows)
    references = {f"sheet:{base}:{key}": key for key in sheet}

    rankings = service.rank_all(tuple(references), SEASON)

    for reference, key in references.items():
        ranked = rankings[reference]
        assert len(ranked) == TEAM_COUNT, reference
        assert {
            tricode: position for position, tricode in enumerate(ranked, start=1)
        } == {
            tricode: TEAM_COUNT + 1 - rank for tricode, rank in sheet[key].items()
        }, reference


# --- ties ------------------------------------------------------------------


def test_tied_teams_hold_consecutive_ranks_in_tricode_order():
    """The documented tie rule, at the boundary where 31 - r can differ.

    The Sheet gives the m teams tied at Season rank r one shared rank.  The
    Team Filter orders them by tricode inside the positions 32 - r - m through
    31 - r, and only the last of them sits exactly at 31 - r.  Every untied
    team keeps the guarantee exactly.
    """

    tied = {"BOS", "LAL", "MIA"}
    base = "shot_zones"

    def per48(tricode):
        values = _per48_builder(base)(tricode)
        values["Restricted Area_FGM"] = (
            0.5 if tricode in tied else _distinct(tricode, 0) + 1.0
        )
        return values

    rows = _rows(base, per48)
    sheet = _sheet_ranks(base, rows)["Restricted Area:FGM"]
    ranked = _service(base, rows).ranked_teams(
        "sheet:shot_zones:Restricted Area:FGM", SEASON
    )
    position = {tricode: index for index, tricode in enumerate(ranked, start=1)}

    # The three tied teams allow the fewest: the Sheet ranks them all 1.
    assert {sheet[tricode] for tricode in tied} == {1}
    assert [tricode for tricode in ranked if tricode in tied] == ["BOS", "LAL", "MIA"]
    assert [position[tricode] for tricode in ("BOS", "LAL", "MIA")] == [28, 29, 30]
    assert position["MIA"] == TEAM_COUNT + 1 - sheet["MIA"]
    for tricode in set(sheet) - tied:
        assert position[tricode] == TEAM_COUNT + 1 - sheet[tricode]


# --- existing filters are unchanged ----------------------------------------


def test_named_filters_keep_their_own_definitions():
    for name in SUPPORTED_TEAM_FILTERS:
        assert team_filter_ranking(name) is TEAM_FILTER_RANKINGS[name]


def test_a_sheet_filter_does_not_change_a_named_filter_ranking():
    """Play types still rank points per possession beside a Sheet PTS row."""

    def per48(tricode):
        metrics = {key: 1.0 for key in NBA_PUBLICATION_TAXONOMY["play_types"]}
        # GSW allows more transition points per 48, LAL more per possession.
        points, possessions = {"LAL": (22.0, 20.0), "GSW": (30.0, 30.0)}.get(
            tricode, (10.0, 20.0)
        )
        metrics["Transition_PTS"] = points
        metrics["Transition_POSS"] = possessions
        return metrics

    rows = _rows("play_types", per48)
    service = _service("play_types", rows)

    alone = service.ranked_teams("Transition", SEASON)
    together = service.rank_all(
        ("Transition", "sheet:play_types:Transition:PTS"), SEASON
    )

    assert together["Transition"] == alone
    assert alone[:2] == ["LAL", "GSW"]
    assert together["sheet:play_types:Transition:PTS"][:2] == ["GSW", "LAL"]


def test_a_sheet_filter_refuses_an_untrusted_publication_like_any_filter():
    assert TeamFilterRankingService(StubReader({})).ranked_teams(
        "sheet:shot_zones:Corner 3:FGM", SEASON
    ) == []


def test_a_sheet_row_that_any_team_lacks_refuses_the_whole_ranking():
    """The corrected #91 contract: no per-team exclusion for ``sheet:``.

    A ``sheet:`` ranking has no denominator, so there is no "no rate" case.
    On a ledger base a team missing the row's metric makes the publication
    untrusted for that one filter, which returns an empty ranking, while the
    base's other rows still rank all thirty teams.
    """

    base = "assist_locations"
    full = _per48_builder(base)

    def per48(tricode):
        values = full(tricode)
        if tricode == "LAL":
            del values["corner3_assists"]
        return values

    rows = _rows(base, per48)
    service = _service(base, rows)
    lacking = "sheet:assist_locations:Corner3Assists"
    others = tuple(
        f"sheet:{base}:{key}"
        for key in DEFENSE_SHEET_ROW_METRICS[base]
        if f"sheet:{base}:{key}" != lacking
    )

    rankings = service.rank_all((lacking, *others), SEASON)

    assert rankings[lacking] == []
    assert others
    for reference in others:
        assert len(rankings[reference]) == TEAM_COUNT, reference
        assert "LAL" in rankings[reference], reference
