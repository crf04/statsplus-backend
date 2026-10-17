"""Migration 068: research activity counts and cohort tags (crf04/statsplus#213)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations

pytestmark = pytest.mark.real_migrations

_USER = (
    "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
    "VALUES ('u', 'u@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
)


def test_migration_068_creates_both_tables_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE research_activity_days"))
        connection.execute(text("DROP TABLE account_cohorts"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 68"))

    first = run_migrations(engine)

    assert first.applied == ("068_research_activity",)
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("research_activity_days")} == {
        "firebase_uid",
        "game_day",
        "actions",
    }
    assert inspector.get_pk_constraint("research_activity_days")["constrained_columns"] == [
        "firebase_uid",
        "game_day",
    ]
    assert {c["name"] for c in inspector.get_columns("account_cohorts")} == {
        "firebase_uid",
        "cohort",
        "tagged_at",
    }
    assert inspector.get_pk_constraint("account_cohorts")["constrained_columns"] == ["firebase_uid"]
    for table in ("research_activity_days", "account_cohorts"):
        [foreign_key] = inspector.get_foreign_keys(table)
        assert foreign_key["referred_table"] == "users"
        assert foreign_key["options"].get("ondelete") == "CASCADE"

    second = run_migrations(engine)

    assert second.applied == ()


def test_the_tables_refuse_values_the_service_never_writes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_USER))

    for statement in (
        "INSERT INTO research_activity_days VALUES ('u', '2026-10-14', 0)",
        "INSERT INTO account_cohorts VALUES ('u', 'vip', CURRENT_TIMESTAMP)",
        "INSERT INTO research_activity_days VALUES ('ghost', '2026-10-14', 1)",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement))


def test_deleting_an_account_deletes_its_counts_and_tag(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_USER))
        connection.execute(text("INSERT INTO research_activity_days VALUES ('u', '2026-10-14', 3)"))
        connection.execute(text("INSERT INTO account_cohorts VALUES ('u', 'friend', CURRENT_TIMESTAMP)"))
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'u'"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM research_activity_days")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM account_cohorts")).scalar() == 0


def test_the_upgrade_function_is_idempotent_and_keeps_existing_rows(tmp_path):
    from app.migrations import _create_research_activity_tables

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_USER))
        connection.execute(text("INSERT INTO research_activity_days VALUES ('u', '2026-10-14', 3)"))
        connection.execute(text("INSERT INTO account_cohorts VALUES ('u', 'friend', CURRENT_TIMESTAMP)"))
        _create_research_activity_tables(connection)
        _create_research_activity_tables(connection)

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT actions FROM research_activity_days WHERE firebase_uid = 'u'")
            ).scalar()
            == 3
        )
        assert (
            connection.execute(text("SELECT cohort FROM account_cohorts WHERE firebase_uid = 'u'")).scalar()
            == "friend"
        )
