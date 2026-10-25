"""Migrations 076 and 077: the reports table and ``users.public_handle`` (crf04/statsplus#342)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import downgrade_reports, downgrade_user_public_handle, run_migrations

pytestmark = pytest.mark.real_migrations

_USER = (
    "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
    "VALUES ('u', 'u@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
)
_REPORT = (
    "INSERT INTO reports (id, firebase_uid, created_at, title, document, calls, cards) "
    "VALUES (:id, :uid, CURRENT_TIMESTAMP, 'Brief', '{}', '[]', '{}')"
)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_USER))
    yield engine
    engine.dispose()


def test_migration_076_creates_the_table_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE reports"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 76"))

    assert run_migrations(engine).applied == ("076_create_reports",)

    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("reports")}
    assert list(columns) == [
        "id",
        "firebase_uid",
        "created_at",
        "title",
        "document",
        "calls",
        "cards",
        "show_author",
        "deleted_at",
        "taken_down_at",
    ]
    assert str(columns["id"]["type"]) == "VARCHAR(12)"
    assert str(columns["title"]["type"]) == "VARCHAR(120)"
    assert {name for name, column in columns.items() if column["nullable"]} == {"deleted_at", "taken_down_at"}
    assert inspector.get_pk_constraint("reports")["constrained_columns"] == ["id"]
    [foreign_key] = inspector.get_foreign_keys("reports")
    assert foreign_key["referred_table"] == "users"
    assert foreign_key["constrained_columns"] == ["firebase_uid"]
    assert foreign_key["options"].get("ondelete") == "CASCADE"
    assert [(index["name"], index["column_names"]) for index in inspector.get_indexes("reports")] == [
        ("idx_reports_owner_created", ["firebase_uid", "created_at"])
    ]
    assert run_migrations(engine).applied == ()


def test_migration_077_adds_the_public_handle_to_an_existing_users_table(engine):
    with engine.begin() as connection:
        downgrade_user_public_handle(connection)
    assert "public_handle" not in {column["name"] for column in inspect(engine).get_columns("users")}

    assert run_migrations(engine).applied == ("077_user_public_handle",)

    columns = {column["name"]: column for column in inspect(engine).get_columns("users")}
    assert str(columns["public_handle"]["type"]) == "VARCHAR(40)"
    assert columns["public_handle"]["nullable"] is True
    with engine.connect() as connection:
        assert connection.execute(text("SELECT public_handle FROM users")).scalars().all() == [None]
    assert run_migrations(engine).applied == ()


def test_downgrade_drops_only_the_reports_table_and_upgrade_restores_it(engine):
    with engine.begin() as connection:
        downgrade_reports(connection)
    with engine.begin() as connection:
        downgrade_reports(connection)  # reversing twice is harmless

    assert "reports" not in inspect(engine).get_table_names()
    assert "users" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    assert 76 not in versions and {75, 77} <= versions

    assert run_migrations(engine).applied == ("076_create_reports",)
    assert "reports" in inspect(engine).get_table_names()


def test_a_report_for_an_unknown_account_is_refused_and_a_deleted_account_takes_its_reports(engine):
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        connection.execute(text(_REPORT), {"id": "ghost00001", "uid": "ghost"})

    with engine.begin() as connection:
        connection.execute(text(_REPORT), {"id": "kept000001", "uid": "u"})
        connection.execute(text(_REPORT), {"id": "kept000002", "uid": "u"})
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'u'"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reports")).scalar() == 0


def test_show_author_defaults_to_false_in_the_database(engine):
    with engine.begin() as connection:
        connection.execute(text(_REPORT), {"id": "default001", "uid": "u"})

    with engine.connect() as connection:
        assert connection.execute(text("SELECT show_author FROM reports")).scalar() == 0
