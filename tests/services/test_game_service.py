"""Offline tests for GameService filtering, table access, and team ranking.

Every test here runs against a temporary SQLite database and mocked game logs
so the suite never contacts stats.nba.com or Redis.
"""

import asyncio

import pandas as pd
import pytest

from app.services.nl_query.parser import SelfFilter


def run(coro):
    """Execute a coroutine from a synchronous test."""
    return asyncio.run(coro)


@pytest.fixture
def game_logs():
    """Four game logs with distinct minutes, dates, venues, and opponents."""
    return pd.DataFrame(
        [
            {
                "GAME_ID": "0001", "GAME_DATE": "2025-01-01", "MATCHUP": "LAL vs. HOU",
                "TEAM_ABBREVIATION": "LAL", "MIN": 20, "PTS": 10, "REB": 4, "AST": 3,
            },
            {
                "GAME_ID": "0002", "GAME_DATE": "2025-01-05", "MATCHUP": "LAL @ GSW",
                "TEAM_ABBREVIATION": "LAL", "MIN": 30, "PTS": 25, "REB": 8, "AST": 7,
            },
            {
                "GAME_ID": "0003", "GAME_DATE": "2025-01-10", "MATCHUP": "LAL vs. GSW",
                "TEAM_ABBREVIATION": "LAL", "MIN": 36, "PTS": 30, "REB": 12, "AST": 11,
            },
            {
                "GAME_ID": "0004", "GAME_DATE": "2025-01-15", "MATCHUP": "LAL @ BOS",
                "TEAM_ABBREVIATION": "LAL", "MIN": 40, "PTS": 40, "REB": 5, "AST": 2,
            },
        ]
    )


@pytest.fixture
def game_engine(tmp_path):
    """Seed the database tables GameService is allowed to read."""
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'games.db'}")

    pd.DataFrame(
        [
            {"id": 2544, "full_name": "LeBron James"},
            {"id": 201939, "full_name": "Stephen Curry"},
        ]
    ).to_sql("player_information", engine, index=False)

    pd.DataFrame(
        [
            {"team": "LAL", "Transition": 1.10, "Isolation": 0.80},
            {"team": "GSW", "Transition": 1.30, "Isolation": 0.95},
            {"team": "BOS", "Transition": 0.90, "Isolation": 1.05},
        ]
    ).to_sql("team_play_types", engine, index=False)

    pd.DataFrame(
        [
            {"Name": "LAL", "TwoPtAssists": 12, "ThreePtAssists": 9},
            {"Name": "GSW", "TwoPtAssists": 18, "ThreePtAssists": 14},
            {"Name": "BOS", "TwoPtAssists": 15, "ThreePtAssists": 11},
        ]
    ).to_sql("processed_team_assists", engine, index=False)

    pd.DataFrame(
        [
            {"TEAM_ABBREVIATION": "LAL", "FG2M": 10},
            {"TEAM_ABBREVIATION": "GSW", "FG2M": 20},
            {"TEAM_ABBREVIATION": "BOS", "FG2M": 15},
        ]
    ).to_sql("less_than_10_ft", engine, index=False)

    return engine


@pytest.fixture
def service(game_engine, monkeypatch):
    """Build a GameService with caching disabled and no Redis connection."""
    from app.services import game_service as game_service_module

    monkeypatch.setattr(game_service_module, "get_redis_client", lambda *args, **kwargs: None)
    built = game_service_module.GameService(game_engine)
    assert built.cache.enabled is False
    return built


# --- table access and the SQL-injection whitelist ---------------------------


def test_fetch_data_rejects_a_table_outside_the_whitelist(service):
    with pytest.raises(ValueError, match="Invalid table name"):
        service._fetch_data_from_table("users; DROP TABLE users")


def test_fetch_data_returns_rows_for_an_allowed_table(service):
    df = service._fetch_data_from_table("team_play_types")

    assert sorted(df["team"].tolist()) == ["BOS", "GSW", "LAL"]


# --- player identity -------------------------------------------------------


def test_get_player_id_resolves_a_close_name(service):
    assert service.get_player_id("LeBron James") == 2544


def test_get_player_id_raises_for_an_unknown_player(service):
    with pytest.raises(ValueError, match="No matching player found"):
        service.get_player_id("Nonexistent Person")


def test_get_team_name_by_id_returns_none_for_an_unknown_id(service):
    assert service._get_team_name_by_id(-1) is None


def test_get_team_name_by_id_resolves_a_real_team(service):
    lakers_id = next(
        team["id"] for team in service.all_teams if team["abbreviation"] == "LAL"
    )

    assert service._get_team_name_by_id(lakers_id) == "Los Angeles Lakers"


# --- apply_filters ---------------------------------------------------------


def test_minutes_filter_is_inclusive_on_both_bounds(service, game_logs):
    result = run(service.apply_filters(game_logs, {"minutes_filter": [30, 40]}))

    assert result["GAME_ID"].tolist() == ["0002", "0003", "0004"]


def test_date_filter_keeps_games_on_or_after_the_date(service, game_logs):
    result = run(service.apply_filters(game_logs, {"date_filter": "2025-01-10"}))

    assert result["GAME_ID"].tolist() == ["0003", "0004"]


def test_home_location_filter_excludes_away_games(service, game_logs):
    result = run(service.apply_filters(game_logs, {"location_filter": "Home"}))

    assert result["GAME_ID"].tolist() == ["0001", "0003"]


def test_away_location_filter_keeps_only_away_games(service, game_logs):
    result = run(service.apply_filters(game_logs, {"location_filter": "Away"}))

    assert result["GAME_ID"].tolist() == ["0002", "0004"]


def test_both_location_filter_keeps_every_game(service, game_logs):
    result = run(service.apply_filters(game_logs, {"location_filter": "Both"}))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


def test_teams_against_filter_matches_the_opponent_in_the_matchup(service, game_logs):
    result = run(service.apply_filters(game_logs, {"teams_against": {"GSW"}}))

    assert result["GAME_ID"].tolist() == ["0002", "0003"]


def test_empty_teams_against_filter_keeps_every_game(service, game_logs):
    result = run(service.apply_filters(game_logs, {"teams_against": set()}))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


@pytest.mark.parametrize(
    ("operator", "value", "value2", "expected"),
    [
        ("gte", 25, None, ["0002", "0003", "0004"]),
        ("gt", 25, None, ["0003", "0004"]),
        ("lt", 25, None, ["0001"]),
        ("lte", 25, None, ["0001", "0002"]),
        ("eq", 30, None, ["0003"]),
        ("between", 10, 25, ["0001", "0002"]),
    ],
)
def test_self_filter_operators_select_the_right_games(
    service, game_logs, operator, value, value2, expected
):
    self_filter = SelfFilter(
        stat_column="PTS", operator=operator, value=value, value2=value2
    )

    result = run(service.apply_filters(game_logs, {"self_filters": [self_filter]}))

    assert result["GAME_ID"].tolist() == expected


def test_self_filter_for_a_missing_column_is_ignored(service, game_logs):
    self_filter = SelfFilter(stat_column="NOT_A_COLUMN", operator="gte", value=1)

    result = run(service.apply_filters(game_logs, {"self_filters": [self_filter]}))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


def test_multiple_self_filters_are_combined(service, game_logs):
    filters = [
        SelfFilter(stat_column="PTS", operator="gte", value=25),
        SelfFilter(stat_column="AST", operator="gte", value=7),
    ]

    result = run(service.apply_filters(game_logs, {"self_filters": filters}))

    assert result["GAME_ID"].tolist() == ["0002", "0003"]


def test_game_filter_keeps_only_the_leading_games(service, game_logs):
    result = run(service.apply_filters(game_logs, {"game_filter": 2}))

    assert result["GAME_ID"].tolist() == ["0001", "0002"]


def test_non_numeric_game_filter_is_ignored(service, game_logs):
    result = run(service.apply_filters(game_logs, {"game_filter": "not-a-number"}))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


def test_filters_compose_across_minutes_location_and_stats(service, game_logs):
    filter_params = {
        "minutes_filter": [30, 48],
        "location_filter": "Home",
        "self_filters": [SelfFilter(stat_column="REB", operator="gte", value=10)],
    }

    result = run(service.apply_filters(game_logs, filter_params))

    assert result["GAME_ID"].tolist() == ["0003"]


# --- provider response processing ------------------------------------------


def _api_game_log_row(**overrides):
    """One PlayerGameLogs row carrying every column the processing reads."""
    row = {
        "SEASON_YEAR": "2025-26", "PLAYER_ID": 2544, "PLAYER_NAME": "LeBron James",
        "TEAM_ID": 1610612747, "TEAM_ABBREVIATION": "LAL", "TEAM_NAME": "Lakers",
        "NICKNAME": "LeBron", "GAME_ID": "0001", "GAME_DATE": "2025-01-01",
        "MATCHUP": "LAL vs. HOU", "WL": "W", "MIN": 34.6, "FGM": 10, "FGA": 20,
        "FG_PCT": 0.5, "FG3M": 3, "FG3A": 8, "FG3_PCT": 0.375, "FTM": 5, "FTA": 6,
        "FT_PCT": 0.833, "OREB": 1, "DREB": 7, "REB": 8, "AST": 9, "TOV": 3,
        "STL": 2, "BLK": 1, "BLKA": 1, "PF": 2, "PFD": 3, "PTS": 28,
        "PLUS_MINUS": 12, "NBA_FANTASY_PTS": 52.3, "DD2": 1, "TD3": 0,
        "WNBA_FANTASY_PTS": 0, "AVAILABLE_FLAG": 1,
    }
    row.update({f"{stat}_RANK": 1 for stat in [
        "GP", "W", "L", "W_PCT", "MIN", "FGM", "FGA", "FG_PCT", "FG3M", "FG3A",
        "FG3_PCT", "FTM", "FTA", "FT_PCT", "OREB", "DREB", "REB", "AST", "TOV",
        "STL", "BLK", "BLKA", "PF", "PFD", "PTS", "PLUS_MINUS",
        "NBA_FANTASY_PTS", "DD2", "TD3", "WNBA_FANTASY_PTS",
    ]})
    row.update(overrides)
    return row


def _stub_provider(service, monkeypatch, frame):
    """Replace the PlayerGameLogs endpoint with a fixed response."""
    from app.services import game_service as game_service_module

    class FakeEndpoint:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_data_frames(self):
            return [frame]

    monkeypatch.setattr(
        game_service_module.endpoints.playergamelogs,
        "PlayerGameLogs",
        FakeEndpoint,
    )
    monkeypatch.setattr(service, "get_player_id", lambda name: 2544)


def test_provider_response_is_reduced_to_derived_stats(service, monkeypatch):
    import pandas as pd

    _stub_provider(service, monkeypatch, pd.DataFrame([_api_game_log_row()]))

    logs, next_team = run(service._fetch_game_logs_from_api("LeBron James"))

    row = logs.iloc[0]
    assert row["PRA"] == 28 + 8 + 9
    assert row["PA"] == 28 + 9
    assert row["PR"] == 28 + 8
    assert row["RA"] == 8 + 9
    assert row["STKS"] == 2 + 1
    assert row["FG2M"] == 10 - 3
    assert row["FG2A"] == 20 - 8
    assert row["MIN"] == 35  # rounded to a whole minute
    assert next_team is None
    # Discarded columns must be gone.
    assert "PTS_RANK" not in logs.columns
    assert "NICKNAME" not in logs.columns


def test_processing_survives_a_provider_dropping_an_optional_column(
    service, monkeypatch
):
    """nba_api's declared schema omits several columns we discard.

    If the provider stops returning one, discarding it must not fail the
    request, so the drop is applied with errors='ignore'.
    """
    import pandas as pd

    row = _api_game_log_row()
    for absent in ("NICKNAME", "AVAILABLE_FLAG", "WNBA_FANTASY_PTS"):
        del row[absent]
    _stub_provider(service, monkeypatch, pd.DataFrame([row]))

    logs, _ = run(service._fetch_game_logs_from_api("LeBron James"))

    assert logs.iloc[0]["PRA"] == 45


# --- players on / off ------------------------------------------------------


def _stub_game_logs(service, monkeypatch, logs_by_player):
    """Replace the cached NBA API fetch with a fixed per-player mapping."""

    async def fake_get_game_logs(player_name, season="2025-26"):
        return logs_by_player[player_name], None

    monkeypatch.setattr(service, "_get_game_logs", fake_get_game_logs)


def test_players_on_keeps_only_games_both_players_appeared_in(
    service, game_logs, monkeypatch
):
    teammate = game_logs[game_logs["GAME_ID"].isin(["0002", "0003"])]
    _stub_game_logs(service, monkeypatch, {"Teammate": teammate})

    result = run(service.filter_players_on_off(game_logs, ["Teammate"], [], "2025-26"))

    assert result["GAME_ID"].tolist() == ["0002", "0003"]


def test_players_off_excludes_games_the_other_player_appeared_in(
    service, game_logs, monkeypatch
):
    teammate = game_logs[game_logs["GAME_ID"].isin(["0002", "0003"])]
    _stub_game_logs(service, monkeypatch, {"Teammate": teammate})

    result = run(service.filter_players_on_off(game_logs, [], ["Teammate"], "2025-26"))

    assert result["GAME_ID"].tolist() == ["0001", "0004"]


def test_common_games_requires_every_named_player(service, game_logs, monkeypatch):
    _stub_game_logs(
        service,
        monkeypatch,
        {
            "A": game_logs[game_logs["GAME_ID"].isin(["0002", "0003"])],
            "B": game_logs[game_logs["GAME_ID"].isin(["0003", "0004"])],
        },
    )

    common = run(service.get_common_games(game_logs, ["A", "B"], "2025-26"))

    assert common == {"0003"}


def test_games_to_exclude_unions_every_named_player(service, game_logs, monkeypatch):
    _stub_game_logs(
        service,
        monkeypatch,
        {
            "A": game_logs[game_logs["GAME_ID"] == "0001"],
            "B": game_logs[game_logs["GAME_ID"] == "0004"],
        },
    )

    excluded = run(service.get_games_to_exclude(game_logs, ["A", "B"], "2025-26"))

    assert excluded == {"0001", "0004"}


# --- team ranking ----------------------------------------------------------


def test_playtype_ranking_returns_the_best_teams_first(service):
    assert run(service._filter_teams_uncached("Transition", 2)) == ["GSW", "LAL"]


def test_negative_rank_filter_returns_the_worst_teams(service):
    assert run(service._filter_teams_uncached("Transition", -2)) == ["LAL", "BOS"]


def test_assist_ranking_uses_the_team_name_column(service):
    assert run(service._filter_teams_uncached("TwoPtAssists", 2)) == ["GSW", "BOS"]


def test_short_range_ranking_uses_the_team_abbreviation(service):
    assert run(service._filter_teams_uncached("Less Than 10 ft", 1)) == ["GSW"]
