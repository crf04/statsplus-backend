"""Serve one request's composed reads on one pooled connection."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session


@contextmanager
def request_read_scope(
    engine: Engine | None,
) -> Iterator[tuple[Connection | None, Session | None]]:
    """Open the one connection a request's reads share, plus its session.

    Every read a route composes otherwise checks out its own pooled
    connection and returns it with a reset ``ROLLBACK``, which is a round trip
    per repository call.  ``None`` keeps wirings without an engine -- the demo
    database and the unit tests that inject fake collaborators -- on the
    per-call default.
    """

    if engine is None:
        yield None, None
        return
    with engine.connect() as connection:
        # The publication snapshot reads through a Session; binding it to this
        # connection keeps its generation capture on the same checkout.
        session = Session(bind=connection)
        try:
            yield connection, session
        finally:
            session.close()


@contextmanager
def read_connection(
    engine: Engine, connection: Connection | None
) -> Iterator[Connection]:
    """Reuse the request's connection, else check one out for this read."""

    if connection is not None:
        yield connection
        return
    with engine.connect() as owned:
        yield owned


__all__ = ["read_connection", "request_read_scope"]
