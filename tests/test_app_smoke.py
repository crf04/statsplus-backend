"""Application and route smoke tests."""

import requests

from tests.conftest import SEEDED_PLAYER_NAMES


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


def test_database_healthcheck_reports_ok_for_a_reachable_database(
    make_client, seeded_db_url
):
    response = make_client(seeded_db_url).get("/api/health/db")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["dialect"] == "sqlite"


def test_database_healthcheck_reports_error_for_an_unreachable_database(make_client):
    unreachable = make_client("sqlite:////nonexistent-directory/missing.db")

    response = unreachable.get("/api/health/db")

    assert response.status_code == 500
    error = response.get_json()["error"]
    assert error["code"] == "internal_error"
    # The sanitized envelope must not leak driver or filesystem detail.
    assert "sqlite3" not in error["message"]


def test_players_endpoint_returns_the_names_stored_in_the_database(
    make_client, seeded_db_url
):
    response = make_client(seeded_db_url).get("/api/players")

    assert response.status_code == 200
    assert response.get_json() == SEEDED_PLAYER_NAMES


def test_players_endpoint_reports_failure_when_the_table_is_missing(
    make_client, empty_db_url
):
    response = make_client(empty_db_url).get("/api/players")

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "operation_failed"


def test_game_logs_endpoint_can_be_exercised_with_mocked_service(client, monkeypatch):
    from flask import jsonify

    import app.utils.auth as auth
    from app.routes import game_routes

    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    async def fake_get_filtered_logs(player_name, filter_params):
        return jsonify({
            "player_name": player_name,
            "season_filter": filter_params["season_filter"],
        })

    monkeypatch.setattr(game_routes.game_service, "get_filtered_logs", fake_get_filtered_logs)

    response = client.get("/api/games/game_logs?player_name=LeBron%20James")

    assert response.status_code == 200
    assert response.get_json() == {
        "player_name": "LeBron James",
        "season_filter": "2025-26",
    }


def test_game_logs_returns_service_unavailable_when_nba_stats_times_out(
    client, monkeypatch
):
    import app.utils.auth as auth
    from app.routes import game_routes

    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    async def timed_out(*args, **kwargs):
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
