"""Application and route smoke tests."""

import requests


def test_app_factory_registers_expected_routes(app):
    routes = {str(rule) for rule in app.url_map.iter_rules()}

    assert "/api/health/db" in routes
    assert "/api/health/detailed" in routes
    assert "/api/health/nba-api" in routes
    assert "/api/health/pbp-api" in routes
    assert "/api/players" in routes
    assert "/api/players/profile" in routes
    assert "/api/teams" in routes
    assert "/api/teams/stats" in routes
    assert "/api/games/game_logs" in routes
    assert "/api/nl-query" in routes
    assert "/api/players/test" not in routes
    assert "/api/user/debug/all" not in routes


def test_database_healthcheck(client):
    response = client.get("/api/health/db")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_nba_api_health_classifies_non_2xx_as_provider_http_error(
    client, dependencies, monkeypatch
):
    from app.services import nba_stats_adapter
    from app.providers.nba_stats import NBAStatsAdapter
    from app.services.provider_health_service import ProviderHealthService
    from app.utils import telemetry

    class FailingResponse:
        status_code = 503

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("503 Service Unavailable")

    monkeypatch.setattr(
        nba_stats_adapter.endpoints,
        "LeagueDashTeamStats",
        lambda *args, **kwargs: FailingEndpoint(),
    )

    class FailingEndpoint:
        nba_response = FailingResponse()

        def get_data_frames(self):
            raise AssertionError("health probe must fail on status before parsing")

    dependencies.provider_health_service = ProviderHealthService(
        dependencies.engine,
        settings=dependencies.settings,
        nba_stats=NBAStatsAdapter(settings=dependencies.settings),
        pbp_stats=dependencies.pbp_stats_provider,
    )

    telemetry.clear_recorded_provider_events()

    response = client.get("/api/health/nba-api")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"

    events = telemetry.get_recorded_provider_events()
    assert events
    assert events[-1]["provider"] == telemetry.PROVIDER_NBA_STATS
    assert events[-1]["operation"] == "health_probe"
    assert events[-1]["status_code"] == 503
    assert events[-1]["outcome"] == telemetry.OUTCOME_HTTP_ERROR


def test_pbp_health_has_distinct_provider_signal(client, dependencies, monkeypatch):
    from app.services import provider_health_service
    from app.providers.nba_stats import NBAStatsAdapter
    from app.services.pbp_stats_adapter import PBPTotalsAdapter
    from app.services.provider_health_service import ProviderHealthService
    from app.utils import telemetry

    class FailingResponse:
        status_code = 502

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("502 Service Unavailable")

    class FailingSession:
        def get(self, *args, **kwargs):
            return FailingResponse()

    monkeypatch.setattr(
        provider_health_service,
        "get_shared_nba_session",
        lambda settings: FailingSession(),
    )
    dependencies.provider_health_service = ProviderHealthService(
        dependencies.engine,
        settings=dependencies.settings,
        nba_stats=NBAStatsAdapter(settings=dependencies.settings),
        pbp_stats=PBPTotalsAdapter(
            settings=dependencies.settings,
            session=FailingSession(),
        ),
    )
    telemetry.clear_recorded_provider_events()

    response = client.get("/api/health/pbp-api")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"
    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == telemetry.PROVIDER_PBP_STATS
    assert event["operation"] == "health_probe"
    assert event["status_code"] == 502
    assert event["outcome"] == telemetry.OUTCOME_HTTP_ERROR


def test_detailed_health_reports_both_providers(client, monkeypatch):
    from app.routes import health_routes

    with client.application.app_context():
        monkeypatch.setattr(
            health_routes.health_service,
            "detailed",
            lambda: {
                "status": "healthy",
                "checks": {
                    "database": {"status": "healthy"},
                    "nba_api": {"status": "healthy", "provider": "nba_stats"},
                    "pbp_stats": {"status": "healthy", "provider": "pbp_stats"},
                },
            },
        )

    response = client.get("/api/health/detailed")

    assert response.status_code == 200
    checks = response.get_json()["checks"]
    assert checks["nba_api"]["provider"] == "nba_stats"
    assert checks["pbp_stats"]["provider"] == "pbp_stats"


def test_players_endpoint_smoke(client):
    response = client.get("/api/players")

    assert response.status_code == 200
    assert isinstance(response.get_json(), list)


def test_player_routes_preserve_profile_response_shapes(client):
    service = client.application.extensions["dependencies"].player_service
    service.get_all_players.return_value = ["Jayson Tatum"]

    def profile(player_name, category, opp_team=None):
        assert player_name == "Jayson Tatum"
        if category == "Playtypes":
            return {
                "PLAYER_NAME": "Jayson Tatum",
                "TEAM_ABBREVIATION": "BOS",
                "Transition%": 20.0,
            }
        return [{"Name": "Jayson Tatum", "ThreePtAssists": 30.0}]

    service.get_player_profile.side_effect = profile

    players = client.get("/api/players")
    playtypes = client.get(
        "/api/players/profile?player_name=Jayson%20Tatum&category=Playtypes"
    )
    assists = client.get(
        "/api/players/profile?player_name=Jayson%20Tatum&category=assists"
    )

    assert players.status_code == 200
    assert players.get_json() == ["Jayson Tatum"]
    assert playtypes.status_code == 200
    assert playtypes.get_json()["Transition%"] == 20.0
    assert assists.status_code == 200
    assert assists.get_json()[0]["ThreePtAssists"] == 30.0


def test_game_logs_endpoint_can_be_exercised_with_mocked_service(client, monkeypatch):
    import app.utils.auth as auth
    from app.models.game_logs import GameLogQuery
    from app.routes import game_routes

    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    captured = {}

    def fake_get_filtered_logs(player_name, query):
        assert isinstance(query, GameLogQuery)
        captured["player_name"] = player_name
        captured["season_filter"] = query.season_filter
        return {
            "game_logs": [],
            "averages": [],
            "season_averages": [],
            "next_game": None,
        }

    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service,
            "get_filtered_logs",
            fake_get_filtered_logs,
        )

    response = client.get("/api/games/game_logs?player_name=LeBron%20James")

    assert response.status_code == 200
    assert captured["player_name"] == "LeBron James"
    assert response.get_json() == {
        "game_logs": [],
        "averages": [],
        "season_averages": [],
        "next_game": None,
    }


def test_game_logs_returns_service_unavailable_when_nba_stats_times_out(
    client, monkeypatch
):
    import app.utils.auth as auth
    from app.routes import game_routes

    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    def timed_out(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("stats.nba.com timed out")

    with client.application.app_context():
        monkeypatch.setattr(
            game_routes.game_service,
            "get_filtered_logs",
            timed_out,
        )

    response = client.get("/api/games/game_logs?player_name=LeBron%20James")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "The upstream stats provider timed out. Please try again shortly.",
        }
    }


def test_nl_query_endpoint_can_be_exercised_with_mocked_service(client, monkeypatch):
    import app.utils.auth as auth
    from app.routes import nl_routes

    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)
    with client.application.app_context():
        monkeypatch.setattr(
            nl_routes.nl_service,
            "process_query",
            lambda query: {"query": query, "parsed_by": "test"},
        )

    response = client.post("/api/nl-query", json={"query": "LeBron last 10 games"})

    assert response.status_code == 200
    assert response.get_json() == {
        "query": "LeBron last 10 games",
        "parsed_by": "test",
    }
