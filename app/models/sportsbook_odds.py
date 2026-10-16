"""Sportsbook player-prop odds store (#377, crf04/statsplus#185).

A separate store from the DFS projection archive: two-sided prices are native
columns, the book is a column rather than a provider, and nothing here feeds
DFS provenance, the Targets pool, or the DFS board.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)

from . import Base


class OddsApiPoll(Base):
    """One attempted ``/events/{id}/odds`` call and what it cost."""

    __tablename__ = "odds_api_polls"

    poll_id = Column(Integer, primary_key=True, autoincrement=True)
    odds_event_id = Column(String(64), nullable=False)
    canonical_game_id = Column(String(32), nullable=False)
    #: The tip this poll was scheduled against.  A closing poll only closes
    #: the game while the game still starts then; a postponement reopens it.
    commence_time = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    #: ``complete`` (quotes stored: in ``sportsbook_prop_*`` for the ``us``
    #: feed, in the projection archive for ``us_dfs``), ``empty`` (props not
    #: posted), ``failed``.
    outcome = Column(String(16), nullable=False)
    cost = Column(Integer, nullable=True)
    remaining = Column(Integer, nullable=True)
    is_closing = Column(Boolean, nullable=False, default=False, server_default="0")
    failure_reason = Column(String(64), nullable=True)
    quote_count = Column(Integer, nullable=False, default=0, server_default="0")
    unresolved_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_odds_api_polls_event_started", "odds_event_id", "started_at"),
    )


class SportsbookPropQuote(Base):
    """History: a row per change of a book's line or price, plus closings."""

    __tablename__ = "sportsbook_prop_quotes"

    quote_id = Column(Integer, primary_key=True, autoincrement=True)
    poll_id = Column(Integer, nullable=False)
    canonical_game_id = Column(String(32), nullable=False)
    canonical_player_id = Column(Integer, nullable=False)
    market_category = Column(String(16), nullable=False)
    book = Column(String(32), nullable=False)
    line = Column(Numeric(5, 1, asdecimal=False), nullable=False)
    over_price = Column(Integer, nullable=True)
    under_price = Column(Integer, nullable=True)
    market_last_update = Column(DateTime(timezone=True), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    is_closing = Column(Boolean, nullable=False, default=False, server_default="0")

    __table_args__ = (
        Index(
            "ix_sportsbook_prop_quotes_series",
            "canonical_game_id",
            "canonical_player_id",
            "market_category",
            "book",
            "observed_at",
        ),
    )


class SportsbookPropLatest(Base):
    """The row per book, player, and market that the Matchup read serves."""

    __tablename__ = "sportsbook_prop_latest"

    canonical_game_id = Column(String(32), primary_key=True)
    canonical_player_id = Column(Integer, primary_key=True)
    market_category = Column(String(16), primary_key=True)
    book = Column(String(32), primary_key=True)
    poll_id = Column(Integer, nullable=False)
    line = Column(Numeric(5, 1, asdecimal=False), nullable=False)
    over_price = Column(Integer, nullable=True)
    under_price = Column(Integer, nullable=True)
    market_last_update = Column(DateTime(timezone=True), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    #: Whether this row came from the game's closing snapshot.  The closing
    #: poll is the last paid call, so after tip the latest rows *are* the
    #: closing set; a missed closing fetch leaves them unmarked.
    is_closing = Column(Boolean, nullable=False, default=False, server_default="0")


class OddsApiQuotaState(Base):
    """Singleton: last-seen quota, guard tier, and failure backoff."""

    __tablename__ = "odds_api_quota_state"

    state_key = Column(String(16), primary_key=True)
    remaining = Column(Integer, nullable=True)
    used = Column(Integer, nullable=True)
    last_cost = Column(Integer, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    tier = Column(String(16), nullable=False, default="normal", server_default="normal")
    tier_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    reset_day = Column(Integer, nullable=False, default=1, server_default="1")
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    backoff_until = Column(DateTime(timezone=True), nullable=True)
    last_failure_reason = Column(Text, nullable=True)
    unresolved_total = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime(timezone=True), nullable=False)


__all__ = [
    "OddsApiPoll",
    "OddsApiQuotaState",
    "SportsbookPropLatest",
    "SportsbookPropQuote",
]
