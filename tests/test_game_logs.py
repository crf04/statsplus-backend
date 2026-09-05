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
    CacheSettings,
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
    self_filter = query.self_filters[0]
    assert self_filter.stat == "PTS"
    assert self_filter.operator == "between"
    assert self_filter.value == 25.0
    assert self_filter.value2 == 60.0


def test_game_log_query_trims_and_validates_canonical_season():
    query = GameLogQuery(season_filter=" 2024-25 ")

    assert query.season_filter == "2024-25"


def test_game_log_query_normalizes_one_specific_opponent_tricode():
    query = GameLogQuery(season_filter="2024-25", opponent_filter=" okc ")

    assert query.opponent_filter == "OKC"


def test_game_log_query_defaults_to_no_specific_opponent():
    assert GameLogQuery(season_filter="2024-25").opponent_filter is None


@pytest.mark.parametrize("season", ["", "potato", "2024-27"])
def test_game_log_query_rejects_invalid_season(season):
    with pytest.raises(Exception):
        GameLogQuery(season_filter=season)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_game_log_query_rejects_nonfinite_playstyle_range(value):
    with pytest.raises(Exception):
        GameLogQuery(season_filter="2024-25", playstyle_range=(value, 200))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes_filter": "25"},
        {"minutes_filter": "20,10"},
        {"location_filter": "everywhere"},
        {"date_filter": "not-a-date"},
        {"teams_against": ["OPP_PTS"], "rank_filter": []},
        {"teams_against": ["OPP_PTS"], "rank_filter": ["3", "9"]},
        {"opponent_filter": "XXX"},
        {"opponent_filter": ""},
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
        # The explicit #66 contract amendment removes plus-minus from the
        # game-log self-filter vocabulary.
        {"self_filters": {"PLUS_MINUS": "1,10"}},
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
    assert "PLUS_MINUS" not in SUPPORTED_SELF_FILTER_STATS


@pytest.mark.parametrize(
    "stat",
    [
        "MIN", "FG_PCT", "OREB", "DREB", "PF", "PRA",
        "PA", "PR", "RA", "STKS", "FD_PTS",
    ],
)
def test_documented_self_filter_stats_are_accepted(stat):
    query = GameLogQuery(
        season_filter="2024-25",
        self_filters={stat: "0,100"},
    )

    assert any(self_filter.stat == stat for self_filter in query.self_filters)


@pytest.mark.parametrize(
    ("operator", "value", "value2", "expected_points"),
    [
        ("gte", 25, None, {25, 30}),
        ("gt", 25, None, {30}),
        ("lt", 25, None, {15}),
        ("lte", 25, None, {15, 25}),
        ("eq", 25, None, {25}),
        ("between", 20, 30, {25, 30}),
    ],
)
def test_typed_self_filter_operators_are_applied_exactly(
    monkeypatch,
    mock_db_engine,
    mock_redis_client,
    operator,
    value,
    value2,
    expected_points,
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    payload = {
        "stat_column": "PTS",
        "operator": operator,
        "value": value,
    }
    if value2 is not None:
        payload["value2"] = value2
    query = GameLogQuery(season_filter="2024-25", self_filters=[payload])

    filtered = service.apply_filters(_game_logs_frame(), query)

    assert set(filtered["PTS"]) == expected_points


def test_game_query_normalizes_legacy_nlp_self_filter_object():
    from app.services.nl_query.parser import SelfFilter as ParsedSelfFilter

    query = GameLogQuery(
        season_filter="2024-25",
        self_filters=[
            ParsedSelfFilter(
                stat_column="PTS",
                operator="lte",
                value=25,
            )
        ],
    )

    self_filter = query.self_filters[0]
    assert self_filter.stat == "PTS"
    assert self_filter.operator == "lte"
    assert self_filter.value == 25


def test_repeated_same_stat_filters_survive_legacy_normalization_and_filtering(
    monkeypatch, mock_db_engine, mock_redis_client
):
    """Conjunctions retain both bounds when they target the same stat."""

    from app.services.nl_query.parser import SelfFilter as ParsedSelfFilter

    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    query = GameLogQuery(
        season_filter="2024-25",
        self_filters=[
            ParsedSelfFilter(stat_column="PTS", operator="gte", value=20),
            ParsedSelfFilter(stat_column="PTS", operator="lt", value=30),
        ],
    )

    assert [(item.stat, item.operator.value, item.value) for item in query.self_filters] == [
        ("PTS", "gte", 20.0),
        ("PTS", "lt", 30.0),
    ]
    filtered = service.apply_filters(_game_logs_frame(), query)
    assert set(filtered["PTS"]) == {25}


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
        "MIN_SEC",
    ]
    rows = [
        ["2024-01-15", "001", "LeBron James", 1610612738, "BOS", "BOS vs. LAL",
         30, 25, 8, 7, 40, 32, 33, 15, 2, 44.2, 44.2, 9, 16, 56.3, 6, 9,
         3, 7, 4, 5, 1, 7, 2, 1, 1, 2, 1800],
        ["2024-01-17", "002", "LeBron James", 1610612738, "BOS", "BOS @ MIA",
         20, 15, 4, 5, 24, 20, 19, 9, 0, 27.0, 27.0, 6, 12, 50.0, 4, 6,
         2, 6, 1, 2, 0, 4, 3, 0, 0, 3, 1200],
        ["2024-01-19", "003", "LeBron James", 1610612738, "BOS", "BOS vs. CHI",
         40, 30, 10, 9, 49, 39, 40, 19, 3, 52.0, 52.0, 11, 20, 55.0, 7, 11,
         4, 9, 4, 4, 2, 8, 1, 2, 1, 1, 2400],
    ]
    return pd.DataFrame(rows, columns=columns)


class _StubRankings:
    """Season Rankings that record every filter they were asked to rank."""

    def __init__(self, ranked):
        self.ranked = list(ranked)
        self.calls = []

    def rank_all(self, team_filters, season):
        self.calls.append((tuple(team_filters), season))
        return {team_filter: list(self.ranked) for team_filter in team_filters}


def _make_service(monkeypatch, mock_db_engine, mock_redis_client):
    settings = RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings(current_season="2024-25"),
    )
    service = GameService(
        mock_db_engine,
        mock_redis_client,
        settings=settings,
        team_filter_rankings=_StubRankings(["LAL"]),
    )

    def fake_logs(player_name, season):
        return _game_logs_frame(), None

    monkeypatch.setattr(service, "_get_game_logs", fake_logs)
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


def test_get_filtered_logs_resolves_a_player_only_present_in_the_athlete_catalog(
    mock_db_engine, mock_redis_client
):
    """A 2025 draft-class rookie has no player_information row (that table is
    never refreshed in production) but is durable in the season Athlete
    Catalog and has logs; the request must not 404 (see the Cooper Flagg
    production case)."""

    class _CatalogOnlyRookie:
        def get_catalog(self, season, *, active_only=False):
            if season != "2025-26":
                return []
            return [{"player_id": 1642843, "display_name": "Cooper Flagg", "is_active_for_season": True}]

    class _RookieLogsSource:
        def get_player_logs(self, player_id, season):
            assert player_id == 1642843
            assert season == "2025-26"
            frame = _game_logs_frame().copy()
            frame["PLAYER_NAME"] = "Cooper Flagg"
            return frame

    service = GameService(
        mock_db_engine,
        mock_redis_client,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2025-26"),
        ),
        game_logs_source=_RookieLogsSource(),
        team_filter_rankings=_StubRankings(["LAL"]),
        athlete_catalog=_CatalogOnlyRookie(),
    )
    query = GameLogQuery(season_filter="2025-26")

    result = service.get_filtered_logs("Cooper Flagg", query)

    assert len(result["game_logs"]) == 3
    assert {row["PTS"] for row in result["game_logs"]} == {25, 15, 30}
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

    service.team_filter_rankings = _StubRankings([])

    query = GameLogQuery(
        season_filter="2024-25",
        teams_against=["OPP_PTS"],
        rank_filter=[1],
    )

    result = service.get_filtered_logs("LeBron James", query)

    assert result["game_logs"] == []
    assert result["averages"] == []


def test_a_specific_opponent_keeps_only_games_against_that_team(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)
    query = GameLogQuery(season_filter="2024-25", opponent_filter="MIA")

    result = service.get_filtered_logs("LeBron James", query)

    assert [row["MATCHUP"] for row in result["game_logs"]] == ["BOS @ MIA"]
    assert result["averages"][0]["PTS"] == 15
    GameLogResponse.model_validate(result)


def test_a_specific_opponent_narrows_the_rank_based_opponent_filter(
    monkeypatch, mock_db_engine, mock_redis_client
):
    """The two opponent filters compose as a conjunction, never a union."""

    service = _make_service(monkeypatch, mock_db_engine, mock_redis_client)

    def matchups(opponent):
        query = GameLogQuery(
            season_filter="2024-25",
            teams_against=["OPP_PTS"],
            rank_filter=[1],
            opponent_filter=opponent,
        )
        return [
            row["MATCHUP"]
            for row in service.get_filtered_logs("LeBron James", query)["game_logs"]
        ]

    # The stubbed rankings resolve OPP_PTS rank 1 to LAL.
    assert matchups("LAL") == ["BOS vs. LAL"]
    assert matchups("MIA") == []


def _ranked_service(monkeypatch, mock_db_engine, mock_redis_client, ranked):
    """Build a service whose Team Filters rank from Season publications."""

    service = GameService(
        mock_db_engine,
        mock_redis_client,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2024-25"),
        ),
        team_filter_rankings=_StubRankings(ranked),
    )
    monkeypatch.setattr(
        service, "_get_game_logs", lambda name, season: (_game_logs_frame(), None)
    )
    return service


def test_a_date_filter_trims_the_logs_while_rankings_stay_season_wide(
    monkeypatch, mock_db_engine, mock_redis_client
):
    """A date-plus-Team-Filter request stays valid and season-ranked."""

    service = _ranked_service(
        monkeypatch, mock_db_engine, mock_redis_client, ["MIA", "LAL", "CHI"]
    )
    query = GameLogQuery(
        season_filter="2024-25",
        date_filter="2024-01-16",
        teams_against=["OPP_PTS"],
        rank_filter=[2],
    )

    result = service.get_filtered_logs("LeBron James", query)

    # MIA and LAL rank; the date keeps only the later of the two games.
    assert [row["MATCHUP"] for row in result["game_logs"]] == ["BOS @ MIA"]
    assert service.team_filter_rankings.calls == [(("OPP_PTS",), "2024-25")]


def test_the_same_team_filter_ranks_identically_with_and_without_a_date(
    monkeypatch, mock_db_engine, mock_redis_client
):
    service = _ranked_service(
        monkeypatch, mock_db_engine, mock_redis_client, ["MIA", "LAL", "CHI"]
    )

    def matchups(**filters):
        query = GameLogQuery(
            season_filter="2024-25",
            teams_against=["OPP_PTS"],
            rank_filter=[1],
            **filters,
        )
        return [
            row["MATCHUP"]
            for row in service.get_filtered_logs("LeBron James", query)["game_logs"]
        ]

    assert matchups() == ["BOS @ MIA"]
    assert matchups(date_filter="2024-01-01") == ["BOS @ MIA"]


def test_a_historical_season_never_borrows_current_season_rankings(
    monkeypatch, mock_db_engine, mock_redis_client
):
    """The requested season is the season whose rankings are read."""

    service = _ranked_service(
        monkeypatch, mock_db_engine, mock_redis_client, ["MIA", "LAL", "CHI"]
    )
    query = GameLogQuery(
        season_filter="2023-24",
        teams_against=["OPP_PTS"],
        rank_filter=[1],
    )

    service.get_filtered_logs("LeBron James", query)

    assert service.team_filter_rankings.calls == [(("OPP_PTS",), "2023-24")]


def test_route_serves_a_legacy_date_plus_team_filter_url_unchanged(
    client, dependencies, monkeypatch, mock_db_engine, mock_redis_client
):
    """The wire contract for a previously valid Filter Set URL is unchanged."""

    from app.routes import game_routes

    service = _ranked_service(
        monkeypatch, mock_db_engine, mock_redis_client, ["LAL", "MIA", "CHI"]
    )
    dependencies.game_service = service
    _stub_route_settings(monkeypatch)
    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service,
            "get_filtered_logs",
            lambda player_name, query: GameService.get_filtered_logs(
                service, player_name, query
            ),
        )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2024-25"
        "&date_filter=2024-01-01&teams_against[]=OPP_PTS&rank_filter[]=1"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [row["MATCHUP"] for row in body["game_logs"]] == ["BOS vs. LAL"]
    assert body["next_game"] is None
    GameLogResponse.model_validate(body)


def test_route_returns_empty_when_player_log_publication_is_unavailable(
    client, dependencies, monkeypatch, mock_db_engine, mock_redis_client
):
    """An unavailable durable publication is a successful no-result query."""

    from app.routes import game_routes
    from app.services.game_logs_source import StoredGameLogsSource

    class UnavailablePublication:
        legacy_fallback_allowed = False
        available = False

    class UnavailableSnapshot:
        def read(self, stream_key):
            assert stream_key == "player_game_logs"
            return UnavailablePublication()

    class UnavailableRepository:
        def read_publication_snapshot(self, season):
            assert season == "2024-25"
            return UnavailableSnapshot()

        def list_player_rows(self, *args, **kwargs):
            raise AssertionError("an unavailable publication must not be read")

    service = GameService(
        mock_db_engine,
        mock_redis_client,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2024-25"),
        ),
        game_logs_source=StoredGameLogsSource(UnavailableRepository()),
    )
    dependencies.game_service = service
    _stub_route_settings(monkeypatch)
    with client.application.app_context():
        service = game_routes.game_service._resolve()
        monkeypatch.setattr(service, "get_player_id", lambda name, season: 1)
        monkeypatch.setattr(
            game_routes.game_service,
            "get_filtered_logs",
            lambda player_name, query: GameService.get_filtered_logs(
                service, player_name, query
            ),
        )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2024-25"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["game_logs"] == []
    assert body["averages"] == []
    assert body["season_averages"] == []


def test_game_service_never_caches_player_logs_in_redis(
    monkeypatch, mock_db_engine
):
    class RedisGuard:
        def __getattr__(self, name):
            raise AssertionError(f"player game logs reached Redis through {name}")

    class ChangingDurableSource:
        def __init__(self):
            self.calls = 0

        def get_player_logs(self, player_id, season):
            assert player_id == 1
            assert season == "2024-25"
            self.calls += 1
            frame = _game_logs_frame().head(1).copy()
            frame.loc[:, "PTS"] = self.calls
            return frame

    source = ChangingDurableSource()
    service = GameService(
        mock_db_engine,
        RedisGuard(),
        settings=RuntimeSettings(
            environment="testing",
            cache=CacheSettings(enabled=True),
            nba=NBASeasonSettings(current_season="2024-25"),
        ),
        game_logs_source=source,
    )
    monkeypatch.setattr(service, "get_player_id", lambda _name, _season: 1)

    first, _ = service._get_game_logs("LeBron James", "2024-25")
    second, _ = service._get_game_logs("LeBron James", "2024-25")

    assert source.calls == 2
    assert first.iloc[0]["PTS"] == 1
    assert second.iloc[0]["PTS"] == 2


def test_malformed_provider_response_is_safe_503_without_app_failure(
    client, monkeypatch
):
    from app.routes import game_routes
    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()

    def malformed(*args, **kwargs):
        with telemetry.provider_call(
            telemetry.PROVIDER_NBA_STATS, "player_game_logs"
        ):
            raise telemetry.ProviderResponseError(
                "malformed provider secret=do-not-return"
            )

    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service,
            "get_filtered_logs",
            malformed,
        )

    response = client.get("/api/games/game_logs?player_name=LeBron%20James")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "An upstream provider is currently unavailable. Please try again later.",
        }
    }
    metrics = telemetry.snapshot_metrics()
    assert metrics["provider_failures"][telemetry.PROVIDER_NBA_STATS][
        telemetry.OUTCOME_MALFORMED
    ] == 1
    assert metrics["application_failures"] == {}
    assert "do-not-return" not in response.get_data(as_text=True)


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


def test_route_preserves_repeated_same_stat_self_filters(client, monkeypatch):
    from app.routes import game_routes

    captured = {}

    def fake_get_filtered_logs(player_name, query):
        captured["query"] = query
        return {
            "game_logs": [],
            "averages": [],
            "season_averages": [],
            "next_game": None,
        }

    _stub_route_settings(monkeypatch)
    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service, "get_filtered_logs", fake_get_filtered_logs
        )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James"
        "&self_filters[PTS]=20,48&self_filters[PTS]=0,30"
    )

    assert response.status_code == 200
    assert [item.stat for item in captured["query"].self_filters] == ["PTS", "PTS"]
    assert [item.value for item in captured["query"].self_filters] == [20.0, 0.0]
    assert [item.value2 for item in captured["query"].self_filters] == [48.0, 30.0]


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
    with client.application.app_context():
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


def test_route_passes_a_specific_opponent_to_the_service(client, monkeypatch):
    from app.routes import game_routes

    captured = {}

    def fake_get_filtered_logs(player_name, query):
        captured["query"] = query
        return {
            "game_logs": [],
            "averages": [],
            "season_averages": [],
            "next_game": None,
        }

    _stub_route_settings(monkeypatch)
    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service, "get_filtered_logs", fake_get_filtered_logs
        )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&opponent_filter=okc"
    )

    assert response.status_code == 200
    assert captured["query"].opponent_filter == "OKC"


@pytest.mark.parametrize(
    "query_string",
    [
        "player_name=LeBron%20James&minutes_filter=not-a-range",
        "player_name=LeBron%20James&location_filter=home",
        "player_name=LeBron%20James&teams_against[]=OPP_PTS",
        "player_name=LeBron%20James&opponent_filter=XXX",
        "player_name=LeBron%20James&opponent_filter=",
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


@pytest.mark.parametrize(
    "query_string",
    [
        "season_filter=",
        "season_filter=potato",
        "season_filter=2024-27",
        "playstyle_RTG_min=nan",
        "playstyle_RTG_max=inf",
        "playstyle_RTG_min=-inf",
    ],
)
def test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service(
    client, monkeypatch, query_string
):
    from app.routes import game_routes

    _stub_route_settings(monkeypatch)
    calls = []

    def service_must_not_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid game-log filters reached the service")

    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service, "get_filtered_logs", service_must_not_run
        )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&" + query_string
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "One or more game log filters are invalid.",
        }
    }
    assert calls == []


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
