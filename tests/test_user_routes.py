"""Tests for the authenticated user account routes.

These exercise the success paths, the not-found and validation branches, and
the failure handling of every route under ``/api/user`` except the admin
statistics endpoint, which is covered in ``test_admin_routes``.
"""

import pytest


def _raise(*args, **kwargs):
    raise RuntimeError("database is down")


def assert_error(response, status, code, message):
    """Assert the sanitized error envelope returned by route_error_boundary."""
    assert response.status_code == status
    assert response.get_json()["error"] == {"code": code, "message": message}


# --- authentication --------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/user/profile", "get"),
        ("/api/user/profile", "put"),
        ("/api/user/stats", "get"),
        ("/api/user/deactivate", "post"),
        ("/api/user/sync", "post"),
    ],
)
def test_protected_user_routes_reject_missing_authentication(
    client, authenticate, path, method
):
    # Installing the verifier makes Firebase available, which disables the
    # local development bypass the test app factory would otherwise allow.
    authenticate()

    response = getattr(client, method)(path)

    assert response.status_code == 401


# --- GET /profile ----------------------------------------------------------


def test_profile_returns_the_user_synced_onto_the_request(
    client, authenticate, make_db_user
):
    headers = authenticate(db_user=make_db_user(display_name="Synced User"))

    response = client.get("/api/user/profile", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["user"]["display_name"] == "Synced User"
    assert payload["user"]["firebase_uid"] == "test-uid"


def test_profile_falls_back_to_a_database_lookup(
    client, monkeypatch, authenticate, make_db_user
):
    from app.routes import user_routes

    headers = authenticate(db_user=None)
    monkeypatch.setattr(
        user_routes.user_service,
        "get_user_by_firebase_uid",
        lambda uid: make_db_user(display_name="Fetched User"),
    )

    response = client.get("/api/user/profile", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["user"]["display_name"] == "Fetched User"


def test_profile_returns_not_found_when_the_user_is_absent(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate(db_user=None)
    monkeypatch.setattr(
        user_routes.user_service, "get_user_by_firebase_uid", lambda uid: None
    )

    response = client.get("/api/user/profile", headers=headers)

    assert_error(response, 404, "resource_not_found", "User not found in database.")


def test_profile_returns_server_error_when_the_lookup_fails(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate(db_user=None)
    monkeypatch.setattr(user_routes.user_service, "get_user_by_firebase_uid", _raise)

    response = client.get("/api/user/profile", headers=headers)

    assert_error(response, 500, "operation_failed", "Failed to retrieve user profile.")


# --- PUT /profile ----------------------------------------------------------


def test_profile_update_rejects_an_empty_body(client, authenticate):
    headers = authenticate()

    response = client.put("/api/user/profile", headers=headers, json={})

    assert_error(response, 400, "invalid_input", "No profile data was provided.")


def test_profile_update_returns_not_found_for_an_unknown_user(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(
        user_routes.user_service, "get_user_by_firebase_uid", lambda uid: None
    )

    response = client.put(
        "/api/user/profile", headers=headers, json={"display_name": "New Name"}
    )

    assert_error(response, 404, "resource_not_found", "User not found in database.")


def test_profile_update_forwards_the_submitted_fields(
    client, monkeypatch, authenticate, make_db_user
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(
        user_routes.user_service,
        "get_user_by_firebase_uid",
        lambda uid: make_db_user(),
    )

    submitted = {}

    def capture(firebase_data):
        submitted.update(firebase_data)
        return make_db_user(
            display_name=firebase_data["name"], photo_url=firebase_data["picture"]
        )

    monkeypatch.setattr(user_routes.user_service, "create_or_update_user", capture)

    response = client.put(
        "/api/user/profile",
        headers=headers,
        json={"display_name": "New Name", "photo_url": "https://example.com/p.jpg"},
    )

    assert response.status_code == 200
    assert submitted["uid"] == "test-uid"
    assert submitted["name"] == "New Name"
    assert submitted["picture"] == "https://example.com/p.jpg"
    assert response.get_json()["user"]["display_name"] == "New Name"


def test_profile_update_keeps_existing_values_when_fields_are_omitted(
    client, monkeypatch, authenticate, make_db_user
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(
        user_routes.user_service,
        "get_user_by_firebase_uid",
        lambda uid: make_db_user(),
    )

    submitted = {}

    def capture(firebase_data):
        submitted.update(firebase_data)
        return make_db_user()

    monkeypatch.setattr(user_routes.user_service, "create_or_update_user", capture)

    response = client.put(
        "/api/user/profile", headers=headers, json={"unrelated": "value"}
    )

    assert response.status_code == 200
    assert submitted["name"] == "Test User"
    assert submitted["picture"] is None


def test_profile_update_reports_a_failed_save(
    client, monkeypatch, authenticate, make_db_user
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(
        user_routes.user_service,
        "get_user_by_firebase_uid",
        lambda uid: make_db_user(),
    )
    monkeypatch.setattr(
        user_routes.user_service, "create_or_update_user", lambda data: None
    )

    response = client.put(
        "/api/user/profile", headers=headers, json={"display_name": "New Name"}
    )

    assert_error(response, 500, "operation_failed", "Failed to update user profile.")


# --- GET /stats ------------------------------------------------------------


def test_stats_returns_the_service_payload(client, monkeypatch, authenticate):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(
        user_routes.user_service,
        "get_user_stats",
        lambda uid: {"queries_run": 12},
    )

    response = client.get("/api/user/stats", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "stats": {"queries_run": 12}}


def test_stats_returns_not_found_when_the_service_returns_none(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(user_routes.user_service, "get_user_stats", lambda uid: None)

    response = client.get("/api/user/stats", headers=headers)

    assert_error(response, 404, "resource_not_found", "User not found.")


def test_stats_returns_server_error_when_the_service_raises(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(user_routes.user_service, "get_user_stats", _raise)

    response = client.get("/api/user/stats", headers=headers)

    assert_error(response, 500, "operation_failed", "Failed to retrieve user statistics.")


# --- POST /activity/ping ---------------------------------------------------


def test_activity_ping_succeeds_without_authentication(client):
    response = client.post("/api/user/activity/ping")

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "message": "No authenticated user",
    }


def test_activity_ping_records_the_login_for_an_authenticated_user(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate()
    recorded = []
    monkeypatch.setattr(
        user_routes.user_service,
        "update_last_login",
        lambda uid: recorded.append(uid) or True,
    )

    response = client.post("/api/user/activity/ping", headers=headers)

    assert response.status_code == 200
    assert recorded == ["test-uid"]
    assert response.get_json() == {"success": True, "message": "Activity updated"}


def test_activity_ping_reports_an_unsuccessful_update(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(
        user_routes.user_service, "update_last_login", lambda uid: False
    )

    response = client.post("/api/user/activity/ping", headers=headers)

    assert_error(response, 500, "operation_failed", "Failed to update user activity.")


# --- POST /deactivate ------------------------------------------------------


def test_deactivate_confirms_a_successful_soft_delete(
    client, monkeypatch, authenticate
):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(user_routes.user_service, "deactivate_user", lambda uid: True)

    response = client.post("/api/user/deactivate", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "message": "Account deactivated successfully",
    }


def test_deactivate_reports_a_failed_soft_delete(client, monkeypatch, authenticate):
    from app.routes import user_routes

    headers = authenticate()
    monkeypatch.setattr(user_routes.user_service, "deactivate_user", lambda uid: False)

    response = client.post("/api/user/deactivate", headers=headers)

    assert_error(response, 500, "operation_failed", "Failed to deactivate account.")


# --- POST /sync ------------------------------------------------------------


def test_sync_confirms_a_user_already_persisted_by_the_middleware(
    client, authenticate, make_db_user
):
    headers = authenticate(db_user=make_db_user())

    response = client.post("/api/user/sync", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["user"]["firebase_uid"] == "test-uid"


def test_sync_reports_failure_when_the_user_was_not_persisted(client, authenticate):
    headers = authenticate(db_user=None)

    response = client.post("/api/user/sync", headers=headers)

    assert_error(response, 500, "operation_failed", "User synchronization failed.")
