"""Saved Filter Set model: an account-private bookmark of a Log Workspace URL.

A Saved Filter Set stores only a user-chosen name and the bare query string
that addresses a Log Workspace Filter Set.  No game-log data is retained, so
opening one simply replays the query string through the existing URL entry
path.
"""

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.sql import func

from . import Base


# The stored width and the accepted width are the same limit; the service
# validates against these so a column can never quietly outgrow its check.
SAVED_FILTER_SET_NAME_MAX_LENGTH = 100
SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH = 2048


class SavedFilterSet(Base):
    """A named, account-private Log Workspace query string."""

    __tablename__ = 'saved_filter_sets'

    id = Column(Integer, primary_key=True, autoincrement=True)

    firebase_uid = Column(
        String(128),
        ForeignKey('users.firebase_uid', ondelete='CASCADE'),
        nullable=False,
        comment="Owning account; saved sets are never shared between accounts",
    )
    name = Column(
        String(SAVED_FILTER_SET_NAME_MAX_LENGTH),
        nullable=False,
        comment="User-chosen label",
    )
    query_string = Column(
        String(SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH),
        nullable=False,
        comment="Bare Log Workspace URL query string, without a leading '?'",
    )

    # No server default: like every sibling model, the writing service supplies
    # both timestamps, so there is one clock rather than two.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the filter set was saved",
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the filter set was last renamed",
    )

    __table_args__ = (
        # Newest-first listing is the only read pattern.
        Index('idx_saved_filter_sets_owner_created', 'firebase_uid', 'created_at'),
        # Per-account, case-insensitive name uniqueness. A functional index is
        # used rather than a stored folded column so the exact name the user
        # typed is what comes back; SQLite and PostgreSQL both index
        # ``lower(name)``.
        Index(
            'uq_saved_filter_sets_owner_name',
            firebase_uid,
            func.lower(name),
            unique=True,
        ),
    )

    def __repr__(self):
        return (
            f"<SavedFilterSet(id={self.id}, firebase_uid='{self.firebase_uid}', "
            f"name='{self.name}')>"
        )

    def to_dict(self):
        """Convert to the public JSON shape used by the user API."""
        return {
            'id': self.id,
            'name': self.name,
            'query_string': self.query_string,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
