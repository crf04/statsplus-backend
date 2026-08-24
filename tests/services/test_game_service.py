"""Offline tests for GameService filtering, table access, and team ranking.

Every test here runs against a temporary SQLite database and mocked game logs
so the suite never contacts stats.nba.com or Redis.
"""

import pandas as pd
import pytest

from app.models.game_logs import GameLogQuery
from app.services.nl_query.parser import SelfFilter


def run(value):
    """Keep the historical test helper while exercising sync service calls."""
    return value


def make_query(**filters):
    """Build the typed query used by the synchronous service contract."""
    filters.setdefault("season_filter", "2025-26")
    return GameLogQuery(**filters)


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

    return engine


class _StubRankings:
    """Stand in for the Season Rankings read with one fixed ranking."""

    def __init__(self, ranked):
        self.ranked = list(ranked)
        self.calls = []

    def ranked_teams(self, team_filter):
        self.calls.append(team_filter)
        return list(self.ranked)


@pytest.fixture
def service(game_engine, monkeypatch):
    """Build a GameService with caching disabled and no Redis connection."""
    from app.services import game_service as game_service_module

    monkeypatch.setattr(game_service_module, "get_redis_client", lambda *args, **kwargs: None)
    built = game_service_module.GameService(
        game_engine,
        team_filter_rankings=_StubRankings(["GSW", "LAL", "BOS"]),
    )
    assert built.cache.enabled is False
    return built


# --- table access and the SQL-injection whitelist ---------------------------


def test_fetch_data_rejects_a_table_outside_the_whitelist(service):
    with pytest.raises(ValueError, match="Invalid table name"):
        service._fetch_data_from_table("users; DROP TABLE users")


def test_fetch_data_returns_rows_for_an_allowed_table(service):
    df = service._fetch_data_from_table("player_information")

    assert sorted(df["full_name"].tolist()) == ["LeBron James", "Stephen Curry"]


def test_fetch_data_rejects_a_retired_legacy_team_filter_table(service):
    """The team-filter tables are no longer reachable from this service."""

    with pytest.raises(ValueError, match="Invalid table name"):
        service._fetch_data_from_table("general_opponent_stats")


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
    result = run(service.apply_filters(game_logs, make_query(minutes_filter=[30, 40])))

    assert result["GAME_ID"].tolist() == ["0002", "0003", "0004"]


def test_date_filter_keeps_games_on_or_after_the_date(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query(date_filter="2025-01-10")))

    assert result["GAME_ID"].tolist() == ["0003", "0004"]


def test_home_location_filter_excludes_away_games(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query(location_filter="Home")))

    assert result["GAME_ID"].tolist() == ["0001", "0003"]


def test_away_location_filter_keeps_only_away_games(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query(location_filter="Away")))

    assert result["GAME_ID"].tolist() == ["0002", "0004"]


def test_both_location_filter_keeps_every_game(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query(location_filter="Both")))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


def test_teams_against_filter_matches_the_opponent_in_the_matchup(service, game_logs):
    result = run(
        service.apply_filters(game_logs, make_query(), teams_against={"GSW"})
    )

    assert result["GAME_ID"].tolist() == ["0002", "0003"]


def test_empty_teams_against_filter_matches_no_games(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query(), teams_against=set()))

    assert result.empty


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

    result = run(service.apply_filters(game_logs, make_query(self_filters=[self_filter])))

    assert result["GAME_ID"].tolist() == expected


def test_self_filter_for_a_missing_column_is_ignored(service, game_logs):
    self_filter = SelfFilter(stat_column="FGM", operator="gte", value=1)

    result = run(service.apply_filters(game_logs, make_query(self_filters=[self_filter])))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


def test_multiple_self_filters_are_combined(service, game_logs):
    filters = [
        SelfFilter(stat_column="PTS", operator="gte", value=25),
        SelfFilter(stat_column="AST", operator="gte", value=7),
    ]

    result = run(service.apply_filters(game_logs, make_query(self_filters=filters)))

    assert result["GAME_ID"].tolist() == ["0002", "0003"]


def test_game_filter_keeps_only_the_leading_games(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query(game_filter=2)))

    assert result["GAME_ID"].tolist() == ["0001", "0002"]


def test_non_numeric_game_filter_is_ignored(service, game_logs):
    result = run(service.apply_filters(game_logs, make_query()))

    assert result["GAME_ID"].tolist() == ["0001", "0002", "0003", "0004"]


def test_filters_compose_across_minutes_location_and_stats(service, game_logs):
    filter_params = make_query(
        minutes_filter=[30, 48],
        location_filter="Home",
        self_filters=[SelfFilter(stat_column="REB", operator="gte", value=10)],
    )

    result = run(service.apply_filters(game_logs, filter_params))

    assert result["GAME_ID"].tolist() == ["0003"]


# --- players on / off ------------------------------------------------------


def _stub_game_logs(service, monkeypatch, logs_by_player):
    """Replace the cached NBA API fetch with a fixed per-player mapping."""

    def fake_get_game_logs(player_name, season="2025-26"):
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


def test_players_off_does_not_exclude_an_opponents_appearance(
    service, game_logs, monkeypatch
):
    opponent = game_logs[game_logs["GAME_ID"] == "0002"].copy()
    opponent["TEAM_ABBREVIATION"] = "OPP"
    _stub_game_logs(service, monkeypatch, {"Opponent": opponent})

    result = run(service.filter_players_on_off(game_logs, [], ["Opponent"], "2025-26"))

    assert result["GAME_ID"].tolist() == game_logs["GAME_ID"].tolist()


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


def test_ranking_returns_the_top_teams_first(service):
    assert run(service.filter_teams("Transition", 2)) == ["GSW", "LAL"]


def test_negative_rank_filter_returns_the_worst_teams(service):
    assert run(service.filter_teams("Transition", -2)) == ["LAL", "BOS"]


def test_a_rank_filter_wider_than_the_league_returns_every_ranked_team(service):
    assert run(service.filter_teams("TwoPtAssists", 30)) == ["GSW", "LAL", "BOS"]
    assert run(service.filter_teams("TwoPtAssists", -30)) == ["GSW", "LAL", "BOS"]


def test_every_team_filter_category_ranks_from_the_same_seam(service):
    for team_filter in ("OPP_PTS", "C&S PTS", "Transition", "Less Than 10 ft",
                        "TwoPtAssists"):
        assert run(service.filter_teams(team_filter, 1)) == ["GSW"]

    assert service.team_filter_rankings.calls == [
        "OPP_PTS", "C&S PTS", "Transition", "Less Than 10 ft", "TwoPtAssists"
    ]


def test_an_unsupported_team_filter_is_rejected(service):
    with pytest.raises(ValueError, match="Unsupported team filter"):
        run(service.filter_teams("NOT_A_FILTER", 1))


def test_rankings_are_unavailable_without_season_publications(game_engine, monkeypatch):
    from app.services import game_service as game_service_module

    monkeypatch.setattr(
        game_service_module, "get_redis_client", lambda *args, **kwargs: None
    )
    service = game_service_module.GameService(game_engine)

    assert run(service.filter_teams("OPP_PTS", 5)) == []
