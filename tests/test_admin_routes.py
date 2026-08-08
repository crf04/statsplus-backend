"""Authorization and HTTP method tests for privileged data refresh routes.

Mutating data routes return ``202 Accepted`` with a durable ``job_id`` and
schedule the actual refresh on the job service.  The job service is stubbed
here so these tests exercise only the HTTP seam without touching a database or
an upstream provider.
"""

from __future__ import annotations

import pytest


ADMIN_TOKEN = "admin-test-token"
USER_TOKEN = "user-test-token"


class FakeJobService:
    """Deterministic job service stub for route-level tests."""

    def __init__(self):
        self.started = []

    def start(self, operation, refresh):
        self.started.append((operation, refresh))
        return {
            "job_id": "job-under-test",
            "operation": operation,
            "status": "queued",
            "progress": 0.0,
            "progress_note": None,
            "created_at": "2025-01-01T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "failure_summary": None,
        }

    def get(self, job_id):
        return {
            "job_id": job_id,
            "operation": "update_database",
            "status": "queued",
            "progress": 0.0,
            "progress_note": None,
            "created_at": "2025-01-01T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "failure_summary": None,
        }


def _set_token_verifier(monkeypatch, claims: dict) -> None:
    """Configure the auth module without requiring Firebase credentials."""
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": token, "email": f"{token}@example.com", **claims},
    )
    monkeypatch.setattr(
        auth.UserService,
        "create_or_update_user",
        lambda self, user_data: None,
    )


def _admin_auth(monkeypatch) -> dict[str, str]:
    _set_token_verifier(
        monkeypatch,
        {
            "admin": True,
            "is_admin": True,
            "role": "admin",
            "roles": ["admin"],
        },
    )
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _user_auth(monkeypatch) -> dict[str, str]:
    _set_token_verifier(
        monkeypatch,
        {"admin": False, "is_admin": False, "role": "user", "roles": ["user"]},
    )
    return {"Authorization": f"Bearer {USER_TOKEN}"}


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/data/update_database", "get"),
        ("/api/data/player_PBP", "get"),
        ("/api/data/opponent_PBP", "get"),
        ("/api/data/fetch_players_with_teams", "get"),
        ("/api/players/fetch", "get"),
    ],
)
def test_privileged_mutations_reject_wrong_http_method(client, path, method):
    response = getattr(client, method)(path)

    assert response.status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/api/data/update_database",
        "/api/data/player_PBP",
        "/api/data/opponent_PBP",
        "/api/data/fetch_players_with_teams",
        "/api/data/fetch_playtypes",
        "/api/players/fetch",
    ],
)
def test_privileged_routes_reject_missing_authentication(client, monkeypatch, path):
    _set_token_verifier(monkeypatch, {})

    method = "get" if path.endswith("fetch_playtypes") else (
        "post" if path.endswith(("update_database", "fetch_players_with_teams")) else "put"
    )
    response = getattr(client, method)(path)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/data/update_database",
        "/api/data/player_PBP",
        "/api/data/opponent_PBP",
        "/api/data/fetch_players_with_teams",
        "/api/data/fetch_playtypes",
        "/api/players/fetch",
    ],
)
def test_privileged_routes_reject_non_admin(client, monkeypatch, path):
    headers = _user_auth(monkeypatch)
    method = "get" if path.endswith("fetch_playtypes") else (
        "post" if path.endswith(("update_database", "fetch_players_with_teams")) else "put"
    )
    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("path", "method", "service_method", "operation"),
    [
        (
            "/api/data/update_database",
            "post",
            "update_all_data",
            "update_database",
        ),
        (
            "/api/data/player_PBP",
            "put",
            "fetch_PBP_data",
            "player_pbp",
        ),
        (
            "/api/data/opponent_PBP",
            "put",
            "fetch_PBP_data",
            "opponent_pbp",
        ),
        (
            "/api/data/fetch_players_with_teams",
            "post",
            "fetch_players_with_teams",
            "fetch_players_with_teams",
        ),
    ],
)
def test_data_mutation_routes_return_202_with_job_id(
    client,
    monkeypatch,
    path,
    method,
    service_method,
    operation,
):
    from app.routes import data_update_routes

    headers = _admin_auth(monkeypatch)
    fake = FakeJobService()
    monkeypatch.setattr(data_update_routes, "data_jobs_service", fake)
    monkeypatch.setattr(data_update_routes.data_service, service_method, lambda *args: True)

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 202
    body = response.get_json()
    assert body["job_id"] == "job-under-test"
    assert body["operation"] == operation
    assert body["status"] == "queued"


def test_fetch_playtypes_returns_data(client, monkeypatch):
    from app.routes import data_update_routes

    headers = _admin_auth(monkeypatch)
    monkeypatch.setattr(
        data_update_routes.data_service,
        "get_playtypes",
        lambda: [{"play_type": "Transition"}],
    )

    response = client.get("/api/data/fetch_playtypes", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == [{"play_type": "Transition"}]


def test_player_fetch_returns_202_job(client, monkeypatch):
    from app.routes import player_routes

    headers = _admin_auth(monkeypatch)
    fake = FakeJobService()
    monkeypatch.setattr(player_routes, "player_jobs_service", fake)
    monkeypatch.setattr(
        player_routes.player_service,
        "store_player_information",
        lambda: True,
    )

    response = client.put("/api/players/fetch", headers=headers)

    assert response.status_code == 202
    body = response.get_json()
    assert body["job_id"] == "job-under-test"
    assert body["operation"] == "fetch_players"


def test_job_status_route_enforces_auth(client, monkeypatch):
    from app.routes import data_update_routes

    fake = FakeJobService()
    monkeypatch.setattr(data_update_routes, "data_jobs_service", fake)

    _set_token_verifier(monkeypatch, {})
    response = client.get("/api/data/jobs/job-123")
    assert response.status_code == 401

    response = client.get(
        "/api/data/jobs/job-123", headers=_user_auth(monkeypatch)
    )
    assert response.status_code == 403

    response = client.get(
        "/api/data/jobs/job-123", headers=_admin_auth(monkeypatch)
    )
    assert response.status_code == 200
    assert response.get_json()["job_id"] == "job-123"


def test_admin_stats_rejects_non_admin(client, monkeypatch):
    headers = _user_auth(monkeypatch)

    response = client.get("/api/user/admin/stats", headers=headers)

    assert response.status_code == 403


def test_admin_stats_allows_admin_without_database_call(client, monkeypatch):
    from app.routes import user_routes

    headers = _admin_auth(monkeypatch)
    monkeypatch.setattr(
        user_routes.user_service,
        "get_all_active_users_count",
        lambda: 7,
    )

    response = client.get("/api/user/admin/stats", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "admin_stats": {"total_active_users": 7},
    }


def test_telemetry_route_rejects_missing_authentication(client, monkeypatch):
    _set_token_verifier(monkeypatch, {})
    response = client.get("/api/data/telemetry")
    assert response.status_code == 401


def test_telemetry_route_rejects_non_admin(client, monkeypatch):
    headers = _user_auth(monkeypatch)
    response = client.get("/api/data/telemetry", headers=headers)
    assert response.status_code == 403


def test_telemetry_route_returns_bounded_sanitized_metrics(client, monkeypatch):
    from app.utils import telemetry as telemetry_module

    telemetry_module.clear_recorded_provider_events()
    try:
        headers = _admin_auth(monkeypatch)
        telemetry_module.record_cached_provider_event(
            telemetry_module.PROVIDER_NBA_STATS, "player_game_logs"
        )

        response = client.get("/api/data/telemetry", headers=headers)

        assert response.status_code == 200
        body = response.get_json()
        assert body["provider_events_total"] == 1
        assert body["cache"]["nba_stats"] == {"hit": 1}
        assert len(body["recent_provider_events"]) == 1
        assert body["recent_provider_events"][0]["provider"] == "nba_stats"
        assert body["recent_provider_events"][0]["outcome"] == "success"
        assert "Authorization" not in str(body)
        assert "Bearer" not in str(body)
    finally:
        telemetry_module.clear_recorded_provider_events()
