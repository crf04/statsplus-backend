"""Operator-controlled application settings stored in the database.

A database row, not an environment variable, so an admin can change a setting
without a deploy and every worker sees the change.
"""

from sqlalchemy import Column, DateTime, String
from sqlalchemy.sql import func

from . import Base

SIGNUPS_OPEN_KEY = "signups_open"


class AppSetting(Base):
    """One named setting; an absent row means the setting's default."""

    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
