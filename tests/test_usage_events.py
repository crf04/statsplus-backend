"""Usage events at the HTTP seam (crf04/statsplus#261).

These drive the real Flask routes with a real ``UserService`` and
``UsageEventService`` on a migrated SQLite database; only the Firebase token
verifier, the clock and the data services behind each route are replaced.
Web actions are written after a response is sent, so every request here is
closed before the database is read.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, text

from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.services.usage_events import WEB_ACTION_ENDPOINTS, UsageEventService
from app.services.user_service import UserService

#: Wednesday 12:00 Eastern.
NOON = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 14)
MCP = {"X-CourtAI-Client": "mcp"}

#: Each web action and the request a user's browser makes for it.
WEB_ACTION_REQUESTS = [
    ("matchup_viewed", "get", "/api/games/matchup?game_id=0022600001", {}),
    ("game_logs_queried", "get", "/api/games/game_logs?player_name=A", {}),
    ("query_prompt_parsed", "post", "/api/nl-query", {"json": {"query": "lebron over 25 points"}}),
    ("backtest_viewed", "get", "/api/user/targets/7/backtest", {}),
    ("backtest_viewed", "get", "/api/user/targets/backtests", {}),
]


class Clock:
    """A settable stand-in for the wall clock."""

    def __init__(self, instant: datetime):
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def clock():
    return Clock(NOON)


@pytest.fixture
def usage(engine, clock):
    return UsageEventService(engine, clock=clock)


@pytest.fixture
def client(app, dependencies, engine, runtime_settings, usage, monkeypatch):
    import app.utils.auth as auth

    dependencies.user_service = UserService(engine, settings=runtime_settings)
    dependencies.usage_event_service = usage
    dependencies.matchup_service = Mock(name="matchup_service")
    dependencies.matchup_service.get_matchup.return_value = {"game_id": "0022600001"}
    dependencies.game_service.get_filtered_logs.return_value = {"game_logs": []}
    dependencies.nl_service.process_query.return_value = {"filters": {}}
    dependencies.target_backtest_service = Mock(name="target_backtest_service")
    dependencies.target_backtest_service.backtest.return_value = ({"backtest": 1}, "hit")
    dependencies.target_backtest_service.backtest_all.return_value = (
        {"backtests": [{"target_id": 7, "status": "ok", "backtest": {"players": []}}]},
        "hit",
    )
    app.extensions["dependencies"] = dependencies

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": token, "name": f"Name {token}", "email": f"{token}@example.com"},
    )
    test_client = app.test_client()
    test_client.usage = usage
    return test_client


def _call(client, method, path, token="u1", headers=None, **kwargs):
    """Make a request, finish it the way a server does, and return it."""
    all_headers = {"Authorization": f"Bearer {token}"} if token else {}
    all_headers.update(headers or {})
    response = getattr(client, method)(path, headers=all_headers, **kwargs)
    response.get_data()
    response.close()
    # Usage writes run on a background pool; wait for them before reading.
    client.usage.drain()
    return response


def _events(engine):
    """Every stored row, in a stable order (writes happen after the response), as (uid, day, source, name, tool, outcome, latency_ms)."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT firebase_uid, day, source, name, tool, outcome, latency_ms "
                "FROM usage_events ORDER BY day, firebase_uid, name, id"
            )
        ).all()
    return [(uid, date.fromisoformat(str(day)), *rest) for uid, day, *rest in rows]


def _active(uid, source, day=TODAY):
    return (uid, day, source, "active", None, None, None)


def _action(uid, name, day=TODAY):
    return (uid, day, "web", name, None, None, None)


# --- active ------------------------------------------------------------------


def test_an_authenticated_request_writes_one_web_active_row_and_a_second_writes_none(client, engine):
    _call(client, "get", "/api/games/seasons")
    _call(client, "get", "/api/games/seasons")

    assert _events(engine) == [_active("u1", "web")]


def test_a_connector_request_right_after_web_activity_still_records_an_mcp_active_row(client, engine):
    _call(client, "get", "/api/games/seasons")
    _call(client, "get", "/api/games/seasons", headers=MCP)
    _call(client, "get", "/api/games/seasons", headers=MCP)

    assert _events(engine) == [_active("u1", "web"), _active("u1", "mcp")]


@pytest.mark.parametrize("value", ["MCP", "mcp ", "web", "claude", ""])
def test_an_unrecognised_client_header_records_web(client, engine, value):
    _call(client, "get", "/api/games/seasons", headers={"X-CourtAI-Client": value})

    assert _events(engine) == [_active("u1", "web")]


def test_each_account_has_its_own_active_row(client, engine):
    _call(client, "get", "/api/games/seasons", "u1")
    _call(client, "get", "/api/games/seasons", "u2")

    assert _events(engine) == [_active("u1", "web"), _active("u2", "web")]


def test_active_is_independent_of_the_login_touch_throttle(client, engine, clock):
    # The user row's last_login is fresh for the whole test, so the throttled
    # touch writes nothing on the second request; the new day still records.
    _call(client, "get", "/api/games/seasons")
    clock.instant = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    _call(client, "get", "/api/games/seasons")

    assert _events(engine) == [_active("u1", "web"), _active("u1", "web", date(2026, 10, 15))]


@pytest.mark.parametrize(
    ("instant", "day"),
    [
        # Eastern midnight in EDT (UTC-4).
        (datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC), date(2026, 10, 18)),
        (datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC), date(2026, 10, 19)),
        # The same boundary in EST (UTC-5).
        (datetime(2026, 12, 7, 4, 59, 59, tzinfo=UTC), date(2026, 12, 6)),
        (datetime(2026, 12, 7, 5, 0, 0, tzinfo=UTC), date(2026, 12, 7)),
    ],
)
def test_the_day_is_the_us_eastern_calendar_date(client, engine, clock, instant, day):
    clock.instant = instant

    _call(client, "get", "/api/games/matchup?game_id=0022600001")

    assert _events(engine) == [_active("u1", "web", day), _action("u1", "matchup_viewed", day)]


def test_requests_either_side_of_eastern_midnight_record_two_days(client, engine, clock):
    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)
    _call(client, "get", "/api/games/matchup?game_id=0022600001")
    clock.instant = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
    _call(client, "get", "/api/games/matchup?game_id=0022600001")

    sunday, monday = date(2026, 10, 18), date(2026, 10, 19)
    assert _events(engine) == [
        _active("u1", "web", sunday),
        _action("u1", "matchup_viewed", sunday),
        _active("u1", "web", monday),
        _action("u1", "matchup_viewed", monday),
    ]


# --- web actions -------------------------------------------------------------


def test_a_matchup_read_records_matchup_viewed_once_a_day(client, engine):
    _call(client, "get", "/api/games/matchup?game_id=0022600001")
    _call(client, "get", "/api/games/matchup?game_id=0022600001")

    assert _events(engine) == [_active("u1", "web"), _action("u1", "matchup_viewed")]


@pytest.mark.parametrize(("name", "method", "path", "kwargs"), WEB_ACTION_REQUESTS)
def test_each_web_action_records_its_own_name(client, engine, name, method, path, kwargs):
    response = _call(client, method, path, **kwargs)

    assert response.status_code == 200
    assert _events(engine) == [_active("u1", "web"), _action("u1", name)]


def test_the_table_of_requests_covers_every_endpoint_behind_an_action(app):
    adapter = app.url_map.bind("localhost")
    covered = {}
    for name, method, path, _ in WEB_ACTION_REQUESTS:
        endpoint, _arguments = adapter.match(path.split("?")[0], method=method.upper())
        covered[(method.upper(), endpoint)] = name

    assert covered == WEB_ACTION_ENDPOINTS


def test_other_authenticated_reads_record_no_action(client, engine):
    _call(client, "get", "/api/games/seasons")
    _call(client, "get", "/api/games/matchup/selection?game_id=0022600001&player_id=7")

    assert _events(engine) == [_active("u1", "web")]


def test_a_failed_read_records_no_action(client, engine, dependencies):
    dependencies.matchup_service.get_matchup.side_effect = ResourceNotFoundError("gone")

    missing = _call(client, "get", "/api/games/matchup?game_id=0022600001")
    invalid = _call(client, "get", "/api/games/matchup")
    dependencies.matchup_service.get_matchup.side_effect = RuntimeError("boom")
    broken = _call(client, "get", "/api/games/matchup?game_id=0022600001")

    assert (missing.status_code, invalid.status_code, broken.status_code) == (404, 400, 500)
    assert _events(engine) == [_active("u1", "web")]


def test_an_anonymous_request_records_nothing(client, engine):
    response = _call(client, "get", "/api/games/matchup?game_id=0022600001", token=None)

    assert response.status_code == 401
    assert _events(engine) == []


def test_the_same_read_through_the_connector_records_no_web_action(client, engine):
    response = _call(client, "get", "/api/games/matchup?game_id=0022600001", headers=MCP)

    assert response.status_code == 200
    assert _events(engine) == [_active("u1", "mcp")]


def test_the_connector_header_does_not_change_the_response(client):
    web = _call(client, "get", "/api/games/matchup?game_id=0022600001")
    mcp = _call(client, "get", "/api/games/matchup?game_id=0022600001", headers=MCP)

    assert (web.status_code, web.get_json()) == (mcp.status_code, mcp.get_json())


# --- failures and excluded traffic -------------------------------------------


def test_a_forced_recording_failure_still_returns_the_normal_response(client, engine, caplog):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE usage_events"))

    with caplog.at_level(logging.WARNING):
        response = _call(client, "get", "/api/games/matchup?game_id=0022600001")

    assert response.status_code == 200
    assert response.get_json() == {"game_id": "0022600001"}
    assert "Could not record a active usage event" in caplog.text
    assert "Could not record a matchup_viewed usage event" in caplog.text


def test_a_failed_write_is_retried_by_the_next_request(client, engine):
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE usage_events RENAME TO usage_events_away"))
    _call(client, "get", "/api/games/seasons")
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE usage_events_away RENAME TO usage_events"))

    _call(client, "get", "/api/games/seasons")

    assert _events(engine) == [_active("u1", "web")]


def test_the_local_auth_bypass_user_records_nothing(client, engine, usage, monkeypatch):
    import app.utils.auth as auth

    # A row the events could attach to, as the bypass user has in local development.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
                "VALUES ('dev-user', 'dev@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
            )
        )
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)
    monkeypatch.setattr(auth, "_auth_bypass_enabled", lambda: True)

    response = _call(client, "get", "/api/games/matchup?game_id=0022600001", token=None)
    tool_call = _call(
        client,
        "post",
        "/api/usage/mcp-tool-calls",
        token=None,
        headers=MCP,
        json={"tool": "get_matchup", "outcome": "ok", "latency_ms": 5},
    )

    assert (response.status_code, tool_call.status_code) == (200, 204)
    assert _events(engine) == []


def test_recording_is_off_for_the_read_only_demo_database(client, engine, dependencies):
    dependencies.usage_event_service = UsageEventService(engine, recording=False)

    response = _call(client, "get", "/api/games/matchup?game_id=0022600001")
    _call(
        client,
        "post",
        "/api/usage/mcp-tool-calls",
        headers=MCP,
        json={"tool": "get_matchup", "outcome": "ok", "latency_ms": 5},
    )

    assert response.status_code == 200
    assert _events(engine) == []


@pytest.mark.parametrize(
    ("database_url", "stored"),
    [("sqlite:///nba_play_types.db", []), ("sqlite:///other.db", [("u", "web", "active")])],
)
def test_only_the_demo_database_builds_a_service_that_records_nothing(
    monkeypatch, tmp_path, database_url, stored
):
    from app.config.settings import DatabaseSettings, RuntimeSettings
    from app.dependencies import build_dependencies

    engine = create_engine(f"sqlite:///{tmp_path / 'wired.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
                "VALUES ('u', 'u@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
            )
        )
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database=DatabaseSettings(url=database_url),
    )

    build_dependencies(settings).usage_event_service.record_daily("u", "web", "active")

    assert [(uid, source, name) for uid, _day, source, name, *_ in _events(engine)] == stored


# --- deleting the account ----------------------------------------------------


def test_deleting_the_user_deletes_their_rows(client, engine):
    _call(client, "get", "/api/games/matchup?game_id=0022600001", "u1")
    _call(client, "get", "/api/games/matchup?game_id=0022600001", "u2")
    _call(
        client,
        "post",
        "/api/usage/mcp-tool-calls",
        "u1",
        headers=MCP,
        json={"tool": "get_matchup", "outcome": "ok", "latency_ms": 5},
    )
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'u1'"))

    assert _events(engine) == [_active("u2", "web"), _action("u2", "matchup_viewed")]


# --- the tool-call endpoint --------------------------------------------------

CALL = {"tool": "get_matchup", "outcome": "ok", "latency_ms": 840}


def _post(client, body, token="u1", headers=MCP):
    return _call(client, "post", "/api/usage/mcp-tool-calls", token, headers=headers, json=body)


def test_a_valid_post_returns_204_and_stores_one_row_with_the_posted_values(client, engine):
    response = _post(client, CALL)

    assert (response.status_code, response.get_data()) == (204, b"")
    assert _events(engine) == [
        _active("u1", "mcp"),
        ("u1", TODAY, "mcp", "mcp_tool_call", "get_matchup", "ok", 840),
    ]


def test_a_tool_call_takes_the_us_eastern_day(client, engine, clock):
    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)
    _post(client, CALL)
    clock.instant = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
    _post(client, CALL)

    assert [(row[1], row[3]) for row in _events(engine) if row[3] == "mcp_tool_call"] == [
        (date(2026, 10, 18), "mcp_tool_call"),
        (date(2026, 10, 19), "mcp_tool_call"),
    ]


def test_repeated_posts_store_repeated_rows(client, engine):
    _post(client, CALL)
    _post(client, CALL)
    _post(client, {"tool": "query_game_logs", "outcome": "limit", "latency_ms": 0})

    assert [row[3:] for row in _events(engine)] == [
        ("active", None, None, None),
        ("mcp_tool_call", "get_matchup", "ok", 840),
        ("mcp_tool_call", "get_matchup", "ok", 840),
        ("mcp_tool_call", "query_game_logs", "limit", 0),
    ]


@pytest.mark.parametrize("outcome", ["ok", "error", "limit", "needs_choice"])
def test_every_outcome_is_accepted(client, engine, outcome):
    response = _post(client, {**CALL, "outcome": outcome})

    assert response.status_code == 204
    assert _events(engine)[-1][5] == outcome


@pytest.mark.parametrize(
    "body",
    [
        {**CALL, "tool": "GetMatchup"},
        {**CALL, "tool": "get-matchup"},
        {**CALL, "tool": ""},
        {**CALL, "tool": "a" * 65},
        {**CALL, "tool": "get_matchup\n"},
        {**CALL, "tool": 7},
        {key: value for key, value in CALL.items() if key != "tool"},
        {**CALL, "outcome": "success"},
        {**CALL, "outcome": "OK"},
        {**CALL, "outcome": None},
        {**CALL, "latency_ms": -1},
        {**CALL, "latency_ms": 600001},
        {**CALL, "latency_ms": 1.5},
        {**CALL, "latency_ms": "10"},
        {**CALL, "latency_ms": True},
        {key: value for key, value in CALL.items() if key != "latency_ms"},
        [CALL],
    ],
)
def test_invalid_input_returns_400_with_the_standard_error_and_stores_nothing(client, engine, body):
    response = _post(client, body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    assert [row for row in _events(engine) if row[3] == "mcp_tool_call"] == []


def test_the_limits_of_the_valid_range_are_accepted(client, engine):
    _post(client, {**CALL, "tool": "a" * 64, "latency_ms": 0})
    _post(client, {**CALL, "latency_ms": 600000})

    assert [row[4:] for row in _events(engine) if row[3] == "mcp_tool_call"] == [
        ("a" * 64, "ok", 0),
        ("get_matchup", "ok", 600000),
    ]


@pytest.mark.parametrize("headers", [{}, {"X-CourtAI-Client": "web"}, {"X-CourtAI-Client": "MCP"}])
def test_a_missing_or_wrong_client_header_returns_400_and_stores_nothing(client, engine, headers):
    response = _post(client, CALL, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    assert [row for row in _events(engine) if row[3] == "mcp_tool_call"] == []


def test_an_unauthenticated_post_is_refused(client, engine):
    response = _post(client, CALL, token=None)

    assert response.status_code == 401
    assert _events(engine) == []


def test_a_slow_tool_call_write_does_not_delay_the_204(client, engine, insert_attempts):
    insert_attempts.delay = 1.0

    started = time.monotonic()
    response = client.post(
        "/api/usage/mcp-tool-calls", headers={"Authorization": "Bearer u1", **MCP}, json=CALL
    )
    response.close()
    elapsed = time.monotonic() - started

    assert response.status_code == 204
    assert elapsed < insert_attempts.delay
    assert client.usage.drain(timeout=30)
    assert [row[3:] for row in _events(engine)] == [
        ("active", None, None, None),
        ("mcp_tool_call", "get_matchup", "ok", 840),
    ]


def test_a_storage_failure_still_returns_204(client, engine, caplog):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE usage_events"))

    with caplog.at_level(logging.WARNING):
        response = _post(client, CALL)

    assert response.status_code == 204
    assert "Could not record an MCP tool call" in caplog.text


# --- the write stays off the request path, and is attempted once -------------


def main_thread():
    return threading.main_thread()


@pytest.fixture
def insert_attempts(engine):
    """Counts INSERTs into usage_events; a test may set ``delay`` to slow each."""

    class Attempts:
        count = 0
        delay = 0.0

    attempts = Attempts()
    attempts.threads = []

    @event.listens_for(engine, "before_cursor_execute")
    def count(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO usage_events"):
            attempts.count += 1
            attempts.threads.append(threading.current_thread())
            time.sleep(attempts.delay)

    return attempts


def test_slow_usage_writes_delay_neither_response_on_a_connection(client, engine, insert_attempts):
    insert_attempts.delay = 1.0
    headers = {"Authorization": "Bearer u1"}

    started = time.monotonic()
    first = client.get("/api/games/matchup?game_id=0022600001", headers=headers)
    first.close()  # the server finishing the response, as on a keep-alive connection
    second = client.get("/api/games/game_logs?player_name=A", headers=headers)
    second.close()
    elapsed = time.monotonic() - started

    assert (first.status_code, second.status_code) == (200, 200)
    assert elapsed < insert_attempts.delay
    assert _events(engine) == []  # nothing has been written yet
    assert client.usage.drain(timeout=30)
    assert _events(engine) == [
        _active("u1", "web"),
        _action("u1", "game_logs_queried"),
        _action("u1", "matchup_viewed"),
    ]
    assert insert_attempts.threads and main_thread() not in insert_attempts.threads


def test_a_full_queue_drops_the_overflow_event_with_a_warning_and_writes_nothing_inline(
    client, engine, usage, insert_attempts, caplog
):
    usage._max_pending = 1
    insert_attempts.delay = 1.0

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        response = client.get("/api/games/matchup?game_id=0022600001", headers={"Authorization": "Bearer u1"})
        response.close()
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.get_json() == {"game_id": "0022600001"}
    assert elapsed < insert_attempts.delay
    assert "Usage event queue is full; dropped a matchup_viewed event" in caplog.text
    assert usage.drain(timeout=30)
    assert _events(engine) == [_active("u1", "web")]
    assert insert_attempts.count == 1
    assert main_thread() not in insert_attempts.threads


def test_active_takes_the_day_the_request_arrived_not_the_day_it_was_written(client, engine, clock):
    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)
    response = client.get("/api/games/seasons", headers={"Authorization": "Bearer u1"})
    clock.instant = datetime(2026, 10, 19, 4, 0, 1, tzinfo=UTC)
    response.close()
    client.usage.drain()

    assert _events(engine) == [_active("u1", "web", date(2026, 10, 18))]


def test_both_rows_take_the_day_the_request_arrived_when_the_route_crosses_midnight(
    client, engine, clock, dependencies
):
    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)  # Sunday 23:59:59 ET

    def cross_midnight(*args, **kwargs):
        clock.instant = datetime(2026, 10, 19, 4, 0, 1, tzinfo=UTC)  # Monday 00:00:01 ET
        return {"game_id": "0022600001"}

    dependencies.matchup_service.get_matchup.side_effect = cross_midnight

    response = _call(client, "get", "/api/games/matchup?game_id=0022600001")

    sunday = date(2026, 10, 18)
    assert response.status_code == 200
    assert _events(engine) == [_active("u1", "web", sunday), _action("u1", "matchup_viewed", sunday)]


def test_a_daily_event_is_attempted_once_per_process(client, insert_attempts):
    for _ in range(3):
        _call(client, "get", "/api/games/seasons")

    assert insert_attempts.count == 1


def test_concurrent_requests_for_one_key_make_one_attempt(usage, engine, insert_attempts):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
                "VALUES ('u1', 'u1@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
            )
        )
    insert_attempts.delay = 0.3
    gate = threading.Barrier(6)

    def write():
        gate.wait()
        usage.record_daily("u1", "web", "matchup_viewed")

    threads = [threading.Thread(target=write) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert insert_attempts.count == 1
    assert _events(engine) == [_action("u1", "matchup_viewed")]


def test_a_failed_write_releases_its_key_so_the_next_request_retries(client, engine, insert_attempts):
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE usage_events RENAME TO usage_events_away"))
    _call(client, "get", "/api/games/seasons")
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE usage_events_away RENAME TO usage_events"))

    _call(client, "get", "/api/games/seasons")
    _call(client, "get", "/api/games/seasons")

    assert insert_attempts.count == 2
    assert _events(engine) == [_active("u1", "web")]


def test_a_delayed_write_for_an_earlier_day_does_not_forget_todays_keys(usage, engine, insert_attempts):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
                "VALUES ('u1', 'u1@example.com', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
            )
        )
    usage.record_daily("u1", "web", "active")
    usage.record_daily("u1", "web", "active", at=datetime(2026, 10, 13, 16, 0, tzinfo=UTC))
    usage.record_daily("u1", "web", "active")

    assert insert_attempts.count == 2
    assert [row[1] for row in _events(engine)] == [DAY_BEFORE, TODAY]


DAY_BEFORE = date(2026, 10, 13)
