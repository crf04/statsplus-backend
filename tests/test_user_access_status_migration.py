"""Migration 062: ``users.access_status`` backfills to active, and reruns are no-ops."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations

pytestmark = pytest.mark.real_migrations


def _downgrade_to_before_062(engine) -> None:
    """Rebuild the pre-062 shape: no access_status column, no app_settings."""
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE users DROP COLUMN access_status"))
        connection.execute(text("DROP TABLE app_settings"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 62"))
        for uid in ("old-1", "old-2"):
            connection.execute(
                text(
                    "INSERT INTO users (firebase_uid, email, created_at, last_login, "
                    "is_active) VALUES (:uid, :email, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP, 1)"
                ),
                {"uid": uid, "email": f"{uid}@example.com"},
            )


def test_existing_rows_backfill_to_active_and_rerun_is_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    _downgrade_to_before_062(engine)

    first = run_migrations(engine)

    assert first.applied == ("062_user_access_status",)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT firebase_uid, access_status FROM users ORDER BY firebase_uid")
        ).all()
        settings = connection.execute(text("SELECT count(*) FROM app_settings")).scalar()
    assert [tuple(r) for r in rows] == [("old-1", "active"), ("old-2", "active")]
    assert settings == 0  # no row seeded: sign-ups default to open
    column = next(
        c for c in inspect(engine).get_columns("users") if c["name"] == "access_status"
    )
    assert column["nullable"] is False

    second = run_migrations(engine)

    assert second.applied == ()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM users WHERE access_status = 'active'")
        ).scalar() == 2


def test_upgrade_function_is_idempotent_when_the_column_already_exists(tmp_path):
    from app.migrations import _add_user_access_status

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, "
                "is_active, access_status) VALUES ('w', 'w@example.com', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, 'waitlisted')"
            )
        )
        _add_user_access_status(connection)  # must not reset a waitlisted row
        _add_user_access_status(connection)

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT access_status FROM users WHERE firebase_uid = 'w'")
        ).scalar() == "waitlisted"
