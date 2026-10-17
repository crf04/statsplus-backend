"""Research activity counts and account cohorts (crf04/statsplus#213).

``research_activity_days`` keeps one count per account per US Eastern calendar
date: how many qualifying research reads the account made that day.  It is a
tally, not an event log, so it carries no request detail and grows by at most
one row per active account per day.

``account_cohorts`` tags the accounts that are not outside the founder's
circle.  An untagged account has no row and is treated as outside.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)

from . import Base

COHORT_INTERNAL = "internal"
COHORT_FRIEND = "friend"
COHORT_FRIEND_OF_FRIEND = "friend_of_friend"

#: The tags an admin may set; clearing the tag makes the account outside.
COHORTS = (COHORT_INTERNAL, COHORT_FRIEND, COHORT_FRIEND_OF_FRIEND)


class ResearchActivityDay(Base):
    """How many research reads one account made on one Eastern date."""

    __tablename__ = "research_activity_days"

    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
        primary_key=True,
        comment="Account that made the reads",
    )
    game_day = Column(
        Date,
        primary_key=True,
        comment="US Eastern calendar date of the reads",
    )
    actions = Column(
        Integer,
        nullable=False,
        comment="Qualifying research reads that day; a row exists only if >= 1",
    )

    __table_args__ = (
        CheckConstraint("actions >= 1", name="ck_research_activity_days_actions"),
        Index("idx_research_activity_days_game_day", "game_day"),
    )

    def __repr__(self):
        return (
            f"<ResearchActivityDay(firebase_uid='{self.firebase_uid}', "
            f"game_day='{self.game_day}', actions={self.actions})>"
        )


class AccountCohort(Base):
    """An admin's tag for an account outside the goal's plain 'outside' set."""

    __tablename__ = "account_cohorts"

    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
        primary_key=True,
        comment="Tagged account; at most one tag per account",
    )
    cohort = Column(String(32), nullable=False, comment="internal, friend or friend_of_friend")
    # No server default: the writing service supplies it, like sibling models.
    tagged_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When an admin last set the tag",
    )

    __table_args__ = (
        CheckConstraint(
            "cohort IN ('internal', 'friend', 'friend_of_friend')",
            name="ck_account_cohorts_cohort",
        ),
    )

    def __repr__(self):
        return f"<AccountCohort(firebase_uid='{self.firebase_uid}', cohort='{self.cohort}')>"
