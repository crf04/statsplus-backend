"""Closing sportsbook lines and the ledger of what was bought for them (#433).

``closing_lines`` holds the line the game logs serve: one row per game, player,
market and source book, written once by ``scripts/backfill_closing_lines.py``.
``closing_line_purchases`` is the backfill's ledger, one row per game and
market.  It is written the moment a paid response arrives, before anything is
parsed, so a crash or a rerun never buys a recorded game twice.
``closing_line_spend`` logs what each historical events list cost, and calls
that may have been charged but returned nothing, so the spend cap counts the
whole campaign and not only the games.

Both are separate from the live sportsbook store (``sportsbook_odds``): a
closing line here is bought once from the historical endpoints and never
refreshed.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)

from . import Base

#: ``failed``: the call returned no usable body and cost nothing, so a rerun
#: tries again.  ``received``: a body arrived and its cost is recorded, nothing
#: parsed yet.  ``empty``: the snapshot posted no props (free).  ``purchased``:
#: the allowlisted quotes are stored on the row, lines not yet written.
#: ``complete``: the lines are written.
PURCHASE_OUTCOMES = ("failed", "received", "empty", "purchased", "complete")


class ClosingLine(Base):
    """One book's closing line for one player in one game."""

    __tablename__ = "closing_lines"

    canonical_game_id = Column(String(32), primary_key=True)
    canonical_player_id = Column(Integer, primary_key=True)
    #: Matchup market category (``PTS``); the Odds API market is
    #: ``player_points``.
    market_category = Column(String(16), primary_key=True)
    #: The Odds API bookmaker key (``draftkings``, ``fanduel``, ...).
    book = Column(String(32), primary_key=True)
    line = Column(Numeric(5, 1, asdecimal=False), nullable=False)
    #: The timestamp of the historical snapshot the line was read from.
    snapshot_at = Column(DateTime(timezone=True), nullable=False)
    odds_event_id = Column(String(64), nullable=False)
    bought_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_closing_lines_player",
            "canonical_player_id",
            "market_category",
            "canonical_game_id",
        ),
    )


class ClosingLinePurchase(Base):
    """What one historical event-odds call cost and returned."""

    __tablename__ = "closing_line_purchases"

    canonical_game_id = Column(String(32), primary_key=True)
    market_category = Column(String(16), primary_key=True)
    odds_event_id = Column(String(64), nullable=False)
    #: The ``date`` the call asked for: the last 5-minute mark before tip.
    requested_for = Column(DateTime(timezone=True), nullable=False)
    outcome = Column(String(16), nullable=False)
    http_status = Column(Integer, nullable=True)
    #: ``x-requests-last`` of the call.
    credits = Column(Integer, nullable=True)
    #: ``x-requests-remaining`` after the call.
    remaining = Column(Integer, nullable=True)
    #: The snapshot the API actually returned (its ``timestamp``).
    snapshot_at = Column(DateTime(timezone=True), nullable=True)
    bought_at = Column(DateTime(timezone=True), nullable=False)
    failure_reason = Column(String(64), nullable=True)
    #: The allowlisted ``player_points`` quotes of the response as compact
    #: JSON (``[[book, player name, line], ...]``), kept so lines can be
    #: written or re-resolved without buying the game again.  The raw body is
    #: never stored.
    quotes = Column(Text, nullable=True)
    unresolved_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "outcome IN (" + ", ".join(f"'{name}'" for name in PURCHASE_OUTCOMES) + ")",
            name="ck_closing_line_purchases_outcome",
        ),
    )


class ClosingLineSpend(Base):
    """Credits spent outside a game's ledger row, so the cap can count them."""

    __tablename__ = "closing_line_spend"

    spend_id = Column(Integer, primary_key=True, autoincrement=True)
    #: ``events_list``: a historical events list (its own cost).  ``lost_call``:
    #: a call that may have been charged but returned nothing usable (a
    #: timeout), counted at the documented price.
    kind = Column(String(16), nullable=False)
    #: The US-Eastern game day of an events list, or the game id of a lost
    #: game call.
    subject = Column(String(32), nullable=False)
    credits = Column(Integer, nullable=False)
    remaining = Column(Integer, nullable=True)
    #: Events in a readable list; ``NULL`` otherwise.
    events_listed = Column(Integer, nullable=True)
    spent_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('events_list', 'lost_call')", name="ck_closing_line_spend_kind"),
    )


__all__ = [
    "PURCHASE_OUTCOMES",
    "ClosingLine",
    "ClosingLinePurchase",
    "ClosingLineSpend",
]
