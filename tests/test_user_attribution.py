"""First-touch attribution: ``POST /api/user/attribution`` (crf04/statsplus#203).

These tests drive the real Flask route with a real ``UserService`` on a
migrated SQLite database; only the Firebase token verifier is replaced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.services.user_service import UserService

FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "ref",
    "referrer",
    "landing_path",
)
ALL_NULL = {field: None for field in FIELDS}


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'attribution.db'}")
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(app, dependencies, engine, runtime_settings, monkeypatch):
    import app.utils.auth as auth

    service = UserService(engine, settings=runtime_settings)
    dependencies.user_service = service
    app.extensions["dependencies"] = dependencies
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": token, "email": f"{token}@example.com", "name": token},
    )
    test_client = app.test_client()
    test_client.service = service
    return test_client


def _as(token):
    return {"Authorization": f"Bearer {token}"}


def _stored(engine, uid):
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT utm_source, utm_medium, utm_campaign, utm_content, utm_term, "
                "ref, referrer, landing_path, landed_at FROM user_attributions "
                "WHERE firebase_uid = :uid"
            ),
            {"uid": uid},
        ).mappings().all()
    return [dict(row) for row in rows]


def _count(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM user_attributions")).scalar()


def test_first_record_is_created_with_the_submitted_values(client, engine):
    response = client.post(
        "/api/user/attribution",
        json={
            "utm_source": "x",
            "utm_medium": "paid",
            "utm_campaign": "launch",
            "utm_content": "a",
            "utm_term": "nba",
            "ref": "abc",
            "referrer": "https://twitter.com",
            "landing_path": "/matchups",
            "landed_at": "2026-10-15T12:34:56.000Z",
        },
        headers=_as("u1"),
    )

    assert response.status_code == 201
    assert response.get_json() == {"recorded": True}
    [row] = _stored(engine, "u1")
    assert row["utm_source"] == "x"
    assert row["utm_medium"] == "paid"
    assert row["utm_campaign"] == "launch"
    assert row["utm_content"] == "a"
    assert row["utm_term"] == "nba"
    assert row["ref"] == "abc"
    assert row["referrer"] == "https://twitter.com"
    assert row["landing_path"] == "/matchups"
    assert str(row["landed_at"]).startswith("2026-10-15 12:34:56")


def test_repeat_is_not_recorded_and_never_overwrites(client, engine):
    first = client.post(
        "/api/user/attribution",
        json={"utm_source": "first", "ref": "one"},
        headers=_as("u1"),
    )
    second = client.post(
        "/api/user/attribution",
        json={"utm_source": "second", "ref": "two", "utm_medium": "later"},
        headers=_as("u1"),
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json() == {"recorded": False}
    [row] = _stored(engine, "u1")
    assert (row["utm_source"], row["ref"], row["utm_medium"]) == ("first", "one", None)
    assert _count(engine) == 1


def test_each_account_gets_its_own_record(client, engine):
    client.post("/api/user/attribution", json={"ref": "a"}, headers=_as("u1"))
    other = client.post("/api/user/attribution", json={"ref": "b"}, headers=_as("u2"))

    assert other.status_code == 201
    assert _stored(engine, "u1")[0]["ref"] == "a"
    assert _stored(engine, "u2")[0]["ref"] == "b"


def test_a_waitlisted_account_is_recorded_and_stays_refused_elsewhere(client, engine):
    client.service.set_signups_open(False)

    response = client.post(
        "/api/user/attribution", json={"utm_source": "ad"}, headers=_as("waiting")
    )

    assert response.status_code == 201
    assert response.get_json() == {"recorded": True}
    assert _stored(engine, "waiting")[0]["utm_source"] == "ad"
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT access_status FROM users WHERE firebase_uid = 'waiting'")
        ).scalar() == "waitlisted"
    profile = client.get("/api/user/profile", headers=_as("waiting"))
    assert profile.status_code == 403
    assert profile.get_json()["error"]["code"] == "waitlisted"
    repeat = client.post("/api/user/attribution", json={}, headers=_as("waiting"))
    assert repeat.status_code == 200


def test_strings_are_trimmed_truncated_and_empty_becomes_null(client, engine):
    response = client.post(
        "/api/user/attribution",
        json={
            "utm_source": "  padded  ",
            "utm_medium": "",
            "utm_campaign": "   ",
            "referrer": "r" * 600,
            "landing_path": "  " + "p" * 600 + "  ",
        },
        headers=_as("u1"),
    )

    assert response.status_code == 201
    [row] = _stored(engine, "u1")
    assert row["utm_source"] == "padded"
    assert row["utm_medium"] is None
    assert row["utm_campaign"] is None
    assert row["referrer"] == "r" * 512
    assert row["landing_path"] == "p" * 512


@pytest.mark.parametrize(
    "landed_at", ["not a date", "", "2026-13-45T99:00:00Z", None]
)
def test_unparseable_landed_at_is_stored_null(client, engine, landed_at):
    response = client.post(
        "/api/user/attribution",
        json={"ref": "abc", "landed_at": landed_at},
        headers=_as("u1"),
    )

    assert response.status_code == 201
    [row] = _stored(engine, "u1")
    assert row["landed_at"] is None
    assert row["ref"] == "abc"


def test_unknown_keys_are_ignored(client, engine):
    response = client.post(
        "/api/user/attribution",
        json={"ref": "abc", "firebase_uid": "someone-else", "extra": {"nested": [1]}},
        headers=_as("u1"),
    )

    assert response.status_code == 201
    assert _stored(engine, "u1")[0]["ref"] == "abc"
    assert _stored(engine, "someone-else") == []


def test_an_all_null_body_is_still_recorded_as_a_direct_visit(client, engine):
    response = client.post("/api/user/attribution", json=ALL_NULL, headers=_as("u1"))
    empty = client.post("/api/user/attribution", json={}, headers=_as("u2"))

    assert response.status_code == 201
    assert empty.status_code == 201
    assert _stored(engine, "u1") == [{**ALL_NULL, "landed_at": None}]
    assert _stored(engine, "u2") == [{**ALL_NULL, "landed_at": None}]


@pytest.mark.parametrize(
    "body",
    [
        [],
        ["ref"],
        "ref",
        42,
        None,
        {"ref": 5},
        {"utm_source": True},
        {"referrer": ["a"]},
        {"landing_path": {"a": 1}},
        {"landed_at": 1760531696},
    ],
)
def test_invalid_bodies_get_invalid_attribution_and_store_nothing(client, engine, body):
    response = client.post("/api/user/attribution", json=body, headers=_as("u1"))

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_attribution"
    assert isinstance(error["message"], str) and error["message"]
    assert _count(engine) == 0


def test_an_unparseable_body_is_invalid_attribution(client, engine):
    response = client.post(
        "/api/user/attribution",
        data="{not json",
        content_type="application/json",
        headers=_as("u1"),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_attribution"


def test_a_missing_token_is_a_401_and_stores_nothing(client, engine):
    response = client.post("/api/user/attribution", json={"ref": "abc"})

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    assert _count(engine) == 0


def test_an_unverifiable_token_is_a_401(client, engine, monkeypatch):
    import app.utils.auth as auth

    def reject(token):
        raise ValueError("bad token")

    monkeypatch.setattr(auth, "verify_firebase_token", reject)

    response = client.post(
        "/api/user/attribution", json={"ref": "abc"}, headers=_as("forged")
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"
    assert _count(engine) == 0


def test_the_exemption_is_only_for_this_route(client):
    client.service.set_signups_open(False)
    client.post("/api/user/attribution", json={}, headers=_as("waiting"))

    for path in ("/api/user/targets", "/api/user/saved-filter-sets", "/api/user/stats"):
        response = client.get(path, headers=_as("waiting"))
        assert response.status_code == 403, path
        assert response.get_json()["error"]["code"] == "waitlisted"


def test_a_utc_offset_is_normalized_to_utc(client, engine):
    client.post(
        "/api/user/attribution",
        json={"landed_at": "2026-10-15T12:00:00-04:00"},
        headers=_as("u1"),
    )

    [row] = _stored(engine, "u1")
    assert str(row["landed_at"]).startswith("2026-10-15 16:00:00")


@pytest.mark.parametrize(
    "landed_at",
    [
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_a_timestamp_that_overflows_utc_conversion_is_stored_null(
    client, engine, landed_at
):
    response = client.post(
        "/api/user/attribution",
        json={"ref": "abc", "landed_at": landed_at},
        headers=_as("u1"),
    )

    assert response.status_code == 201
    [row] = _stored(engine, "u1")
    assert row["landed_at"] is None
    assert row["ref"] == "abc"


def test_the_earliest_representable_utc_timestamp_is_kept(client, engine):
    response = client.post(
        "/api/user/attribution",
        json={"landed_at": "0001-01-01T00:00:00+00:00"},
        headers=_as("u1"),
    )

    assert response.status_code == 201
    assert str(_stored(engine, "u1")[0]["landed_at"]).startswith("0001-01-01 00:00:00")


def test_losing_the_insert_race_reports_not_recorded_and_keeps_the_winner(
    client, engine, monkeypatch
):
    from sqlalchemy.orm import Session

    from app.models.user_attribution import UserAttribution

    client.post("/api/user/attribution", json={"ref": "winner"}, headers=_as("u1"))
    real_get = Session.get
    calls = []

    def blind_first_lookup(self, entity, ident, *args, **kwargs):
        calls.append(entity)
        if entity is UserAttribution and len(calls) == 1:
            return None  # the pre-check misses; the insert then hits the PK
        return real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(Session, "get", blind_first_lookup)

    response = client.post(
        "/api/user/attribution", json={"ref": "loser"}, headers=_as("u1")
    )

    assert response.status_code == 200
    assert response.get_json() == {"recorded": False}
    assert _stored(engine, "u1")[0]["ref"] == "winner"
    assert _count(engine) == 1
