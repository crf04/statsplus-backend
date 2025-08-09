"""Database utilities for creating and reusing a SQLAlchemy engine.

This centralizes environment-driven database configuration so deployments can
switch between SQLite (default) and managed databases like Postgres on Railway
without code changes elsewhere.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Final

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


DEFAULT_SQLITE_PATH: Final[str] = "sqlite:///nba_play_types.db"


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


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create and cache the global SQLAlchemy engine.

    The engine is created from the `DATABASE_URL` environment variable if set,
    otherwise falls back to the project-local SQLite database file. The engine
    is memoized so imports across modules share the same connection pool.

    Returns
    -------
    Engine
        A configured SQLAlchemy engine.
    """

    raw_url = os.getenv("DATABASE_URL", DEFAULT_SQLITE_PATH)
    normalized_url = _normalize_database_url(raw_url)
    return create_engine(normalized_url)

