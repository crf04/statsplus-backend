"""Pausing sign-ups: waitlisted users, the admin switch, and the lockout guarantees.

These tests drive the real Flask routes with a real ``UserService`` on a
migrated SQLite database.  Only the Firebase token verifier is replaced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text

from app.migrations import run_migrations
from app.models.user import User
from app.services.user_service import UserService

WAITLISTED_BODY = {
    "error": {
        "code": "waitlisted",
        "message": (
            "The beta is full right now. You're on the waitlist and we'll "
            "let you in soon."
        ),
    }
}
ADMIN_CLAIMS = {"admin": True, "role": "admin", "roles": ["admin"]}


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def gate_client(app, dependencies, engine, runtime_settings, monkeypatch):
    import app.utils.auth as auth

    service = UserService(engine, settings=runtime_settings)
    dependencies.user_service = service
    app.extensions["dependencies"] = dependencies

    claims_by_token = {
        "admin-1": ADMIN_CLAIMS,
        # Same email as "veteran": user sync fails on the unique email.
        "collider": {"email": "veteran@example.com"},
    }
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": token,
            "name": f"Name {token}",
            **{"email": f"{token}@example.com", **claims_by_token.get(token, {})},
        },
    )
    client = app.test_client()
    client.service = service
    return client


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _status(engine, uid: str):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT access_status FROM users WHERE firebase_uid = :uid"),
            {"uid": uid},
        ).scalar()


def _pause(client):
    response = client.put("/api/admin/signups", json={"open": False}, headers=_as("admin-1"))
    assert response.status_code == 200
    assert response.get_json() == {"open": False}


def test_signups_are_open_by_default(gate_client, engine):
    assert gate_client.get("/api/admin/signups", headers=_as("admin-1")).get_json() == {
        "open": True
    }

    response = gate_client.get("/api/user/profile", headers=_as("newcomer"))

    assert response.status_code == 200
    assert _status(engine, "newcomer") == "active"


def test_first_sign_in_while_paused_is_waitlisted_with_the_exact_403(gate_client, engine):
    _pause(gate_client)

    response = gate_client.get("/api/user/profile", headers=_as("newcomer"))

    assert response.status_code == 403
    assert response.get_json() == WAITLISTED_BODY
    assert _status(engine, "newcomer") == "waitlisted"
    # The next request is still refused: the status is durable.
    again = gate_client.get("/api/user/targets", headers=_as("newcomer"))
    assert again.status_code == 403
    assert again.get_json() == WAITLISTED_BODY


def test_pausing_never_locks_out_an_existing_user(gate_client, engine):
    assert gate_client.get("/api/user/profile", headers=_as("veteran")).status_code == 200

    _pause(gate_client)

    assert gate_client.get("/api/user/profile", headers=_as("veteran")).status_code == 200
    assert _status(engine, "veteran") == "active"


def test_a_row_that_predates_the_column_stays_active_while_paused(gate_client, engine):
    # A user row written by code that does not know access_status (a rollout
    # worker, or the pre-migration database) gets the column default.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
                "VALUES ('legacy', 'legacy@example.com', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 1)"
            )
        )
    _pause(gate_client)

    assert gate_client.get("/api/user/profile", headers=_as("legacy")).status_code == 200


def test_waitlisted_admin_can_still_use_admin_routes(gate_client, engine):
    # The admin's own first sign-in lands while sign-ups are paused.
    gate_client.service.set_signups_open(False)

    response = gate_client.get("/api/admin/signups", headers=_as("admin-1"))

    assert _status(engine, "admin-1") == "waitlisted"
    assert response.status_code == 200
    assert response.get_json() == {"open": False}
    users = gate_client.get("/api/admin/users?status=waitlisted", headers=_as("admin-1"))
    assert users.status_code == 200


def test_resuming_signups_admits_new_users_again(gate_client, engine):
    _pause(gate_client)
    gate_client.put("/api/admin/signups", json={"open": True}, headers=_as("admin-1"))

    assert gate_client.get("/api/user/profile", headers=_as("late")).status_code == 200
    assert _status(engine, "late") == "active"


def test_waitlisted_user_stays_waitlisted_after_signups_resume(gate_client, engine):
    _pause(gate_client)
    gate_client.get("/api/user/profile", headers=_as("queued"))
    gate_client.put("/api/admin/signups", json={"open": True}, headers=_as("admin-1"))

    assert gate_client.get("/api/user/profile", headers=_as("queued")).status_code == 403


def test_admin_lists_and_admits_waitlisted_users(gate_client, engine):
    gate_client.get("/api/admin/signups", headers=_as("admin-1"))  # admin row exists
    _pause(gate_client)
    gate_client.get("/api/user/profile", headers=_as("queued"))

    listing = gate_client.get("/api/admin/users?status=waitlisted", headers=_as("admin-1"))

    assert listing.status_code == 200
    users = listing.get_json()["users"]
    assert [u["uid"] for u in users] == ["queued"]
    queued = next(u for u in users if u["uid"] == "queued")
    assert set(queued) == {"uid", "email", "display_name", "created_at"}
    assert queued["email"] == "queued@example.com"
    assert queued["display_name"] == "Name queued"
    assert queued["created_at"]

    admitted = gate_client.post("/api/admin/users/queued/admit", headers=_as("admin-1"))

    assert admitted.status_code == 200
    assert admitted.get_json() == {"uid": "queued", "status": "active"}
    assert gate_client.get("/api/user/profile", headers=_as("queued")).status_code == 200
    remaining = gate_client.get("/api/admin/users?status=waitlisted", headers=_as("admin-1"))
    assert "queued" not in [u["uid"] for u in remaining.get_json()["users"]]


def test_admit_unknown_uid_is_404_and_admit_is_idempotent(gate_client):
    assert gate_client.post("/api/admin/users/ghost/admit", headers=_as("admin-1")).status_code == 404

    gate_client.get("/api/user/profile", headers=_as("veteran"))
    first = gate_client.post("/api/admin/users/veteran/admit", headers=_as("admin-1"))
    second = gate_client.post("/api/admin/users/veteran/admit", headers=_as("admin-1"))
    assert first.get_json() == second.get_json() == {"uid": "veteran", "status": "active"}


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/admin/signups", {}),
        ("put", "/api/admin/signups", {"json": {"open": False}}),
        ("get", "/api/admin/users?status=waitlisted", {}),
        ("post", "/api/admin/users/someone/admit", {}),
    ],
)
def test_admin_endpoints_reject_non_admins_and_anonymous(gate_client, engine, method, path, kwargs):
    denied = getattr(gate_client, method)(path, headers=_as("plain-user"), **kwargs)
    assert denied.status_code == 403
    assert denied.get_json()["error"]["code"] == "forbidden"

    anonymous = getattr(gate_client, method)(path, **kwargs)
    assert anonymous.status_code == 401
    # A rejected request changes nothing: signups stayed open.
    assert gate_client.get("/api/admin/signups", headers=_as("admin-1")).get_json() == {
        "open": True
    }


def test_put_signups_rejects_a_non_boolean(gate_client):
    for body in ({}, {"open": "no"}, {"open": 0}):
        response = gate_client.put("/api/admin/signups", json=body, headers=_as("admin-1"))
        assert response.status_code == 400
    assert gate_client.get("/api/admin/signups", headers=_as("admin-1")).get_json() == {
        "open": True
    }


def test_list_users_rejects_an_unknown_status(gate_client):
    response = gate_client.get("/api/admin/users?status=bogus", headers=_as("admin-1"))
    assert response.status_code == 400


def test_creation_race_yields_one_row_and_keeps_the_winner(engine, runtime_settings, monkeypatch):
    """A competing first request commits between this one's lookup and insert."""
    service = UserService(engine, settings=runtime_settings)
    service.set_signups_open(False)
    original = User.from_firebase_user.__func__

    def racing(cls, data):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (firebase_uid, email, created_at, last_login, "
                    "is_active, access_status) VALUES (:uid, :email, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP, 1, 'waitlisted')"
                ),
                {"uid": data["uid"], "email": data["email"]},
            )
        return original(cls, data)

    monkeypatch.setattr(User, "from_firebase_user", classmethod(racing))

    user = service.create_or_update_user({"uid": "racer", "email": "racer@example.com"})

    assert user is not None
    assert user.access_status == "waitlisted"
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT access_status FROM users WHERE firebase_uid = 'racer'")
        ).all()
    assert [r[0] for r in rows] == ["waitlisted"]


def test_existing_rows_are_never_rewritten_by_sync(engine, runtime_settings):
    service = UserService(engine, settings=runtime_settings)
    service.create_or_update_user({"uid": "a", "email": "a@example.com"})
    service.set_signups_open(False)
    service.create_or_update_user({"uid": "b", "email": "b@example.com"})
    service.set_signups_open(True)

    service.create_or_update_user({"uid": "a", "email": "a@example.com"})
    service.create_or_update_user({"uid": "b", "email": "b@example.com"})

    assert _status(engine, "a") == "active"
    assert _status(engine, "b") == "waitlisted"


def test_waitlist_refusal_runs_before_rate_limiting(gate_client, engine):
    """Authentication (and so the waitlist gate) precedes the limiter.

    The limiter keys an unauthenticated caller by IP at 60/min.  A waitlisted
    caller is refused before that bucket counts them, so past 60 requests they
    still get the waitlisted 403 and never a 429.
    """
    gate_client.service.set_signups_open(False)

    responses = [
        gate_client.get("/api/user/profile", headers=_as("queued")) for _ in range(65)
    ]

    assert {r.status_code for r in responses} == {403}
    assert responses[-1].get_json() == WAITLISTED_BODY
    assert "Retry-After" not in responses[-1].headers


def test_failed_user_sync_while_paused_is_refused_not_waved_through(gate_client, engine):
    """A sync that stores no row (unique-email clash) must not skip the gate."""
    gate_client.get("/api/user/profile", headers=_as("veteran"))
    gate_client.service.set_signups_open(False)

    response = gate_client.get("/api/user/profile", headers=_as("collider"))

    assert response.status_code == 403
    assert response.get_json() == WAITLISTED_BODY
    assert _status(engine, "collider") is None


def test_failed_user_sync_while_open_is_not_refused(gate_client, engine):
    gate_client.get("/api/user/profile", headers=_as("veteran"))

    response = gate_client.get("/api/user/profile", headers=_as("collider"))

    assert response.status_code != 403
    assert _status(engine, "collider") is None


def test_waitlisted_user_is_refused_on_optional_auth_routes_too(gate_client):
    gate_client.service.set_signups_open(False)

    response = gate_client.post("/api/user/activity/ping", headers=_as("queued"))

    assert response.status_code == 403
    assert response.get_json() == WAITLISTED_BODY


def test_first_admin_request_reads_the_signup_setting_once(gate_client, engine):
    """Provisioning a new admin and answering GET /signups share one read."""
    reads = []

    def record(_connection, _cursor, statement, *_args):
        if "FROM app_settings" in statement:
            reads.append(statement)

    event.listen(engine, "before_cursor_execute", record)

    response = gate_client.get("/api/admin/signups", headers=_as("admin-1"))

    assert response.status_code == 200
    assert len(reads) == 1
