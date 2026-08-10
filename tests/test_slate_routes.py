"""HTTP contract for the authenticated slate read."""

import pytest

from app.errors import InvalidInputError, ProviderUnavailableError


def test_get_slate_returns_the_service_payload(client, dependencies):
    expected = {
        "slate_date": "2026-01-02",
        "freshness": {
            "schedule": {
                "status": "fresh",
                "retrieved_at": "2026-01-02T10:00:00+00:00",
            },
            "pool": {"status": "unavailable", "retrieved_at": None, "providers": {}},
        },
        "games": [],
    }
    dependencies.slate_service.get_slate.return_value = expected

    response = client.get("/api/games/slate?date=2026-01-02")

    assert response.status_code == 200
    assert response.get_json() == expected
    dependencies.slate_service.get_slate.assert_called_once_with("2026-01-02")


def test_get_slate_requires_authentication(client, monkeypatch):
    from app.utils import auth

    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "false")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    response = client.get("/api/games/slate")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_get_slate_preserves_an_explicit_empty_date_as_invalid(client, dependencies):
    dependencies.slate_service.get_slate.side_effect = InvalidInputError("bad date")

    response = client.get("/api/games/slate?date=")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    dependencies.slate_service.get_slate.assert_called_once_with("")


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidInputError("bad date"), 400, "invalid_input"),
        (ProviderUnavailableError("missing schedule"), 503, "provider_unavailable"),
    ],
)
def test_get_slate_uses_standard_error_contract(
    client, dependencies, error, status, code
):
    dependencies.slate_service.get_slate.side_effect = error

    response = client.get("/api/games/slate?date=bad")

    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    assert response.headers["X-Request-ID"]
