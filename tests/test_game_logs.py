"""Contract tests for the typed game-log query/response slice (#9).

These tests pin the behavior introduced by the typed ``GameLogQuery`` /
``GameLogResponse`` models: malformed filters fail with a clear 400, the
response uses ordinary JSON arrays (never pandas JSON strings), the filtering
pipeline accepts one typed query, and the synchronous service/route path stays
free of per-request event loops (#10).
"""

import pandas as pd
import pytest
import requests

from app.config.settings import (
    NBASeasonSettings,
    RuntimeSettings,
)
from app.models.game_logs import GameLogQuery, GameLogResponse
from app.services.game_service import GameService


# ---------------------------------------------------------------- query model


def test_game_log_query_accepts_typed_and_raw_filters():
    query = GameLogQuery(
        season_filter="2024-25",
        minutes_filter="25,48",
        rank_filter=["10"],
        teams_against=["OPP_PTS"],
        players_on=["Jayson Tatum"],
        location_filter="Home",
        game_filter="5",
        self_filters={"PTS": "25,60"},
    )

    assert query.minutes_filter == (25, 48)
    assert query.rank_filter == [10]
    assert query.teams_against == ["OPP_PTS"]
    assert query.location_filter == "Home"
    assert query.game_filter == 5
    assert query.self_filters == {"PTS": (25.0, 60.0)}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes_filter": "25"},
        {"minutes_filter": "20,10"},
        {"location_filter": "everywhere"},
        {"date_filter": "not-a-date"},
        {"teams_against": ["OPP_PTS"], "rank_filter": []},
        {"teams_against": ["OPP_PTS"], "rank_filter": ["3", "9"]},
        {"game_filter": 0},
        {"rank_filter": ["abc"]},
        {"self_filters": {"PTS": "25"}},
        {"self_filters": {"PTS": ["low", "high"]}},
    ],
)
def test_game_log_query_rejects_malformed_filters(kwargs):
    with pytest.raises(Exception):
        GameLogQuery(season_filter="2024-25", **dict(kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"teams_against": ["NOT_A_FILTER"]},
        {"self_filters": {"YOLO": "1,10"}},
    ],
)
def test_game_log_query_rejects_unsupported_closed_filters(kwargs):
    from app.models.catalogs import (
        SUPPORTED_SELF_FILTER_STATS,
        SUPPORTED_TEAM_FILTERS,
    )

    with pytest.raises(Exception):
        GameLogQuery(season_filter="2024-25", **dict(kwargs))

    assert "OPP_PTS" in SUPPORTED_TEAM_FILTERS
    assert "PTS" in SUPPORTED_SELF_FILTER_STATS


@pytest.mark.parametrize(
    "stat",
    [
        "MIN", "FG_PCT", "OREB", "DREB", "PF", "PLUS_MINUS", "PRA",
        "PA", "PR", "RA", "STKS", "FD_PTS",
    ],
)
def test_documented_self_filter_stats_are_accepted(stat):
    query = GameLogQuery(
        season_filter="2024-25",
        self_filters={stat: "0,100"},
    )

    assert stat in query.self_filters


@pytest.mark.parametrize("legacy", ["<10 Ft", "<10 ft", "Less Than 10 Ft"])
def test_less_than_ten_feet_filter_aliases_are_normalized(legacy):
    query = GameLogQuery(
        season_filter="2024-25",
        teams_against=[legacy],
        rank_filter=[1],
    )

    assert query.teams_against == ["Less Than 10 ft"]


# ------------------------------------------------------------- response model


def test_game_log_response_models_plain_arrays():
    response = GameLogResponse(
        game_logs=[{"GAME_DATE": "2024-01-15", "PTS": 25}],
        averages=[{"PTS": 25.0}],
        season_averages=[{"PTS": 24.0}],
        next_game="Boston Celtics",
    )

    dumped = response.model_dump()
    assert isinstance(dumped["game_logs"], list)
    assert isinstance(dumped["averages"], list)
    assert isinstance(dumped["season_averages"], list)
    assert dumped["next_game"] == "Boston Celtics"


def test_game_log_response_allows_empty_arrays():
    dumped = GameLogResponse(
        game_logs=[], averages=[], season_averages=[], next_game=None
    ).model_dump()
    assert dumped["game_logs"] == []
    assert dumped["averages"] == []
    assert dumped["season_averages"] == []


# ------------------------------------------------------------------ helpers


def _stub_route_settings(monkeypatch):
    from app.routes import game_routes

    monkeypatch.setattr(game_routes, "_default_season", lambda: "2024-25")


def _game_logs_frame():
    """Fabricated full player game-log frame with the real scored columns."""
    columns = [
        "GAME_DATE", "GAME_ID", "PLAYER_NAME", "TEAM_ID", "TEAM_ABBREVIATION",
        "MATCHUP", "MIN", "PTS", "REB", "AST", "PRA", "PA", "PR", "RA", "STKS",
        "FD_PTS", "NBA_FANTASY_PTS", "FGM", "FGA", "FG_PCT", "FG2M", "FG2A",
        "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB", "TOV", "STL", "BLK", "PF",
        "PLUS_MINUS", "MIN_SEC",
    ]
    rows = [
        ["2024-01-15", "001", "LeBron James", 1610612738, "BOS", "BOS vs. LAL",
         30, 25, 8, 7, 40, 32, 33, 15, 2, 44.2, 44.2, 9, 16, 56.3, 6, 9,
         3, 7, 4, 5, 1, 7, 2, 1, 1, 2, 12, 1800],
        ["2024-01-17", "002", "LeBron James", 1610612738, "BOS", "BOS @ MIA",
         20, 15, 4, 5, 24, 20, 19, 9, 0, 27.0, 27.0, 6, 12, 50.0, 4, 6,
         2, 6, 1, 2, 0, 4, 3, 0, 0, 3, -4, 1200],
        ["2024-01-19", "003", "LeBron James", 1610612738, "BOS", "BOS vs. CHI",
         40, 30, 10, 9, 49, 39, 40, 19, 3, 52.0, 52.0, 11, 20, 55.0, 7, 11,
         4, 9, 4, 4, 2, 8, 1, 2, 1, 1, 18, 2400],
    ]
    return pd.DataFrame(rows, columns=columns)


def _make_service(monkeypatch, mock_db_engine, mock_redis_client):
    settings = RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings(current_season="2024-25"),
    )
    service = GameService(mock_db_engine, mock_redis_client, settings=settings)

    def fake_logs(player_name, season):
        return _game_logs_frame(), None

    def fake_filter_teams(team_filter, rank, date_filter=None):
        return ["LAL"]

    monkeypatch.setattr(service, "_get_game_logs", fake_logs)
    monkeypatch.setattr(service, "filter_teams", fake_filter_teams)
    return service


# -------------------------------------------------------------------- service


def test_game_service_returns_plain_arrays_for_filtered_logs(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    query = GameLogQuery(
        season_filter="2024-25",
        minutes_filter="25,48",
        location_filter="Home",
        teams_against=["OPP_PTS"],
        rank_filter=[1],
    )

    result = service.get_filtered_logs("LeBron James", query)

    assert isinstance(result["game_logs"], list)
    assert isinstance(result["averages"], list)
    assert isinstance(result["season_averages"], list)
    assert result["averages"] and result["season_averages"]
    assert len(result["game_logs"]) == 1
    assert result["game_logs"][0]["MATCHUP"] == "BOS vs. LAL"
    assert result["game_logs"][0]["MIN"] == 30
    assert result["next_game"] is None

    GameLogResponse.model_validate(result)


def test_service_returns_empty_arrays_when_no_games_match(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    query = GameLogQuery(season_filter="2024-25", minutes_filter="45,48")

    result = service.get_filtered_logs("LeBron James", query)

    assert result["game_logs"] == []
    assert result["averages"] == []
    assert len(result["season_averages"]) == 1


def test_service_returns_no_logs_when_opponent_filter_resolves_empty(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)

    def empty_filter_teams(team_filter, rank, date_filter=None):
        return []

    monkeypatch.setattr(service, "filter_teams", empty_filter_teams)

    query = GameLogQuery(
        season_filter="2024-25",
        teams_against=["OPP_PTS"],
        rank_filter=[1],
    )

    result = service.get_filtered_logs("LeBron James", query)

    assert result["game_logs"] == []
    assert result["averages"] == []


def test_service_surfaces_provider_timeout(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)

    def timed_out(player_name, season):
        raise requests.exceptions.ReadTimeout("stats.nba.com timed out")

    monkeypatch.setattr(service, "_get_game_logs", timed_out)

    with pytest.raises(requests.exceptions.ReadTimeout):
        service.get_filtered_logs(
            "LeBron James", GameLogQuery(season_filter="2024-25")
        )


def test_filter_pipeline_applies_location_and_self_filters(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    frame = _game_logs_frame()

    filtered = service.apply_filters(
        frame,
        GameLogQuery(
            season_filter="2024-25",
            location_filter="Away",
            self_filters={"PTS": "14,16"},
        ),
    )

    assert len(filtered) == 1
    assert filtered.iloc[0]["MATCHUP"] == "BOS @ MIA"


def test_apply_filters_with_no_opponent_query_keeps_all_games(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    frame = _game_logs_frame()

    filtered = service.apply_filters(
        frame,
        GameLogQuery(season_filter="2024-25", minutes_filter="0,48"),
        teams_against=None,
    )

    assert len(filtered) == len(frame)


def test_apply_filters_with_empty_resolved_opponent_set_matches_zero_games(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    frame = _game_logs_frame()

    filtered = service.apply_filters(
        frame,
        GameLogQuery(season_filter="2024-25", minutes_filter="0,48"),
        teams_against=set(),
    )

    assert filtered.empty


# --------------------------------------------------------- route-level contract


def test_route_returns_arrays_from_typed_service(client, monkeypatch):
    from app.routes import game_routes

    captured = {}

    def fake_get_filtered_logs(player_name, query):
        assert isinstance(query, GameLogQuery)
        captured["player_name"] = player_name
        captured["season"] = query.season_filter
        return {
            "game_logs": [{"GAME_DATE": "2024-01-15", "PTS": 26}],
            "averages": [{"PTS": 26.0}],
            "season_averages": [],
            "next_game": None,
        }

    _stub_route_settings(monkeypatch)
    monkeypatch.setattr(
        game_routes.game_service, "get_filtered_logs", fake_get_filtered_logs
    )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2024-25"
        "&minutes_filter=25,48"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body["game_logs"], list)
    assert body["game_logs"] == [{"GAME_DATE": "2024-01-15", "PTS": 26}]
    assert body["averages"] == [{"PTS": 26.0}]
    assert body["season_averages"] == []
    assert captured["player_name"] == "LeBron James"
    assert captured["season"] == "2024-25"


@pytest.mark.parametrize(
    "query_string",
    [
        "player_name=LeBron%20James&minutes_filter=not-a-range",
        "player_name=LeBron%20James&location_filter=home",
        "player_name=LeBron%20James&teams_against[]=OPP_PTS",
        "player_name=LeBron%20James&date_filter=not-a-date",
        "player_name=LeBron%20James&game_filter=0",
    ],
)
def test_route_returns_400_for_malformed_filters(client, monkeypatch, query_string):
    _stub_route_settings(monkeypatch)

    response = client.get(f"/api/games/game_logs?{query_string}")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "One or more game log filters are invalid.",
        }
    }


def test_route_returns_400_when_player_name_missing(client, monkeypatch):
    _stub_route_settings(monkeypatch)

    response = client.get("/api/games/game_logs")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "player_name is required.",
        }
    }
