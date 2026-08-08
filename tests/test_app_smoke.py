"""Application and route smoke tests."""

import requests


def test_app_factory_registers_expected_routes(app):
    routes = {str(rule) for rule in app.url_map.iter_rules()}

    assert "/api/health/db" in routes
    assert "/api/health/detailed" in routes
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


def test_players_endpoint_smoke(client):
    response = client.get("/api/players")

    assert response.status_code == 200
    assert isinstance(response.get_json(), list)


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

    monkeypatch.setattr(game_routes.game_service, "get_filtered_logs", fake_get_filtered_logs)

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

    monkeypatch.setattr(game_routes.game_service, "get_filtered_logs", timed_out)

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
