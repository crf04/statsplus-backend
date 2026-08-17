"""User synchronization write-throttling tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event

from app.models.user import User
from app.services.user_service import UserService


FIREBASE_USER = {
    "uid": "auth-latency-user",
    "email": "auth-latency@example.com",
    "name": "Auth Latency User",
    "picture": "https://example.com/avatar.jpg",
}


@pytest.fixture
def user_engine():
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    yield engine
    engine.dispose()


def _seed_user(engine, *, last_login: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": FIREBASE_USER["uid"],
                "email": FIREBASE_USER["email"],
                "display_name": FIREBASE_USER["name"],
                "photo_url": FIREBASE_USER["picture"],
                "created_at": last_login - timedelta(days=1),
                "last_login": last_login,
                "is_active": True,
            },
        )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def test_identical_recent_user_is_not_touched_on_each_request(
    user_engine, runtime_settings
):
    original_login = datetime.now(timezone.utc) - timedelta(minutes=1)
    _seed_user(user_engine, last_login=original_login)
    service = UserService(user_engine, settings=runtime_settings)
    statements = []

    def record_statement(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    event.listen(user_engine, "before_cursor_execute", record_statement)

    service.create_or_update_user(FIREBASE_USER)

    stored = service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    assert _naive_utc(stored.last_login) == _naive_utc(original_login)
    assert not any(
        statement.startswith(("UPDATE", "INSERT", "DELETE"))
        for statement in statements
    )


def test_profile_change_is_persisted_without_refreshing_recent_login(
    user_engine, runtime_settings
):
    original_login = datetime.now(timezone.utc) - timedelta(minutes=1)
    _seed_user(user_engine, last_login=original_login)
    service = UserService(user_engine, settings=runtime_settings)

    service.create_or_update_user({**FIREBASE_USER, "name": "Renamed User"})

    stored = service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    assert stored.display_name == "Renamed User"
    assert _naive_utc(stored.last_login) == _naive_utc(original_login)


def test_stale_login_is_refreshed(user_engine, runtime_settings):
    original_login = datetime.now(timezone.utc) - timedelta(minutes=16)
    _seed_user(user_engine, last_login=original_login)
    service = UserService(user_engine, settings=runtime_settings)

    service.create_or_update_user(FIREBASE_USER)

    stored = service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    assert _naive_utc(stored.last_login) > _naive_utc(original_login)
