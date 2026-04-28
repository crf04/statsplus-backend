"""
SQLAlchemy models for NBA Game Logs application.

This module sets up the SQLAlchemy declarative base and provides
the foundation for ORM models.
"""

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.utils.db import get_engine
import logging

logger = logging.getLogger(__name__)

# Create the declarative base for all models
Base = declarative_base()

def get_session():
    """
    Create a new SQLAlchemy session.
    
    Returns:
        Session: A new SQLAlchemy session bound to the application engine
    """
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()

def create_all_tables():
    """
    Create all tables defined by models that inherit from Base.
    This will only create tables that don't already exist.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("Database tables are ready")

# Import all models here to ensure they're registered with Base.metadata
from .user import User  # noqa: E402

__all__ = ['Base', 'get_session', 'create_all_tables', 'User']
