"""Serve one request's composed reads on one pooled connection."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


@contextmanager
def request_read_scope(
    engine: Engine | None,
    *,
    repeatable_read: bool = False,
) -> Iterator[tuple[Connection | None, Session | None]]:
    """Open the one connection a request's reads share, plus its session.

    Every read a route composes otherwise checks out its own pooled
    connection and returns it with a reset ``ROLLBACK``, which is a round trip
    per repository call.  ``None`` keeps wirings without an engine -- the demo
    database and the unit tests that inject fake collaborators -- on the
    per-call default.

    ``repeatable_read`` reads every statement from the snapshot the first one
    takes on PostgreSQL, so a composition or prune that commits after the
    capture cannot empty the request's later projection reads.  Only a scope
    that never writes asks for it.
    """

    if engine is None:
        yield None, None
        return
    with engine.connect() as connection:
        if repeatable_read and connection.dialect.name == "postgresql":
            connection.execution_options(isolation_level="REPEATABLE READ")
        # The publication snapshot reads through a Session; binding it to this
        # connection keeps its generation capture on the same checkout.
        session = Session(bind=connection)
        try:
            yield connection, session
        finally:
            session.close()


@contextmanager
def read_savepoint(
    read_scope: tuple[Connection | None, Session | None],
) -> Iterator[Callable[[], None]]:
    """A savepoint ``recover()`` returns a read scope's connection to after a read fails.

    A failed statement aborts a PostgreSQL transaction, so every later read on
    the request's connection would fail too.  A read scope never writes, so
    rolling back to one savepoint taken before all of the reads loses nothing
    and keeps the transaction's snapshot: one statement per request, plus two
    per failure, rather than two per read.  No connection has nothing to
    recover: each read checks out its own.
    """

    connection, session = read_scope
    if connection is None:
        yield lambda: None
        return
    # The session joins the transaction now, without a statement: joining
    # inside the savepoint would nest one of its own there, which the
    # rollback destroys before the session closes it.
    if session is not None:
        session.connection()
    savepoint = connection.begin_nested()

    def recover() -> None:
        nonlocal savepoint
        # A connection that cannot roll back is lost: the reads after this one
        # fail on it, each on its own, as they would without the savepoint.
        with suppress(SQLAlchemyError):
            savepoint.rollback()
            savepoint = connection.begin_nested()

    yield recover


@contextmanager
def read_connection(engine: Engine, connection: Connection | None) -> Iterator[Connection]:
    """Reuse the request's connection, else check one out for this read."""

    if connection is not None:
        yield connection
        return
    with engine.connect() as owned:
        yield owned


__all__ = ["read_connection", "read_savepoint", "request_read_scope"]
