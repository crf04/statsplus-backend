"""Integration tests that run against a real Postgres database.

Production uses Postgres while the default test suite uses SQLite, so dialect
differences (timezone-aware timestamps, identifier casing, DDL support) are
invisible everywhere else. These tests close that gap.

They are skipped unless ``TEST_DATABASE_URL`` is set. That variable is
deliberately distinct from ``DATABASE_URL`` so a developer's real database can
never be dropped by a test run.
"""

import os

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from app.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    return url


@pytest.fixture
def pg_engine(postgres_url):
    """Provide a Postgres engine with a freshly created schema."""
    engine = create_engine(postgres_url)

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield engine

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def user_service(pg_engine, monkeypatch):
    """Point UserService at the Postgres test database."""
    from sqlalchemy.orm import sessionmaker

    from app.services import user_service as user_service_module

    session_factory = sessionmaker(bind=pg_engine)
    monkeypatch.setattr(user_service_module, "get_session", session_factory)

    return user_service_module.UserService()


FIREBASE_USER = {
    "uid": "pg-uid-1",
    "email": "pg-user@example.com",
    "name": "Postgres User",
    "picture": "https://example.com/pg.jpg",
}


# --- schema ----------------------------------------------------------------


def test_user_model_ddl_applies_to_postgres(pg_engine):
    """The User model's columns and indexes must be creatable on Postgres."""
    with pg_engine.connect() as connection:
        columns = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            )
        ).scalars().all()
        indexes = connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'users'")
        ).scalars().all()

    assert set(columns) == {
        "firebase_uid",
        "email",
        "display_name",
        "photo_url",
        "created_at",
        "last_login",
        "is_active",
    }
    assert "idx_users_email" in indexes
    assert "idx_users_last_login" in indexes
    assert "idx_users_created_at" in indexes


# --- UserService round trips ----------------------------------------------


def test_create_then_update_user_persists_changes(user_service):
    created = user_service.create_or_update_user(FIREBASE_USER)
    assert created is not None

    updated = user_service.create_or_update_user(
        {**FIREBASE_USER, "name": "Renamed User"}
    )

    assert updated is not None
    fetched = user_service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    assert fetched.display_name == "Renamed User"
    assert fetched.email == FIREBASE_USER["email"]


def test_lookup_by_email_matches_the_created_user(user_service):
    user_service.create_or_update_user(FIREBASE_USER)

    found = user_service.get_user_by_email(FIREBASE_USER["email"])

    assert found is not None
    assert found.firebase_uid == FIREBASE_USER["uid"]


def test_update_last_login_reports_success(user_service):
    user_service.create_or_update_user(FIREBASE_USER)

    assert user_service.update_last_login(FIREBASE_USER["uid"]) is True


def test_update_last_login_reports_failure_for_an_unknown_user(user_service):
    assert user_service.update_last_login("does-not-exist") is False


def test_deactivate_user_removes_them_from_the_active_count(user_service):
    user_service.create_or_update_user(FIREBASE_USER)
    assert user_service.get_all_active_users_count() == 1

    assert user_service.deactivate_user(FIREBASE_USER["uid"]) is True

    assert user_service.get_all_active_users_count() == 0


def test_deactivate_reports_failure_for_an_unknown_user(user_service):
    assert user_service.deactivate_user("does-not-exist") is False


def test_user_stats_are_computable_against_postgres_timestamps(user_service):
    """Postgres returns timezone-aware timestamps; the stats math must cope."""
    user_service.create_or_update_user(FIREBASE_USER)

    stats = user_service.get_user_stats(FIREBASE_USER["uid"])

    assert stats is not None
    assert stats["is_active"] is True
    assert stats["account_age_days"] == 0
    assert stats["days_since_last_login"] == 0


def test_user_stats_are_none_for_an_unknown_user(user_service):
    assert user_service.get_user_stats("does-not-exist") is None


def test_last_login_is_stored_as_utc_not_server_local_time(user_service):
    """A naive UTC write into a timestamptz column lands in the future.

    Postgres reads a naive value as server-local time, so writing
    ``datetime.utcnow()`` skews the stored instant by the session's UTC offset.
    """
    from datetime import datetime, timezone

    user_service.create_or_update_user(FIREBASE_USER)

    fetched = user_service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    skew = (datetime.now(timezone.utc) - fetched.last_login).total_seconds()

    assert skew >= 0, f"last_login is {-skew:.0f}s in the future"
    assert skew < 300


# --- dialect-sensitive table reads ----------------------------------------


def test_game_service_reads_a_normalized_table_on_postgres(pg_engine, monkeypatch):
    """The unquoted identifiers GameService emits must resolve on Postgres."""
    from app.services import game_service as game_service_module

    pd.DataFrame(
        [
            {"team": "LAL", "Transition": 1.10},
            {"team": "GSW", "Transition": 1.30},
        ]
    ).to_sql("team_play_types", pg_engine, index=False, if_exists="replace")

    monkeypatch.setattr(game_service_module, "get_redis_client", lambda: None)
    service = game_service_module.GameService(pg_engine)

    df = service._fetch_data_from_table("team_play_types")

    assert sorted(df["team"].tolist()) == ["GSW", "LAL"]

    with pg_engine.connect() as connection:
        connection.execute(text("DROP TABLE IF EXISTS team_play_types"))
        connection.commit()


def test_user_round_trip_preserves_the_serialized_shape(user_service):
    """to_dict must stay JSON-serializable with real Postgres timestamps."""
    user_service.create_or_update_user(FIREBASE_USER)

    fetched = user_service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    payload = fetched.to_dict()

    assert payload["firebase_uid"] == FIREBASE_USER["uid"]
    assert payload["is_active"] is True
    assert isinstance(payload["created_at"], str)
    assert isinstance(payload["last_login"], str)
