"""`GET /api/teams/defense-ranks`: one base's Defense Sheet for the whole league.

The CourtAI MCP reads this route to answer "who allows the most X".  Every
row must be the Matchup Defense Sheet's row for all thirty teams, so the
parity tests build the Sheet the way the Matchup does from the very
publication rows the route reads.
"""

from datetime import date

import pytest

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.rebound_types import REBOUND_TYPE_WIRE_NAMES
from app.domain.team_matchup_taxonomy import DEFENSE_SHEET_BASES, NBA_PUBLICATION_TAXONOMY
from app.services.matchup import MatchupService, _WindowMetricIndex
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.traditional_opponent_publications import normalize_traditional_opponent_window
from tests.services.test_defense_sheet_team_filters import STREAM_BY_BASE, _distinct, _per48_builder
from tests.support.publication_stubs import RETRIEVED_AT, SEASON, league, read
from tests.support.publication_stubs import team_service as _team_service

STREAMS = {**STREAM_BY_BASE, "rebound_types": "rebound_types_season"}


def _distinct_per48(base):
    if base != "rebound_types":
        return _per48_builder(base)
    return lambda tricode: {
        key: _distinct(tricode, position) for position, key in enumerate(REBOUND_TYPE_WIRE_NAMES)
    }


def _client(dependencies, client, rows_by_base):
    dependencies.team_service = _team_service(
        {STREAMS[base]: read(STREAMS[base], rows) for base, rows in rows_by_base.items()}
    )
    return client


@pytest.fixture
def distinct_rows():
    return {base: league(_distinct_per48(base)) for base in DEFENSE_SHEET_BASES}


@pytest.fixture
def ranks_client(dependencies, client, distinct_rows):
    return _client(dependencies, client, distinct_rows)


@pytest.fixture
def zones_client(dependencies, client):
    zones = {key: 7.0 for key in NBA_PUBLICATION_TAXONOMY["shot_zones"]}
    rows = league(
        lambda tricode: {key: value * (1.5 if tricode == "LAL" else 1.0) for key, value in zones.items()}
    )
    return _client(dependencies, client, {"shot_zones": rows})


def _matchup_sheet(base, rows):
    """``teams[].defense_sheet[base]`` per tricode, composed as the Matchup composes it."""

    if base == "traditional":
        rows = normalize_traditional_opponent_window(rows, stream_key=STREAMS[base]).teams
    window = TeamMatchupQueryService._publication_base_window(
        SEASON,
        cutoff=date(2026, 1, 10),
        window_games=None,
        base=base,
        rows=rows,
        retrieved_at=RETRIEVED_AT,
    )
    windows = {"season": window}
    indexes = {"season": _WindowMetricIndex.build(window)}
    team_ids = tuple(NBA_TEAM_ID_TO_TRICODE)
    availability = MatchupService._surface_availability(windows, indexes, team_ids)
    return {
        tricode: MatchupService._team(
            {"tricode": tricode, "name": tricode}, team_id, windows, indexes, availability
        )["defense_sheet"][base]
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    }


def _ranks(client, query):
    response = client.get(f"/api/teams/defense-ranks?{query}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


@pytest.mark.parametrize("base", DEFENSE_SHEET_BASES)
def test_every_row_is_the_matchup_sheet_row_for_all_thirty_teams(ranks_client, distinct_rows, base):
    body = _ranks(ranks_client, f"base={base}")
    sheet = _matchup_sheet(base, distinct_rows[base])

    assert (body["status"], body["reason"], body["season"], body["window"]) == ("ok", None, SEASON, "season")
    assert [row["row"] for row in body["rows"]] == [f"sheet:{base}:{row['key']}" for row in sheet["ATL"]]
    for served in body["rows"]:
        assert len(served["teams"]) == 30
        assert [team["rank"] for team in served["teams"]] == sorted(
            (team["rank"] for team in served["teams"]), reverse=True
        )
        for team in served["teams"]:
            matchup = next(
                row for row in sheet[team["tricode"]] if f"sheet:{base}:{row['key']}" == served["row"]
            )
            assert served["label"] == matchup["label"]
            assert (team["value"], team["rank"]) == (
                matchup["season"]["allowed_per_48"],
                matchup["season"]["rank"],
            )
            assert team["vs_league"] == pytest.approx(matchup["season"]["percent_vs_league_average"] / 100)


def test_a_row_carries_each_team_value_rank_and_vs_league(zones_client):
    body = _ranks(zones_client, "base=shot_zones")
    row = next(row for row in body["rows"] if row["row"] == "sheet:shot_zones:Restricted Area:FGM")

    assert body["as_of"] == "2026-01-10"
    assert body["provenance"]["sources"] == {}
    assert [
        {
            key: entry[key]
            for key in ("stream_key", "publication_id", "season", "coverage_cutoff", "freshness")
        }
        for entry in body["provenance"]["generation"]
    ] == [
        {
            "stream_key": "exact_shot_zones_opponent_season",
            "publication_id": "publication-1",
            "season": SEASON,
            "coverage_cutoff": "2026-01-10T12:00:00+00:00",
            "freshness": "fresh",
        }
    ]
    assert {key: value for key, value in row.items() if key != "teams"} == {
        "row": "sheet:shot_zones:Restricted Area:FGM",
        "slice": "Restricted Area",
        "metric": "FGM",
        "label": "Restricted Area FGM",
        "unit": "per48",
        "league_average": 7.116667,
    }
    # The Lakers allow the most, so they lead at rank 30; the tied 29 share rank 1.
    assert row["teams"][0] == {"tricode": "LAL", "value": 10.5, "rank": 30, "vs_league": 0.47540984}
    assert row["teams"][1] == {"tricode": "ATL", "value": 7.0, "rank": 1, "vs_league": -0.01639344}
    assert len(row["teams"]) == 30


def test_traditional_rows_have_no_slice_and_include_the_defensive_columns(ranks_client):
    body = _ranks(ranks_client, "base=traditional")

    assert {row["metric"] for row in body["rows"]} >= {"OPP_TOV", "OPP_STL", "OPP_BLK", "OPP_PTS"}
    assert {row["slice"] for row in body["rows"]} == {None}


def test_shot_types_omit_the_impossible_threes_inside_ten_feet(ranks_client):
    rows = {row["row"] for row in _ranks(ranks_client, "base=shot_types")["rows"]}

    assert "sheet:shot_types:Less Than 10 ft:FG3M" not in rows
    assert "sheet:shot_types:Less Than 10 ft:PTS" in rows
    assert "sheet:shot_types:Catch and Shoot:FG3M" in rows


def test_a_metric_narrows_to_that_row_of_every_slice(ranks_client):
    body = _ranks(ranks_client, "base=shot_zones&metric=FGM")

    assert [row["row"] for row in body["rows"]] == [
        "sheet:shot_zones:Above the Break 3:FGM",
        "sheet:shot_zones:Corner 3:FGM",
        "sheet:shot_zones:In The Paint (Non-RA):FGM",
        "sheet:shot_zones:Mid-Range:FGM",
        "sheet:shot_zones:Restricted Area:FGM",
    ]


def test_a_row_reference_narrows_to_that_one_row(ranks_client):
    body = _ranks(ranks_client, "base=play_types&metric=sheet:play_types:Isolation:POSS")

    assert [row["row"] for row in body["rows"]] == ["sheet:play_types:Isolation:POSS"]
    assert len(body["rows"][0]["teams"]) == 30


@pytest.mark.parametrize(
    ("query", "parameter"),
    [
        ("", "base"),
        ("base=rebounding", "base"),
        ("base=shot_zones&metric=PPP", "metric"),
        ("base=shot_zones&metric=sheet:play_types:Isolation:PTS", "metric"),
        ("base=shot_types&metric=sheet:shot_types:Less Than 10 ft:FG3M", "metric"),
    ],
)
def test_an_unknown_base_or_metric_is_a_bad_request(ranks_client, query, parameter):
    response = ranks_client.get(f"/api/teams/defense-ranks?{query}")

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert (error["code"], error["details"]) == ("invalid_input", {"parameters": [parameter]})


def test_an_unpublished_league_serves_no_rows(dependencies, client):
    body = _ranks(_client(dependencies, client, {}), "base=play_types")
    generation = body.pop("provenance")["generation"]

    assert [(entry["stream_key"], entry["freshness"]) for entry in generation] == [
        ("synergy_play_types_opponent_season", "unavailable")
    ]
    assert body == {
        "season": SEASON,
        "base": "play_types",
        "window": "season",
        "status": "unavailable",
        "reason": "missing",
        "as_of": None,
        "rows": [],
    }


def test_a_partial_league_serves_no_rows(dependencies, client, distinct_rows):
    partial = distinct_rows["traditional"][:29]

    body = _ranks(_client(dependencies, client, {"traditional": partial}), "base=traditional")

    assert (body["status"], body["reason"], body["rows"]) == ("unavailable", "league_incomplete", [])


def test_a_malformed_season_is_a_bad_request(ranks_client):
    response = ranks_client.get("/api/teams/defense-ranks?base=traditional&season=2026-28")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
