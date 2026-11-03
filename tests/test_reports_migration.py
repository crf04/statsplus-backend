"""Migrations 076 and 077: the reports table and ``users.public_handle`` (crf04/statsplus#342),
080: listing, Featured and unique normalised handles (crf04/statsplus#372), and 081: author
edits (crf04/statsplus#377), and 082: each report's origin (crf04/statsplus#382)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import (
    downgrade_report_edits,
    downgrade_report_origin,
    downgrade_reports,
    downgrade_research_feed,
    downgrade_user_public_handle,
    run_migrations,
)

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
        "listed_at",
        "featured_at",
        "covers",
        "notes",
        "edited_at",
        "edits",
        "origin",
    ]
    assert str(columns["id"]["type"]) == "VARCHAR(12)"
    assert str(columns["title"]["type"]) == "VARCHAR(120)"
    assert {name for name, column in columns.items() if column["nullable"]} == {
        "deleted_at",
        "taken_down_at",
        "listed_at",
        "featured_at",
        "covers",
        "notes",
        "edited_at",
        "edits",
    }
    assert inspector.get_pk_constraint("reports")["constrained_columns"] == ["id"]
    [foreign_key] = inspector.get_foreign_keys("reports")
    assert foreign_key["referred_table"] == "users"
    assert foreign_key["constrained_columns"] == ["firebase_uid"]
    assert foreign_key["options"].get("ondelete") == "CASCADE"
    assert [(index["name"], index["column_names"]) for index in inspector.get_indexes("reports")] == [
        ("idx_reports_feed", ["listed_at", "id"]),
        ("idx_reports_owner_created", ["firebase_uid", "created_at"]),
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


# --- 080_research_feed ----------------------------------------------------------------------


def _before_080(engine, handles):
    """The database as 079 left it, holding ``handles`` (uid to stored value)."""

    with engine.begin() as connection:
        downgrade_research_feed(connection)
        connection.execute(text("DELETE FROM users"))
        for uid, handle in handles.items():
            connection.execute(
                text(
                    "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active, public_handle) "
                    "VALUES (:uid, :uid || '@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, :handle)"
                ),
                {"uid": uid, "handle": handle},
            )
        connection.execute(text(_REPORT), {"id": "old0000001", "uid": next(iter(handles))})


def _handles(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(text("SELECT firebase_uid, public_handle FROM users ORDER BY 1")).all()
        )


def _versions(engine):
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())


def test_migration_080_normalises_handles_and_existing_reports_stay_unlisted(engine):
    _before_080(
        engine,
        {"founder": "@bighatprops", "mixed": "  @Mixed_Case1 ", "plain": "plain_one", "none": None},
    )
    assert "listed_at" not in {column["name"] for column in inspect(engine).get_columns("reports")}

    assert run_migrations(engine).applied == ("080_research_feed",)

    assert _handles(engine) == {
        "founder": "bighatprops",
        "mixed": "mixed_case1",
        "none": None,
        "plain": "plain_one",
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT listed_at, featured_at, covers FROM reports")).all() == [
            (None, None, None)
        ]
    assert "report_listings" in inspect(engine).get_table_names()
    assert "idx_reports_feed" in {index["name"] for index in inspect(engine).get_indexes("reports")}
    assert run_migrations(engine).applied == ()


def test_after_080_a_handle_is_unique_case_insensitively_in_the_database(engine):
    _before_080(engine, {"founder": "@bighatprops", "other": None})
    run_migrations(engine)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET public_handle = 'BIGHATPROPS' WHERE firebase_uid = 'other'")
        )


@pytest.mark.parametrize("bad", ["@b", "bad handle", "x" * 21, "dash-name", "", "@"])
def test_migration_080_fails_loudly_on_a_handle_it_cannot_normalise_and_changes_nothing(engine, bad):
    _before_080(engine, {"founder": "@bighatprops", "broken": bad})

    with pytest.raises(ValueError, match="cannot be normalised .* for firebase_uid broken; fix or clear"):
        run_migrations(engine)

    assert _handles(engine) == {"broken": bad, "founder": "@bighatprops"}
    assert 80 not in _versions(engine)
    assert "listed_at" not in {column["name"] for column in inspect(engine).get_columns("reports")}
    assert "report_listings" not in inspect(engine).get_table_names()


def test_migration_080_fails_loudly_on_handles_that_collide_once_normalised(engine):
    _before_080(engine, {"founder": "@BigHatProps", "copycat": "bighatprops", "fine": "@fine"})

    with pytest.raises(ValueError, match=r"collide once normalised: bighatprops \(copycat, founder\)"):
        run_migrations(engine)

    assert _handles(engine) == {"copycat": "bighatprops", "fine": "@fine", "founder": "@BigHatProps"}
    assert 80 not in _versions(engine)
    assert "listed_at" not in {column["name"] for column in inspect(engine).get_columns("reports")}
    assert "report_listings" not in inspect(engine).get_table_names()


# --- 081_report_edits ----------------------------------------------------------------------


def test_migration_081_adds_the_edit_columns_and_existing_reports_read_as_published(engine):
    with engine.begin() as connection:
        connection.execute(text(_REPORT), {"id": "old0000001", "uid": "u"})
        downgrade_report_edits(connection)
    with engine.begin() as connection:
        downgrade_report_edits(connection)  # reversing twice is harmless
    columns = {column["name"] for column in inspect(engine).get_columns("reports")}
    assert not {"notes", "edited_at", "edits"} & columns
    assert 81 not in _versions(engine)

    assert run_migrations(engine).applied == ("081_report_edits",)

    columns = {column["name"]: column for column in inspect(engine).get_columns("reports")}
    assert [str(columns[name]["type"]) for name in ("notes", "edited_at", "edits")] == [
        "TEXT",
        "DATETIME",
        "JSON",
    ]
    assert all(columns[name]["nullable"] for name in ("notes", "edited_at", "edits"))
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id, title, document, calls, notes, edited_at, edits FROM reports")
        ).all() == [("old0000001", "Brief", "{}", "[]", None, None, None)]
    assert run_migrations(engine).applied == ()


def test_migration_081_reruns_after_a_partial_run_left_some_columns(engine):
    with engine.begin() as connection:
        downgrade_report_edits(connection)
        connection.execute(text("ALTER TABLE reports ADD COLUMN notes TEXT"))

    assert run_migrations(engine).applied == ("081_report_edits",)

    assert {"notes", "edited_at", "edits"} <= {
        column["name"] for column in inspect(engine).get_columns("reports")
    }


# --- 082_report_origin ---------------------------------------------------------------------


def _origins(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT id, origin FROM reports ORDER BY id")).all()


def test_migration_082_makes_every_existing_report_an_assistant_report(engine):
    with engine.begin() as connection:
        connection.execute(text(_REPORT), {"id": "old0000001", "uid": "u"})
        downgrade_report_origin(connection)
    with engine.begin() as connection:
        downgrade_report_origin(connection)  # reversing twice is harmless
    assert "origin" not in {column["name"] for column in inspect(engine).get_columns("reports")}
    assert 82 not in _versions(engine)

    assert run_migrations(engine).applied == ("082_report_origin",)

    assert _origins(engine) == [("old0000001", "assistant")]
    with engine.begin() as connection:
        connection.execute(text(_REPORT), {"id": "new0000001", "uid": "u"})
        connection.execute(
            text(
                "INSERT INTO reports (id, firebase_uid, created_at, title, document, calls, cards, origin) "
                "VALUES ('bld0000001', 'u', CURRENT_TIMESTAMP, 'Mine', '{}', '[]', '{}', 'builder')"
            )
        )
    assert _origins(engine) == [
        ("bld0000001", "builder"),
        ("new0000001", "assistant"),
        ("old0000001", "assistant"),
    ]
    assert run_migrations(engine).applied == ()


@pytest.mark.parametrize("origin", ["'ai'", "'Builder'", "NULL"])
def test_after_082_the_database_refuses_any_other_origin(engine, origin):
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reports (id, firebase_uid, created_at, title, document, calls, cards, origin) "
                f"VALUES ('bad0000001', 'u', CURRENT_TIMESTAMP, 'Mine', '{{}}', '[]', '{{}}', {origin})"
            )
        )
