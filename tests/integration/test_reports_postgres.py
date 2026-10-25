"""Postgres checks for CourtAI reports (crf04/statsplus#342).

Covers what SQLite cannot: migrations 076 and 077 and their reversal on the
production dialect, the ``JSONB`` columns, and a publish and read round trip
of a realistic report through the service.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config.settings import load_settings
from app.migrations import downgrade_reports, downgrade_user_public_handle, run_migrations
from app.services.reports import ReportService
from app.services.user_service import UserService

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def _user(engine, uid):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    UserService(engine, settings=settings).create_or_update_user(
        {"uid": uid, "email": f"{uid}@example.com", "name": uid}
    )


def _body():
    return {
        "report": {
            "title": "Injury brief",
            "date": "2026-10-24",
            "opening": ["Three games carry a real absence."],
            "tabs": [
                {
                    "title": "Spots",
                    "layout": "rows",
                    "items": [{"title": "Brunson points", "tag": "lean", "cards": ["c1"]}],
                }
            ],
        },
        "calls": [
            {
                "id": "c1",
                "tool": "query_game_logs",
                "args": {"player": "Jalen Brunson"},
                "structured": {"rows": [{"PTS": 31, "MIN": "34:12"}], "note": "ünïcode ✓"},
                "card": "game-logs",
            }
        ],
        "cards": {"game-logs": "<html><script>render()</script></html>"},
        "show_author": True,
    }


def test_migrations_076_and_077_apply_reverse_and_reapply(engine):
    columns = {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("reports")}
    assert columns == {
        "id": "VARCHAR(12)",
        "firebase_uid": "VARCHAR(128)",
        "created_at": "TIMESTAMP",
        "title": "VARCHAR(120)",
        "document": "JSONB",
        "calls": "JSONB",
        "cards": "JSONB",
        "show_author": "BOOLEAN",
        "deleted_at": "TIMESTAMP",
        "taken_down_at": "TIMESTAMP",
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'reports' AND column_name = 'created_at'"
                )
            ).scalar()
            == "timestamp with time zone"
        )
        assert (
            connection.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'reports'::regclass AND contype = 'f'"
                )
            ).scalar()
            == "c"
        )
    users = {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("users")}
    assert users["public_handle"] == "VARCHAR(40)"

    with engine.begin() as connection:
        downgrade_reports(connection)
        downgrade_user_public_handle(connection)
    assert "reports" not in inspect(engine).get_table_names()
    assert "public_handle" not in {c["name"] for c in inspect(engine).get_columns("users")}
    assert run_migrations(engine).applied == ("076_create_reports", "077_user_public_handle")
    assert run_migrations(engine).applied == ()


def test_a_report_round_trips_and_goes_with_its_account(engine):
    _user(engine, "author")
    service = ReportService(engine)
    service.set_public_handle("author", "@bighatprops")

    published = service.publish("author", _body())
    read = service.read(published["id"], viewer_uid="author")

    assert read["author"] == {"label": "@bighatprops"}
    assert read["is_author"] is True
    assert read["created_at"].endswith("+00:00")
    assert read["calls"] == [
        {
            "id": "c1",
            "tool": "query_game_logs",
            "card": "game-logs",
            "structured": {"rows": [{"PTS": 31, "MIN": "34:12"}], "note": "ünïcode ✓"},
            "meta": None,
        }
    ]
    assert read["cards"] == {"game-logs": "<html><script>render()</script></html>"}
    assert service.meta(published["id"])["tags"] == {"lean": 1}
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT document -> 'tabs' -> 0 ->> 'layout' FROM reports WHERE id = :id"),
                {"id": published["id"]},
            ).scalar()
            == "rows"
        )

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'author'"))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reports")).scalar() == 0
