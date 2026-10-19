"""Postgres checks for usage events (crf04/statsplus#261).

Covers what SQLite cannot: the migration and its reversal on the production
dialect, the partial unique index and ``ON CONFLICT`` clause, concurrent first
writes of one daily event, and the report on a connection PostgreSQL itself
makes read-only.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config.settings import load_settings
from app.migrations import downgrade_usage_events, run_migrations
from app.services.usage_events import UsageEventService
from app.services.user_service import UserService
from scripts.usage_report import main as report_main

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

NOON = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
DAY = date(2026, 10, 14)


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


def _user(engine, uid):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    UserService(engine, settings=settings).create_or_update_user(
        {"uid": uid, "email": f"{uid}@example.com", "name": uid}
    )


def _service(engine):
    return UsageEventService(engine, clock=lambda: NOON)


def _rows(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT firebase_uid, day, source, name, tool, outcome, latency_ms FROM usage_events ORDER BY id"
            )
        ).all()


def test_migration_073_applies_reverses_and_reapplies(engine):
    inspector = inspect(engine)
    assert {c["name"]: str(c["type"]) for c in inspector.get_columns("usage_events")} == {
        "id": "BIGINT",
        "firebase_uid": "VARCHAR(128)",
        "occurred_at": "TIMESTAMP",
        "day": "DATE",
        "source": "VARCHAR(8)",
        "name": "VARCHAR(32)",
        "tool": "VARCHAR(64)",
        "outcome": "VARCHAR(16)",
        "latency_ms": "INTEGER",
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'usage_events' AND column_name = 'occurred_at'"
                )
            ).scalar()
            == "timestamp with time zone"
        )
        assert (
            connection.execute(
                text(
                    "SELECT is_identity FROM information_schema.columns WHERE table_name = 'usage_events' AND column_name = 'id'"
                )
            ).scalar()
            == "YES"
        )
        assert (
            connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_usage_events_daily'")
            )
            .scalar()
            .endswith("WHERE ((name)::text <> 'mcp_tool_call'::text)")
        )

    with engine.begin() as connection:
        downgrade_usage_events(connection)
    assert "usage_events" not in inspect(engine).get_table_names()
    assert run_migrations(engine).applied == ("073_usage_events",)
    assert run_migrations(engine).applied == ()


def test_the_database_enforces_the_closed_lists_and_the_daily_key(engine):
    _user(engine, "u1")
    insert = (
        "INSERT INTO usage_events (firebase_uid, occurred_at, day, source, name, tool, outcome, latency_ms) "
        "VALUES ('u1', now(), '2026-10-14', :source, :name, :tool, :outcome, :latency)"
    )

    def add(source="web", name="active", tool=None, outcome=None, latency=None):
        with engine.begin() as connection:
            connection.execute(
                text(insert),
                {"source": source, "name": name, "tool": tool, "outcome": outcome, "latency": latency},
            )

    add()
    add(source="mcp")
    for kwargs in (
        {"name": "page_viewed"},
        {"source": "ios"},
        {"source": "mcp", "name": "mcp_tool_call", "outcome": "success"},
        {},  # duplicate daily event
    ):
        with pytest.raises(IntegrityError):
            add(**kwargs)
    add("mcp", "mcp_tool_call", "get_matchup", "limit", 7)
    add("mcp", "mcp_tool_call", "get_matchup", "limit", 7)

    assert len(_rows(engine)) == 4


def test_deleting_the_user_deletes_their_rows(engine):
    _user(engine, "u1")
    _user(engine, "u2")
    for uid in ("u1", "u2"):
        _service(engine).record_daily(uid, "web", "active")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'u1'"))

    assert [row[0] for row in _rows(engine)] == ["u2"]


def test_concurrent_first_writes_of_one_daily_event_leave_one_row(engine):
    _user(engine, "u1")
    # Separate services share no memo, as separate worker processes do.
    services = [_service(engine) for _ in range(12)]
    gate = threading.Barrier(len(services))

    def write(service):
        gate.wait()
        service.record_daily("u1", "web", "matchup_viewed")

    threads = [threading.Thread(target=write, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert _rows(engine) == [("u1", DAY, "web", "matchup_viewed", None, None, None)]


def test_a_tool_call_row_round_trips_on_postgres(engine):
    _user(engine, "u1")
    service = _service(engine)

    service.record_tool_call("u1", "get_matchup", "needs_choice", 612)
    service.record_tool_call("u1", "get_matchup", "needs_choice", 612)

    assert _rows(engine) == [("u1", DAY, "mcp", "mcp_tool_call", "get_matchup", "needs_choice", 612)] * 2


def test_the_report_runs_on_a_read_only_postgres_connection_and_changes_nothing(engine, capsys):
    for uid in ("u1", "u2"):
        _user(engine, uid)
    with engine.begin() as connection:
        # Created 03:30 UTC Monday: still Sunday evening in Eastern time.
        connection.execute(
            text("UPDATE users SET created_at = '2026-10-12 03:30:00+00' WHERE firebase_uid = 'u1'")
        )
        connection.execute(
            text("UPDATE users SET created_at = '2026-10-12 04:30:00+00' WHERE firebase_uid = 'u2'")
        )
    service = _service(engine)
    service.record_daily("u1", "web", "active")
    service.record_daily("u2", "mcp", "active")
    service.record_tool_call("u1", "get_matchup", "ok", 100)
    service.record_tool_call("u2", "get_matchup", "error", 300)
    before = _rows(engine)

    assert (
        report_main(
            ["--database-url", str(engine.url.render_as_string(hide_password=False)), "--weeks", "2"],
            today=DAY,
        )
        == 0
    )

    # Earlier tests may leave JSON logging on stdout; the report starts at its first line.
    out = capsys.readouterr().out
    out = out[out.index("Usage events recorded") :]
    assert (
        out
        == """\
Usage events recorded since 2026-10-14

Weekly active users (week starting Monday, US Eastern)
week        web  mcp  combined
2026-10-05    0    0         0
2026-10-12    1    1         2

Signup-cohort retention (share active, any source, N weeks after signup)
cohort      users   wk0   wk1
2026-10-05      1    0%  100%
2026-10-12      1  100%     -

Weekly users per web action
week        matchup_viewed  game_logs_queried  query_prompt_parsed  backtest_viewed
2026-10-05               0                  0                    0                0
2026-10-12               0                  0                    0                0

MCP tool calls, 2026-10-05 to today
tool         calls  users  error  limit  needs_choice  p50_ms  p95_ms
get_matchup      2      2    50%     0%            0%     100     300
"""
    )
    assert _rows(engine) == before

    from scripts.usage_report import open_read_only_engine

    read_only = open_read_only_engine(str(engine.url.render_as_string(hide_password=False)))
    try:
        with pytest.raises(DBAPIError, match="read-only transaction"), read_only.begin() as connection:
            connection.execute(text("DELETE FROM usage_events"))
    finally:
        read_only.dispose()
