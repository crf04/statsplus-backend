"""`GET /api/teams/stats` contract for the two Log Workspace panels.

`OpposingTeamProfile` requests all five categories and adds `date` once the
user picks one; `PlayerProfile` requests `Playtypes` or `Assists` and never
sends a date.  These tests drive the real service over seeded publications so
the panel-visible shape is proven, not mocked.
"""

from datetime import datetime, timezone

import pytest

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import NBA_PUBLICATION_TAXONOMY
from app.services.database_first_activation import (
    PublicationRead,
    PublicationTeamWindowRow,
)
from app.services.ledger_derivations import (
    ASSIST_DERIVED_METRICS,
    TEAM_METRICS,
)
from app.services.team_filter_rankings import TeamFilterRankingService
from app.services.team_service import TeamService

SEASON = "2025-26"
RETRIEVED_AT = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
LAKERS = "Los Angeles Lakers"


def _read(stream_key, per48_for):
    rows = tuple(
        PublicationTeamWindowRow(
            team_id=team_id,
            team_tricode=tricode,
            game_ids=("0022500001",),
            game_count=1,
            per48=per48_for(tricode),
            league_average={},
            population_sigma={},
            competition_rank={},
        )
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    )
    return PublicationRead(
        stream_key=stream_key,
        publication_id="publication-1",
        season=SEASON,
        cutoff=RETRIEVED_AT.isoformat(),
        version=1,
        status="active",
        freshness="fresh",
        age_seconds=0,
        payload={"rows": []},
        retrieved_at=RETRIEVED_AT,
        decoded=rows,
    )


def _seeded_reads():
    """One published generation of all five Season streams."""

    def scaled(metrics, factor):
        return {key: value * factor for key, value in metrics.items()}

    traditional = {metric: 4.0 for metric in TEAM_METRICS}
    traditional.update(
        points=112.0, field_goals_made=42.0, field_goals_attempted=88.0,
        three_pointers_made=13.0, three_pointers_attempted=36.0,
    )
    assists = {metric: 5.0 for metric in ASSIST_DERIVED_METRICS}
    play_types = {key: 3.0 for key in NBA_PUBLICATION_TAXONOMY["play_types"]}
    shot_types = {key: 6.0 for key in NBA_PUBLICATION_TAXONOMY["shot_types"]}
    shot_zones = {key: 7.0 for key in NBA_PUBLICATION_TAXONOMY["shot_zones"]}
    # The Lakers row differs from the league so a rank is never vacuous.
    factor_for = lambda tricode: 1.5 if tricode == "LAL" else 1.0  # noqa: E731

    return {
        "traditional_opponent_season": _read(
            "traditional_opponent_season",
            lambda tricode: scaled(traditional, factor_for(tricode)),
        ),
        "assist_locations_season": _read(
            "assist_locations_season",
            lambda tricode: scaled(assists, factor_for(tricode)),
        ),
        "synergy_play_types_opponent_season": _read(
            "synergy_play_types_opponent_season",
            lambda tricode: scaled(play_types, factor_for(tricode)),
        ),
        "grouped_shot_types_opponent_season": _read(
            "grouped_shot_types_opponent_season",
            lambda tricode: scaled(shot_types, factor_for(tricode)),
        ),
        "exact_shot_zones_opponent_season": _read(
            "exact_shot_zones_opponent_season",
            lambda tricode: scaled(shot_zones, factor_for(tricode)),
        ),
    }


class _StubReader:
    def __init__(self, reads):
        self._reads = reads

    def read_many(self, stream_keys, *, season=None):
        return {
            stream_key: self._reads.get(
                stream_key,
                PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                ),
            )
            for stream_key in tuple(stream_keys)
        }


class _StubGovernance:
    def resolve_team_game_ids(self, season, cutoff, *, window, **kwargs):
        return {
            team_id: frozenset({"0022500001"})
            for team_id in NBA_TEAM_ID_TO_TRICODE
        }


def _team_service(reads):
    return TeamService(
        None,
        settings=RuntimeSettings(
            environment="testing", nba={"current_season": SEASON}
        ),
        season_publications=TeamFilterRankingService(
            _StubReader(reads), governance_resolver=_StubGovernance()
        ),
    )


@pytest.fixture
def panel_client(dependencies, client):
    """The route's own client, serving the real publication-backed service."""

    dependencies.team_service = _team_service(_seeded_reads())
    return client


@pytest.fixture
def unpublished_client(dependencies, client):
    dependencies.team_service = _team_service({})
    return client


def _get(client, category, **params):
    query = "&".join(
        f"{key}={value}" for key, value in
        (("team", LAKERS), ("category", category), *params.items())
    )
    return client.get(f"/api/teams/stats?{query}")


def test_opposing_team_profile_traditional_request(panel_client):
    response = _get(panel_client, "Traditional")

    assert response.status_code == 200
    body = response.get_json()
    assert body["OPP_PTS"] == 168.0
    assert body["OPP_PTS_RANK"] == 30
    assert body["OPP_STL+BLK"] == 12.0
    assert body["OPP_FG_PCT"] == pytest.approx(63.0 / 132.0)
    assert body["OPP_PTS_vs_avg_pct"] == pytest.approx(
        (168.0 / ((168.0 + 29 * 112.0) / 30) - 1) * 100
    )
    assert "OPP_OREB" not in body
    assert "OPP_DREB" not in body


def test_player_profile_playtypes_request_sends_no_date(panel_client):
    response = _get(panel_client, "Playtypes")

    assert response.status_code == 200
    body = response.get_json()
    # Every team's points per possession is 1.0, so every ratio is 1.0.
    assert body["Transition"] == pytest.approx(1.0)
    assert body["PRBallHandler_RANK"] == 1
    assert "Transition_vs_avg_pct" not in body


def test_player_profile_assists_request(panel_client):
    response = _get(panel_client, "Assists")

    assert response.status_code == 200
    body = response.get_json()
    league_average = (7.5 + 29 * 5.0) / 30
    assert body["Assists"] == pytest.approx(7.5 / league_average)
    assert body["AssistPoints"] == pytest.approx(
        (2 * 7.5 + 3 * 7.5) / ((37.5 + 29 * 25.0) / 30)
    )
    assert body["AssistPoints_RANK"] == 30


def test_opposing_team_profile_zone_shooting_request(panel_client):
    response = _get(panel_client, "Zone Shooting")

    assert response.status_code == 200
    body = response.get_json()
    assert body["Restricted Area_OPP_FGM"] == 10.5
    assert body["Restricted Area_OPP_FGA_RANK"] == 30
    assert body["Above the Break 3_OPP_FGM_vs_avg_pct"] == pytest.approx(
        (10.5 / ((10.5 + 29 * 7.0) / 30) - 1) * 100
    )


def test_opposing_team_profile_shooting_type_request(panel_client):
    response = _get(panel_client, "Shooting Type")

    assert response.status_code == 200
    body = response.get_json()
    assert [row["ShootingType"] for row in body] == [
        "Catch and Shoot", "Pullups", "Less Than 10 ft"
    ]
    assert body[0]["FG2M"] == 9.0
    assert body[0]["PTS"] == 2 * 9.0 + 3 * 9.0
    assert body[0]["PTS_RANK"] == 30


def test_a_picked_date_is_ignored_by_every_category(panel_client):
    for category in (
        "Traditional", "Playtypes", "Assists", "Zone Shooting", "Shooting Type"
    ):
        dated = _get(panel_client, category, date="2026-01-05")
        undated = _get(panel_client, category)

        assert dated.status_code == 200
        assert dated.get_json() == undated.get_json()


def test_an_unknown_category_is_a_bad_request(panel_client):
    response = _get(panel_client, "Rebounding")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "Unsupported team stats category: Rebounding.",
        }
    }


def test_a_category_with_no_publication_reports_no_data(unpublished_client):
    for category in (
        "Traditional", "Playtypes", "Assists", "Zone Shooting", "Shooting Type"
    ):
        response = _get(unpublished_client, category)

        assert response.status_code == 404
        assert response.get_json()["error"]["code"] == "resource_not_found"
