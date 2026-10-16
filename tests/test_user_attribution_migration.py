"""Migration 065: the per-user first-touch attribution table (crf04/statsplus#203)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations

pytestmark = pytest.mark.real_migrations


def test_migration_065_creates_the_table_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE user_attributions"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 65"))

    first = run_migrations(engine)

    assert first.applied == ("065_user_attributions",)
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("user_attributions")} == {
        "firebase_uid",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "ref",
        "referrer",
        "landing_path",
        "landed_at",
        "created_at",
    }
    assert inspector.get_pk_constraint("user_attributions")["constrained_columns"] == [
        "firebase_uid"
    ]
    [foreign_key] = inspector.get_foreign_keys("user_attributions")
    assert foreign_key["referred_table"] == "users"
    assert foreign_key["options"].get("ondelete") == "CASCADE"

    second = run_migrations(engine)

    assert second.applied == ()


def test_the_upgrade_function_is_idempotent_and_keeps_existing_rows(tmp_path):
    from app.migrations import _create_user_attributions_table

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, "
                "is_active) VALUES ('u', 'u@example.com', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_attributions (firebase_uid, ref, created_at) "
                "VALUES ('u', 'kept', CURRENT_TIMESTAMP)"
            )
        )
        _create_user_attributions_table(connection)
        _create_user_attributions_table(connection)

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT ref FROM user_attributions WHERE firebase_uid = 'u'")
        ).scalar() == "kept"
