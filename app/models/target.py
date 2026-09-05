"""Target model: an opponent paired with the Qualifiers a player has to meet.

A Target stores no defensive reading (ADR 0001).  It stores the opponent, the
conjunctive Qualifiers, and the user's optional note; the title is derived from
those Qualifiers on every read so it can never drift from the filter it names.

Per-account uniqueness reads the Qualifiers as a set rather than a sequence:
``qualifier_signature`` is the sorted, canonically formatted join of the
Qualifiers, so reordering them does not buy a second copy of the same Target.
"""

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from . import Base


# The stored width and the accepted width are the same limit; the service
# validates against these so a column can never quietly outgrow its check.
TARGET_NOTE_MAX_LENGTH = 280
TARGET_QUALIFIER_SIGNATURE_MAX_LENGTH = 2048

#: The comparator vocabulary, and the symbol each one reads as in a title.
TARGET_COMPARATOR_SYMBOLS = {
    'at_or_above': '≥',
    'at_or_below': '≤',
}


def _share_as_percentage(threshold: float) -> str:
    """Render a 0-1 share as a percentage, dropping a trailing ``.0``."""

    percentage = round(threshold * 100, 1)
    if percentage == int(percentage):
        return str(int(percentage))
    return f"{percentage:.1f}"


def derive_target_title(opponent, qualifiers) -> str:
    """Build the title a Target reads as, from its opponent and Qualifiers.

    Each Qualifier reads as its slice's display label rather than the stored
    provider key, so ``PRBallHandler`` renders as ``P&R ball handler``.  The
    labels come from the diet catalogue, which is the one backend source the
    frontend shares, so the two cannot drift.

    Imported inside the function: ``app.services.player_diet`` imports the
    model package, so a module-level import here would close that cycle.
    """

    from app.services.player_diet import PLAYER_DIET_SLICE_LABELS

    clauses = ', '.join(
        f"{PLAYER_DIET_SLICE_LABELS[qualifier['slice_key']]} "
        f"{TARGET_COMPARATOR_SYMBOLS[qualifier['comparator']]} "
        f"{_share_as_percentage(qualifier['threshold'])}%"
        for qualifier in qualifiers
    )
    return f"{opponent} vs {clauses}"


def target_qualifier_signature(qualifiers) -> str:
    """Return the order-insensitive identity of a set of Qualifiers.

    The threshold is formatted to a fixed width so ``0.4`` and ``0.40`` are one
    value, and the parts are sorted so the caller's ordering cannot produce two
    signatures for the same set.
    """

    return '|'.join(sorted(
        f"{qualifier['base']}:{qualifier['slice_key']}:"
        f"{qualifier['comparator']}:{float(qualifier['threshold']):.6f}"
        for qualifier in qualifiers
    ))


class Target(Base):
    """An account-private opponent-plus-Qualifiers record."""

    __tablename__ = 'targets'

    id = Column(Integer, primary_key=True, autoincrement=True)

    firebase_uid = Column(
        String(128),
        ForeignKey('users.firebase_uid', ondelete='CASCADE'),
        nullable=False,
        comment="Owning account; targets are never shared between accounts",
    )
    opponent = Column(
        String(3),
        nullable=False,
        comment="Canonical NBA tricode of the opponent the target is aimed at",
    )
    note = Column(
        String(TARGET_NOTE_MAX_LENGTH),
        nullable=True,
        comment="Optional user note; never part of the derived title",
    )
    qualifier_signature = Column(
        String(TARGET_QUALIFIER_SIGNATURE_MAX_LENGTH),
        nullable=False,
        comment="Order-insensitive identity of the qualifier set",
    )

    # No server default: like every sibling model, the writing service supplies
    # both timestamps, so there is one clock rather than two.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the target was created",
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the target's qualifiers or note last changed",
    )

    qualifiers = relationship(
        'TargetQualifier',
        back_populates='target',
        order_by='TargetQualifier.position',
        cascade='all, delete-orphan',
        passive_deletes=True,
        lazy='selectin',
    )

    __table_args__ = (
        # Newest-first listing is the only read pattern.
        Index('idx_targets_owner_created', 'firebase_uid', 'created_at'),
        # The per-account duplicate rule, enforced by the database rather than
        # only by the service's pre-check.
        Index(
            'uq_targets_owner_opponent_signature',
            'firebase_uid',
            'opponent',
            'qualifier_signature',
            unique=True,
        ),
    )

    @property
    def title(self) -> str:
        """The derived title; never stored, never user-set (ADR 0001)."""

        return derive_target_title(
            self.opponent,
            [qualifier.to_dict() for qualifier in self.qualifiers],
        )

    def __repr__(self):
        return (
            f"<Target(id={self.id}, firebase_uid='{self.firebase_uid}', "
            f"opponent='{self.opponent}')>"
        )

    def to_dict(self):
        """Convert to the public JSON shape used by the user API."""
        return {
            'id': self.id,
            'opponent': self.opponent,
            'title': self.title,
            'note': self.note,
            'qualifiers': [
                qualifier.to_dict() for qualifier in self.qualifiers
            ],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TargetQualifier(Base):
    """One conjunctive condition on a player's diet share within a Target."""

    __tablename__ = 'target_qualifiers'

    id = Column(Integer, primary_key=True, autoincrement=True)

    target_id = Column(
        Integer,
        ForeignKey('targets.id', ondelete='CASCADE'),
        nullable=False,
        comment="Owning target",
    )
    position = Column(
        Integer,
        nullable=False,
        comment="The order the author entered this qualifier in",
    )
    base = Column(
        String(32),
        nullable=False,
        comment="Player diet base, e.g. shot_zones",
    )
    slice_key = Column(
        String(64),
        nullable=False,
        comment="A slice of that base, e.g. Corner 3",
    )
    comparator = Column(
        String(16),
        nullable=False,
        comment="at_or_above or at_or_below",
    )
    threshold = Column(
        Float,
        nullable=False,
        comment="Share of the base the player must meet, 0-1 inclusive",
    )

    target = relationship('Target', back_populates='qualifiers')

    __table_args__ = (
        # Qualifiers are only ever read as one target's ordered set.
        Index('idx_target_qualifiers_target', 'target_id', 'position'),
    )

    def __repr__(self):
        return (
            f"<TargetQualifier(target_id={self.target_id}, "
            f"slice_key='{self.slice_key}', comparator='{self.comparator}', "
            f"threshold={self.threshold})>"
        )

    def to_dict(self):
        """Convert to the public JSON shape used by the user API."""
        return {
            'base': self.base,
            'slice_key': self.slice_key,
            'comparator': self.comparator,
            'threshold': self.threshold,
        }
