"""
SQLAlchemy models for NBA Game Logs application.

This module sets up the SQLAlchemy declarative base and provides
the foundation for ORM models.
"""

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from app.config.settings import get_runtime_settings
from app.utils.db import get_engine, is_demo_database_url
import logging

logger = logging.getLogger(__name__)

# Create the declarative base for all models
Base = declarative_base()

def get_session(engine: Engine | None = None):
    """
    Create a new SQLAlchemy session.
    
    Returns:
        Session: A new SQLAlchemy session bound to the application engine
    """
    session_engine = engine or get_engine()
    Session = sessionmaker(bind=session_engine)
    return Session()

def create_all_tables():
    """
    Apply the current application schema migrations.

    The function name is retained for the app-factory compatibility seam;
    callers that need a repeatable workflow should use ``scripts/migrate.py``.
    """
    settings = get_runtime_settings()
    if is_demo_database_url(settings.database.url):
        logger.info("Skipping application migrations for the read-only demo database")
        return

    engine = get_engine(settings)
    from app.migrations import run_migrations

    result = run_migrations(engine)
    logger.info(
        "Database tables are ready at schema version %s (applied: %s)",
        result.current_version,
        ", ".join(result.applied) or "none",
    )

# Import all models here to ensure they're registered with Base.metadata
from .user import User  # noqa: E402

__all__ = ['Base', 'get_session', 'create_all_tables', 'User']
