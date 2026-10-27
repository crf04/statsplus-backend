"""Target model: an opponent paired with the Qualifiers a player has to meet.

A Target stores no defensive reading (ADR 0001).  It stores the opponent, the
conjunctive Qualifiers, and the user's optional note; the title is derived from
those Qualifiers on every read so it can never drift from the filter it names.

Per-account uniqueness reads the Qualifiers as a set rather than a sequence:
``qualifier_signature`` is the sorted, canonically formatted join of the
Qualifiers, so reordering them does not buy a second copy of the same Target.

A Qualifier is any rule a player must meet (ADR 0005): a Diet share, a Position
(one or more of Guard, Wing and Big) or an Average Qualifier on a season stat
average.  The ``base`` says which; the other columns are null where the kind
does not use them.
"""

from types import MappingProxyType

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from ..domain.player_diet_taxonomy import PLAYER_DIET_SLICE_LABELS
from ..domain.player_position import POSITION_GROUPS
from . import Base

#: The ``base`` values of the two Qualifier kinds that are not a Diet share.
POSITION_QUALIFIER_BASE = "position"
SEASON_AVERAGE_QUALIFIER_BASE = "season_average"

# The stored width and the accepted width are the same limit; the service
# validates against these so a column can never quietly outgrow its check.
TARGET_NOTE_MAX_LENGTH = 280
TARGET_QUALIFIER_SIGNATURE_MAX_LENGTH = 2048

#: How each comparator reads in a derived title.  Only title rendering touches
#: this; validation names ``TARGET_COMPARATORS`` below, so adding a symbol is
#: not by itself enough to widen what a Qualifier may say.
TARGET_COMPARATOR_SYMBOLS = MappingProxyType(
    {
        "at_or_above": "≥",
        "at_or_below": "≤",
    }
)

#: The comparator vocabulary a Qualifier may use.  Every entry needs a symbol
#: above and a test below, or it can be accepted on write and then fail on the
#: read that evaluates it.
TARGET_COMPARATORS: tuple[str, ...] = tuple(TARGET_COMPARATOR_SYMBOLS)

#: How each comparator reads as a test on a stored share.  Both bounds are
#: inclusive: a Qualifier names a threshold a player is allowed to sit on.
#: Resolution evaluates every Qualifier through this table, so the comparator
#: a Target stores and the comparison it gets are one vocabulary.
TARGET_COMPARATOR_TESTS = MappingProxyType(
    {
        "at_or_above": lambda share, threshold: share >= threshold,
        "at_or_below": lambda share, threshold: share <= threshold,
    }
)


def _share_as_percentage(threshold: float) -> str:
    """Render a 0-1 share as a percentage, dropping a trailing ``.0``."""

    percentage = round(threshold * 100, 1)
    if percentage == int(percentage):
        return str(int(percentage))
    return f"{percentage:.1f}"


def _average_threshold(threshold: float) -> str:
    """Render an Average threshold at its own precision, without trailing zeros."""

    return f"{float(threshold):.10f}".rstrip("0").rstrip(".")


def _qualifier_clause(qualifier) -> str:
    base = qualifier["base"]
    if base == POSITION_QUALIFIER_BASE:
        return "/".join(qualifier["groups"])
    symbol = TARGET_COMPARATOR_SYMBOLS[qualifier["comparator"]]
    if base == SEASON_AVERAGE_QUALIFIER_BASE:
        return f"{qualifier['stat']} avg {symbol} {_average_threshold(qualifier['threshold'])}"
    return (
        f"{PLAYER_DIET_SLICE_LABELS[qualifier['slice_key']]} "
        f"{symbol} {_share_as_percentage(qualifier['threshold'])}%"
    )


def derive_target_title(opponent, qualifiers) -> str:
    """Build the title a Target reads as, from its opponent and Qualifiers.

    The Position reads first, then the other Qualifiers in the order the user
    entered them.  A Diet Qualifier reads as its slice's display label rather
    than the stored provider key, so ``PRBallHandler`` renders as ``P&R ball
    handler``.  The labels come from the diet taxonomy, which is the one backend
    source the frontend shares, so the two cannot drift.
    """

    ordered = sorted(qualifiers, key=lambda qualifier: qualifier["base"] != POSITION_QUALIFIER_BASE)
    clauses = ", ".join(_qualifier_clause(qualifier) for qualifier in ordered)
    return f"{opponent} vs {clauses}"


def target_qualifier_part(qualifier) -> str:
    """Return the canonical identity of one Qualifier.

    This is the single definition of when two Qualifiers are the same one: it
    is both a field of the stored signature and what the in-request repeat
    check compares, so the duplicate rule cannot mean one thing within a
    request and another across them.  A Diet threshold is formatted to a fixed
    width so ``0.4`` and ``0.40`` are one value; an Average threshold is
    compared to two decimals, and Position groups as a set.
    """

    base = qualifier["base"]
    if base == POSITION_QUALIFIER_BASE:
        groups = sorted(set(qualifier["groups"]), key=POSITION_GROUPS.index)
        return f"{base}:{'+'.join(groups)}"
    if base == SEASON_AVERAGE_QUALIFIER_BASE:
        return f"{base}:{qualifier['stat']}:{qualifier['comparator']}:" f"{float(qualifier['threshold']):.2f}"
    return (
        f"{qualifier['base']}:{qualifier['slice_key']}:"
        f"{qualifier['comparator']}:{float(qualifier['threshold']):.6f}"
    )


def target_qualifier_signature(qualifiers) -> str:
    """Return the order-insensitive identity of a set of Qualifiers.

    The parts are sorted so the caller's ordering cannot produce two
    signatures for the same set.
    """

    return "|".join(sorted(target_qualifier_part(qualifier) for qualifier in qualifiers))


class Target(Base):
    """An account-private opponent-plus-Qualifiers record."""

    __tablename__ = "targets"

    id = Column(Integer, primary_key=True, autoincrement=True)

    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
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
    conditions = Column(JSON, nullable=True)
    stat_preferences = Column(JSON, nullable=True)
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
        "TargetQualifier",
        back_populates="target",
        order_by="TargetQualifier.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        # Newest-first listing is the only read pattern.
        Index("idx_targets_owner_created", "firebase_uid", "created_at"),
        # The per-account duplicate rule, enforced by the database rather than
        # only by the service's pre-check.
        Index(
            "uq_targets_owner_opponent_signature",
            "firebase_uid",
            "opponent",
            "qualifier_signature",
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
        return f"<Target(id={self.id}, firebase_uid='{self.firebase_uid}', " f"opponent='{self.opponent}')>"

    def to_dict(self):
        """Convert to the public JSON shape used by the user API."""
        return {
            "id": self.id,
            "opponent": self.opponent,
            "title": self.title,
            "note": self.note,
            "conditions": self.conditions,
            "stat_preferences": self.stat_preferences,
            "qualifiers": [qualifier.to_dict() for qualifier in self.qualifiers],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TargetQualifier(Base):
    """One conjunctive condition on a player's diet share within a Target."""

    __tablename__ = "target_qualifiers"

    id = Column(Integer, primary_key=True, autoincrement=True)

    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
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
        nullable=True,
        comment="A slice of that base, e.g. Corner 3; null for Position and Average",
    )
    comparator = Column(
        String(16),
        nullable=True,
        comment="at_or_above or at_or_below; null for Position",
    )
    threshold = Column(
        Float,
        nullable=True,
        comment=(
            "Diet: the share the player must meet, 0-1 inclusive.  Average: the "
            "season average, 0 or more.  Null for Position"
        ),
    )
    stat = Column(
        "average_stat",
        String(16),
        nullable=True,
        comment="Average Qualifier: the Target stat column it reads, e.g. 3PA/36",
    )
    groups = Column(
        "position_groups",
        JSON,
        nullable=True,
        comment="Position Qualifier: the Guard/Wing/Big groups it names, in that order",
    )

    target = relationship("Target", back_populates="qualifiers")

    __table_args__ = (
        # Qualifiers are only ever read as one target's ordered set.
        Index("idx_target_qualifiers_target", "target_id", "position"),
    )

    def __repr__(self):
        return (
            f"<TargetQualifier(target_id={self.target_id}, base='{self.base}', "
            f"slice_key='{self.slice_key}', comparator='{self.comparator}', "
            f"threshold={self.threshold})>"
        )

    def to_dict(self):
        """Convert to the public JSON shape used by the user API."""
        if self.base == POSITION_QUALIFIER_BASE:
            return {"base": self.base, "groups": list(self.groups or ())}
        if self.base == SEASON_AVERAGE_QUALIFIER_BASE:
            return {
                "base": self.base,
                "stat": self.stat,
                "comparator": self.comparator,
                "threshold": self.threshold,
            }
        return {
            "base": self.base,
            "slice_key": self.slice_key,
            "comparator": self.comparator,
            "threshold": self.threshold,
        }
