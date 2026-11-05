"""One request's reads on one checked-out PostgreSQL connection (#697).

Each read keeps its own transaction and snapshot; only the checkout, and its
pre-ping, is shared.  Skipped unless ``TEST_DATABASE_URL`` names a disposable
database.
"""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.utils.db import RequestPool, request_connection

pytestmark = pytest.mark.integration


def _url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    return url


@pytest.fixture
def engine():
    engine = create_engine(
        _url(), poolclass=RequestPool, pool_pre_ping=True, pool_size=1, max_overflow=1, pool_timeout=0.5
    )
    yield engine
    engine.dispose()


@pytest.fixture
def checkouts(engine):
    counted: list[int] = []
    event.listen(engine, "checkout", lambda *_: counted.append(1))
    return counted


@pytest.fixture
def writer():
    writer = create_engine(_url())
    yield writer
    writer.dispose()


@pytest.fixture
def table(writer):
    name = f"lease_{uuid4().hex[:8]}"
    with writer.begin() as connection:
        connection.execute(text(f"CREATE TABLE {name} (id integer)"))
    yield name
    with writer.begin() as connection:
        connection.execute(text(f"DROP TABLE {name}"))


def _backend_and_transaction(connection) -> tuple[int, int]:
    return tuple(connection.execute(text("SELECT pg_backend_pid(), txid_current()")).one())


def test_a_requests_reads_share_one_checkout_and_each_has_its_own_transaction(engine, checkouts):
    with request_connection(engine):
        reads = []
        for _ in range(3):
            with engine.connect() as connection:
                reads.append(_backend_and_transaction(connection))
        session = Session(bind=engine)
        reads.append(_backend_and_transaction(session))
        session.close()

    assert len({backend for backend, _ in reads}) == 1
    assert len({transaction for _, transaction in reads}) == 4
    assert len(checkouts) == 1
    assert engine.pool.checkedout() == 0


def test_a_repeatable_read_keeps_its_snapshot_and_the_next_read_reads_committed(engine, writer, table):
    with request_connection(engine):
        with engine.connect() as connection:
            snapshot = connection.execution_options(isolation_level="REPEATABLE READ")
            before = snapshot.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            with writer.begin() as other:
                other.execute(text(f"INSERT INTO {table} VALUES (1)"))
            during = snapshot.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        with engine.connect() as connection:
            after = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()

    assert (before, during, after) == (0, 0, 1)
    assert isolation == "read committed"


def test_a_failed_read_leaves_the_next_read_its_answer(engine, checkouts):
    with request_connection(engine):
        with engine.connect() as connection, pytest.raises(DBAPIError):
            connection.execute(text("SELECT 1 / 0"))
        with engine.connect() as connection:
            answer = connection.execute(text("SELECT 7")).scalar_one()

    assert answer == 7
    assert len(checkouts) == 1


def test_a_read_opened_inside_another_reads_snapshot_takes_its_own(engine, writer, table):
    with request_connection(engine), engine.connect() as outer:
        snapshot = outer.execution_options(isolation_level="REPEATABLE READ")
        outer_backend, _ = _backend_and_transaction(snapshot)
        with writer.begin() as other:
            other.execute(text(f"INSERT INTO {table} VALUES (1)"))
        with engine.connect() as inner:
            inner_backend, _ = _backend_and_transaction(inner)
            inner_count = inner.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        outer_count = snapshot.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()

    assert inner_backend != outer_backend
    assert (inner_count, outer_count) == (1, 0)


def test_another_thread_never_reads_on_the_requests_connection(engine):
    backends: list[int] = []

    def read() -> None:
        with engine.connect() as connection:
            backends.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())

    with request_connection(engine):
        read()
        helper = threading.Thread(target=read)
        helper.start()
        helper.join()

    assert len(set(backends)) == 2


def test_a_lost_connection_is_replaced_for_the_next_read(engine, writer):
    with request_connection(engine):
        with engine.connect() as connection:
            lost = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            with writer.begin() as other:
                other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": lost})
            with pytest.raises(DBAPIError):
                connection.execute(text("SELECT 1"))
        with engine.connect() as connection:
            replacement = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

    assert replacement != lost
    assert engine.pool.checkedout() == 0
