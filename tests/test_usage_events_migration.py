"""Migration 073: the usage events table (crf04/statsplus#261)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import downgrade_usage_events, run_migrations

pytestmark = pytest.mark.real_migrations

_USER = (
    "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
    "VALUES ('u', 'u@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
)
_INSERT = (
    "INSERT INTO usage_events (firebase_uid, occurred_at, day, source, name, tool, outcome, latency_ms) "
    "VALUES ({uid}, CURRENT_TIMESTAMP, '2026-10-14', {source}, {name}, {tool}, {outcome}, {latency})"
)


def _insert(
    connection, source="'web'", name="'active'", tool="NULL", outcome="NULL", latency="NULL", uid="'u'"
):
    connection.execute(
        text(_INSERT.format(uid=uid, source=source, name=name, tool=tool, outcome=outcome, latency=latency))
    )


def _tool_call(connection, outcome="'ok'"):
    _insert(connection, "'mcp'", "'mcp_tool_call'", "'get_matchup'", outcome, "120")


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(_USER))
    return engine


def test_the_migration_creates_the_table_and_reruns_as_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE usage_events"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 73"))

    first = run_migrations(engine)

    assert first.applied == ("073_usage_events",)
    inspector = inspect(engine)
    assert [c["name"] for c in inspector.get_columns("usage_events")] == [
        "id",
        "firebase_uid",
        "occurred_at",
        "day",
        "source",
        "name",
        "tool",
        "outcome",
        "latency_ms",
    ]
    assert inspector.get_pk_constraint("usage_events")["constrained_columns"] == ["id"]
    [foreign_key] = inspector.get_foreign_keys("usage_events")
    assert foreign_key["referred_table"] == "users"
    assert foreign_key["options"].get("ondelete") == "CASCADE"
    assert {index["name"] for index in inspector.get_indexes("usage_events")} == {
        "uq_usage_events_daily",
        "idx_usage_events_day_name",
    }
    assert run_migrations(engine).applied == ()


def test_downgrade_drops_only_its_table_and_upgrade_restores_it(engine):
    with engine.begin() as connection:
        downgrade_usage_events(connection)
    with engine.begin() as connection:
        downgrade_usage_events(connection)  # reversing twice is harmless

    assert "usage_events" not in inspect(engine).get_table_names()
    assert {"users", "research_activity_days"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    assert 73 not in versions and {72} <= versions

    restored = run_migrations(engine)

    assert restored.applied == ("073_usage_events",)
    assert "usage_events" in inspect(engine).get_table_names()


@pytest.mark.parametrize(
    "values",
    [
        {"name": "'page_viewed'"},
        {"source": "'ios'"},
        {"source": "'mcp'", "name": "'mcp_tool_call'", "outcome": "'success'"},
        {"source": "NULL"},
        {"name": "NULL"},
    ],
)
def test_the_check_constraints_reject_an_unknown_name_source_and_outcome(engine, values):
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert(connection, **values)


def test_a_row_for_an_unknown_account_is_refused(engine):
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        _insert(connection, uid="'ghost'")


def test_the_unique_index_rejects_a_duplicate_daily_event(engine):
    with engine.begin() as connection:
        _insert(connection)
        _insert(connection, "'mcp'")  # another source is another key
        _insert(connection, name="'matchup_viewed'")  # another name is another key

    for name in ("'active'", "'matchup_viewed'"):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert(connection, name=name)


@pytest.mark.parametrize("outcome", ["'ok'", "'error'", "'limit'", "'needs_choice'"])
def test_duplicate_tool_calls_are_accepted(engine, outcome):
    with engine.begin() as connection:
        _tool_call(connection, outcome)
        _tool_call(connection, outcome)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM usage_events")).scalar() == 2


# --- 083_usage_report_published (crf04/statsplus#382) ---------------------------------------


def _as_before_083(engine):
    """Rebuild ``usage_events`` with the CHECK it had before ``report_published``, keeping one row."""

    with engine.begin() as connection:
        objects = connection.execute(
            text(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'usage_events' AND sql IS NOT NULL"
            )
        ).all()
        connection.execute(text("DROP TABLE usage_events"))
        for kind, _, sql in sorted(objects, key=lambda row: row[0] != "table"):
            if kind == "table":
                assert "'backtest_viewed', 'report_published'" in sql
                sql = sql.replace("'backtest_viewed', 'report_published'", "'backtest_viewed'")
            connection.execute(text(sql))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 83"))
        _insert(connection, name="'matchup_viewed'")


def _rows(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT id, firebase_uid, source, name FROM usage_events ORDER BY id")
        ).all()


def test_migration_083_lets_an_older_table_record_report_published_and_keeps_its_rows(engine):
    _as_before_083(engine)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert(connection, name="'report_published'")

    assert run_migrations(engine).applied == ("083_usage_report_published",)

    with engine.begin() as connection:
        _insert(connection, name="'report_published'")
    assert _rows(engine) == [(1, "u", "web", "matchup_viewed"), (2, "u", "web", "report_published")]
    assert {index["name"] for index in inspect(engine).get_indexes("usage_events")} == {
        "uq_usage_events_daily",
        "idx_usage_events_day_name",
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert(connection, name="'report_published'")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert(connection, name="'page_viewed'")
    assert run_migrations(engine).applied == ()
