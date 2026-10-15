"""Postgres checks for the sign-up switch: migration 062 and the creation race."""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import load_settings
from app.migrations import run_migrations
from app.services.user_service import UserService

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=16)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def test_migration_backfills_existing_rows_to_active_and_rerun_is_a_noop(engine):
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE users DROP COLUMN access_status"))
        connection.execute(text("DROP TABLE app_settings"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 62"))
        for uid in ("old-1", "old-2"):
            connection.execute(
                text(
                    "INSERT INTO users (firebase_uid, email, display_name, is_active) "
                    "VALUES (:uid, :email, 'Old', true)"
                ),
                {"uid": uid, "email": f"{uid}@example.com"},
            )

    assert run_migrations(engine).applied == ("062_user_access_status",)
    assert run_migrations(engine).applied == ()

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT firebase_uid, access_status FROM users ORDER BY 1")
        ).all()
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'access_status'"
            )
        ).scalar()
    assert [tuple(r) for r in rows] == [("old-1", "active"), ("old-2", "active")]
    assert nullable == "NO"


def test_concurrent_first_requests_while_paused_create_one_waitlisted_row(engine):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    service = UserService(engine, settings=settings)
    service.set_signups_open(False)
    workers = 8
    barrier = threading.Barrier(workers)
    results: list = []

    def first_request():
        barrier.wait()
        user = service.create_or_update_user(
            {"uid": "same-uid", "email": "same@example.com", "name": "Same"}
        )
        results.append(user.access_status if user is not None else None)

    threads = [threading.Thread(target=first_request) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["waitlisted"] * workers
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT firebase_uid, access_status FROM users")
        ).all()
    assert [tuple(r) for r in rows] == [("same-uid", "waitlisted")]


def test_signup_setting_persists_across_service_instances(engine):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    UserService(engine, settings=settings).set_signups_open(False)

    assert UserService(engine, settings=settings).get_signups_open() is False
    UserService(engine, settings=settings).set_signups_open(True)
    assert UserService(engine, settings=settings).get_signups_open() is True
