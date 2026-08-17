"""Authentication and authorization regression tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask, jsonify

import app.utils.auth as auth
from app.errors import register_error_handlers


class _FakeUserService:
    """Avoid database writes while testing token authorization."""

    def create_or_update_user(self, user_data: dict[str, Any]) -> None:
        return None


def _make_app(decorator, **config: Any) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config.update(config)
    register_error_handlers(app)
    app.extensions["dependencies"] = SimpleNamespace(
        user_service=_FakeUserService()
    )

    @app.get("/protected")
    @decorator
    def protected():
        user = auth.get_current_user()
        return jsonify({"uid": user["uid"]})

    return app


def test_require_auth_fails_closed_when_firebase_is_unavailable(monkeypatch):
    app = _make_app(auth.require_auth)
    monkeypatch.delenv("FIREBASE_ADMIN_DISABLED", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "provider_unavailable",
            "message": (
                "Firebase authentication is unavailable. Configure Firebase Admin "
                "credentials or enable the local development bypass."
            ),
        }
    }


def test_require_auth_allows_explicit_environment_bypass(monkeypatch):
    app = _make_app(auth.require_auth, TESTING=False)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 200
    assert response.get_json() == {"uid": "dev-user"}


def test_require_auth_allows_explicit_testing_bypass(monkeypatch):
    app = _make_app(auth.require_auth)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 200
    assert response.get_json() == {"uid": "dev-user"}


def test_repeated_required_auth_still_verifies_every_token(monkeypatch):
    verified_tokens = []
    synchronized_users = []

    class RecordingUserService:
        def create_or_update_user(self, user_data):
            synchronized_users.append(user_data["uid"])
            return None

    app = _make_app(auth.require_auth)
    app.extensions["dependencies"].user_service = RecordingUserService()
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    def verify(token):
        verified_tokens.append(token)
        return {"uid": "repeat-user", "email": "repeat@example.com"}

    monkeypatch.setattr(auth, "verify_firebase_token", verify)
    client = app.test_client()

    first = client.get(
        "/protected", headers={"Authorization": "Bearer first-token"}
    )
    second = client.get(
        "/protected", headers={"Authorization": "Bearer second-token"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert verified_tokens == ["first-token", "second-token"]
    assert synchronized_users == ["repeat-user", "repeat-user"]


def test_require_auth_rejects_legacy_config_bypass_without_environment_flag(monkeypatch):
    app = _make_app(auth.require_auth, AUTH_BYPASS_ENABLED=True)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("FIREBASE_ADMIN_DISABLED", raising=False)
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_require_auth_rejects_debug_only_bypass(monkeypatch):
    app = _make_app(auth.require_auth, TESTING=False, DEBUG=True)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_require_auth_rejects_nonlocal_environment(monkeypatch):
    app = _make_app(auth.require_auth, TESTING=False)
    monkeypatch.setenv("FLASK_ENV", "staging")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_local_bypass_is_rejected_in_production(monkeypatch):
    app = _make_app(auth.require_auth)
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_local_bypass_is_rejected_without_an_explicit_local_environment(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        TESTING=False,
        DEBUG=False,
        ENV=None,
        FLASK_ENV=None,
    )
    register_error_handlers(app)
    app.extensions["dependencies"] = SimpleNamespace(
        user_service=_FakeUserService()
    )

    @app.get("/protected")
    @auth.require_auth
    def protected():
        return jsonify({"ok": True})

    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_optional_auth_uses_shared_user_sync_mapping(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.get("/optional")
    @auth.require_auth_optional
    def optional():
        user = auth.get_current_user()
        return jsonify(
            {
                "uid": user["uid"],
                "email": user["email"],
                "db_user": user["db_user"],
            }
        )

    class _RecordingUserService:
        def create_or_update_user(self, user_data):
            return {"id": user_data["uid"]}

    app.extensions["dependencies"] = SimpleNamespace(
        user_service=_RecordingUserService()
    )

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": "optional-1",
            "email": "optional@example.com",
            "name": "Optional User",
        },
    )
    response = app.test_client().get(
        "/optional", headers={"Authorization": "Bearer optional-token"}
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "uid": "optional-1",
        "email": "optional@example.com",
        "db_user": {"id": "optional-1"},
    }


def test_optional_auth_ignores_user_sync_failures(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.get("/optional")
    @auth.require_auth_optional
    def optional():
        user = auth.get_current_user()
        return jsonify({"uid": user["uid"] if user else None})

    class _FailingUserService:
        def create_or_update_user(self, user_data):
            raise RuntimeError("database unavailable")

    app.extensions["dependencies"] = SimpleNamespace(
        user_service=_FailingUserService()
    )

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": "optional-2", "email": "optional@example.com"},
    )
    response = app.test_client().get(
        "/optional", headers={"Authorization": "Bearer optional-token"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"uid": "optional-2"}


def test_require_admin_rejects_authenticated_non_admin(monkeypatch):
    app = _make_app(auth.require_admin)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": "user-1", "email": "user@example.com", "role": "viewer"},
    )

    response = app.test_client().get(
        "/protected", headers={"Authorization": "Bearer test-token"}
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": {
            "code": "forbidden",
            "message": "Administrator privileges are required.",
        }
    }


@pytest.mark.parametrize(
    "claims",
    [
        {"admin": True},
        {"role": "admin"},
        {"roles": ["viewer", "admin"]},
    ],
)
def test_require_admin_allows_supported_firebase_admin_claims(monkeypatch, claims):
    app = _make_app(auth.require_admin)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": "admin-1", "email": "admin@example.com", **claims},
    )

    response = app.test_client().get(
        "/protected", headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"uid": "admin-1"}


def test_require_admin_allows_explicit_local_bypass(monkeypatch):
    app = _make_app(auth.require_admin)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)

    response = app.test_client().get("/protected")

    assert response.status_code == 200
    assert response.get_json() == {"uid": "dev-user"}
