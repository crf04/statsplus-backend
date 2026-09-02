"""Database utilities for creating and reusing a SQLAlchemy engine.

This centralizes environment-driven database configuration so deployments can
switch between SQLite (default) and managed databases like Postgres on Railway
without code changes elsewhere.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError

from app.config.settings import DatabaseSettings, RuntimeSettings, get_runtime_settings


DEFAULT_SQLITE_PATH: Final[str] = "sqlite:///nba_play_types.db"
DEMO_DATABASE_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "nba_play_types.db"


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """Enforce declared foreign keys on every SQLite connection.

    SQLite parses ``REFERENCES`` clauses but ignores them unless the pragma is
    set, and the pragma is per connection rather than per database file, so a
    schema that declares ``ON DELETE CASCADE`` -- migration 007's contradiction
    rows beside a decision -- silently keeps orphans without this.  Referential
    integrity is a property of the schema rather than of one caller's engine,
    so the listener is registered on the ``Engine`` class: every engine in the
    process gets it, including the ones tests and scripts build directly.

    PostgreSQL enforces its own constraints and needs no pragma, so the
    listener recognizes the SQLite driver connection by type and does nothing
    at all for any other backend.
    """

    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _normalize_database_url(database_url: str) -> str:
    """Normalize a database URL for SQLAlchemy.

    Converts legacy postgres scheme (postgres://) to the recommended
    postgresql+psycopg2 scheme for SQLAlchemy if needed.

    Parameters
    ----------
    database_url: str
        The raw database URL from environment variables.

    Returns
    -------
    str
        A SQLAlchemy-compatible database URL.
    """

    if database_url.startswith("postgres://"):
        # Railway and other providers may emit `postgres://` URLs
        return database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgresql://") and "+" not in database_url:
        # Prefer explicit driver
        return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return database_url


def is_demo_database_url(database_url: str) -> bool:
    """Return whether ``database_url`` points at the tracked demo fixture.

    The fixture is a read-only data source, not an application migration
    target.  Resolve relative SQLite paths from the current working directory
    because that is also how SQLAlchemy resolves the default URL, and resolve
    symlinks so an alias cannot bypass the read-only boundary.
    """
    try:
        parsed_url = make_url(database_url)
    except (ArgumentError, TypeError):
        return False

    if parsed_url.get_backend_name() != "sqlite" or not parsed_url.database:
        return False

    database_path = parsed_url.database
    if database_path.startswith("file:"):
        database_path = database_path.removeprefix("file:")
    if database_path == ":memory:":
        return False

    return Path(database_path).expanduser().resolve() == DEMO_DATABASE_PATH.resolve()


@lru_cache(maxsize=8)
def _create_engine(
    database_url: str, pool: DatabaseSettings | None = None
) -> Engine:
    """Create and cache an engine for one normalized URL and pool config.

    ``pool`` is applied only to Postgres engines; SQLite engines are built
    with no extra keyword arguments. ``DatabaseSettings`` is frozen, so it is
    hashable and takes part in the cache key.
    """

    if pool is None:
        return create_engine(database_url)
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool.pool_size,
        max_overflow=pool.max_overflow,
        pool_recycle=pool.pool_recycle_seconds,
        connect_args={"connect_timeout": pool.connect_timeout_seconds},
    )


def get_engine(settings: RuntimeSettings | None = None) -> Engine:
    """Create and cache the global SQLAlchemy engine.

    The engine is created from the validated runtime settings object. The
    bundled SQLite database remains the safe local default, and engines are
    memoized by normalized URL and pool configuration so imports across
    modules share a pool. Postgres engines apply the configured connection
    pool (pre-ping, size, overflow, recycle, and connect timeout); SQLite
    engines are built exactly as before, with no extra keyword arguments.

    Returns
    -------
    Engine
        A configured SQLAlchemy engine.
    """

    runtime_settings = settings or get_runtime_settings()
    database_settings = runtime_settings.database
    normalized_url = _normalize_database_url(database_settings.url)
    if make_url(normalized_url).get_backend_name() != "postgresql":
        return _create_engine(normalized_url)
    return _create_engine(normalized_url, database_settings)


# Preserve the small cache-control surface callers historically got from the
# lru_cache-decorated function.
get_engine.cache_clear = _create_engine.cache_clear  # type: ignore[attr-defined]
