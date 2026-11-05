"""The web engine's pool on PostgreSQL: what each read costs and how it recovers (#697).

Round trips are counted by the benchmark's TCP relay. Skipped unless
``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.config.settings import DatabaseSettings, RuntimeSettings
from app.utils import db
from scripts.benchmark_routes import _Relay

pytestmark = pytest.mark.integration


def _url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    return url


@pytest.fixture
def relay():
    url = make_url(_url())
    return _Relay(url.host, url.port or 5432)


@pytest.fixture
def engine(relay):
    relayed = make_url(_url()).set(host="127.0.0.1", port=relay.port).render_as_string(hide_password=False)
    engine = db.get_engine(
        RuntimeSettings(database=DatabaseSettings(url=relayed, statement_timeout_ms=30000))
    )
    yield engine
    engine.dispose()
    db.get_engine.cache_clear()


@pytest.fixture
def admin():
    admin = create_engine(_url())
    yield admin
    admin.dispose()


def _read(engine, sql="SELECT 7", **options):
    with engine.connect() as connection:
        if options:
            connection = connection.execution_options(**options)
        return connection.execute(text(sql)).scalar_one()


def _turns(relay, read):
    relay.reset()
    result = read()
    return result, relay.reset()["round_trips"]


def _terminate(admin, pid):
    with admin.connect() as connection:
        assert connection.execute(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid}).scalar_one()


def test_a_read_on_a_connection_returned_moments_ago_costs_begin_its_statement_and_rollback(engine, relay):
    _read(engine)

    assert _turns(relay, lambda: _read(engine)) == (7, 3)
    _read(engine, isolation_level="REPEATABLE READ")
    _read(engine, isolation_level="REPEATABLE READ", postgresql_readonly=True)
    # A pre-ping here also paid psycopg2's SET of the session's default
    # isolation and read-only setting.
    assert _turns(relay, lambda: _read(engine)) == (7, 3)


def test_a_connection_idle_past_the_threshold_is_pinged_before_its_read(engine, relay, monkeypatch):
    _read(engine)
    monkeypatch.setattr(db, "PING_AFTER_IDLE_SECONDS", 0.0)

    assert _turns(relay, lambda: _read(engine)) == (7, 4)


def test_a_connection_lost_while_idle_past_the_threshold_is_replaced_before_the_read(
    engine, admin, monkeypatch
):
    pid = _read(engine, "SELECT pg_backend_pid()")
    _terminate(admin, pid)
    monkeypatch.setattr(db, "PING_AFTER_IDLE_SECONDS", 0.0)

    assert _read(engine) == 7
    assert _read(engine, "SELECT pg_backend_pid()") != pid


def test_a_connection_lost_moments_after_its_last_read_fails_one_read_and_the_next_reconnects(engine, admin):
    pid = _read(engine, "SELECT pg_backend_pid()")
    _terminate(admin, pid)

    with pytest.raises(DBAPIError) as raised:
        _read(engine)
    assert raised.value.connection_invalidated
    assert _read(engine) == 7
    assert _read(engine, "SELECT pg_backend_pid()") != pid


def test_a_connection_held_past_the_threshold_and_lost_before_its_return_is_replaced_before_the_next_read(
    engine, admin, monkeypatch
):
    monkeypatch.setattr(db, "PING_AFTER_IDLE_SECONDS", 0.2)
    with engine.connect() as connection:
        pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        connection.rollback()
        _terminate(admin, pid)
        time.sleep(0.3)

    assert _read(engine) == 7
    assert _read(engine, "SELECT pg_backend_pid()") != pid


def test_a_ping_failing_for_a_reason_other_than_a_lost_connection_fails_the_checkout(engine, monkeypatch):
    _read(engine)
    monkeypatch.setattr(db, "PING_AFTER_IDLE_SECONDS", 0.0)
    refused = engine.dialect.loaded_dbapi.ProgrammingError("ping refused")

    def _refuse(_dbapi_connection):
        raise refused

    monkeypatch.setattr(engine.dialect, "do_ping", _refuse)

    with pytest.raises(DBAPIError) as raised:
        _read(engine)
    assert raised.value.orig is refused
    assert not raised.value.connection_invalidated


def test_a_read_only_repeatable_read_leaves_the_next_read_on_its_connection_writable_and_read_committed(
    engine,
):
    pid = _read(engine, "SELECT pg_backend_pid()")
    assert (
        _read(
            engine, "SHOW transaction_read_only", isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
        == "on"
    )

    assert _read(engine, "SELECT pg_backend_pid()") == pid
    assert _read(engine, "SHOW transaction_read_only") == "off"
    assert _read(engine, "SHOW transaction_isolation") == "read committed"
