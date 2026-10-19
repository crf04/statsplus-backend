"""Usage events (crf04/statsplus#261).

One append-only table records what accounts do: a daily ``active`` row per
source, four daily-deduplicated web actions, and one ``mcp_tool_call`` row per
connector tool call.  The backend writes it; nothing is backfilled.  The
``day`` column is the US Eastern date of ``occurred_at``, stored so the report
can index on it.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    text,
)

from . import Base

SOURCE_WEB = "web"
SOURCE_MCP = "mcp"
SOURCES = (SOURCE_WEB, SOURCE_MCP)

EVENT_ACTIVE = "active"
EVENT_MATCHUP_VIEWED = "matchup_viewed"
EVENT_GAME_LOGS_QUERIED = "game_logs_queried"
EVENT_QUERY_PROMPT_PARSED = "query_prompt_parsed"
EVENT_BACKTEST_VIEWED = "backtest_viewed"
EVENT_MCP_TOOL_CALL = "mcp_tool_call"

#: The web actions, which (like ``active``) record at most once per day.
WEB_ACTIONS = (
    EVENT_MATCHUP_VIEWED,
    EVENT_GAME_LOGS_QUERIED,
    EVENT_QUERY_PROMPT_PARSED,
    EVENT_BACKTEST_VIEWED,
)
EVENT_NAMES = (EVENT_ACTIVE, *WEB_ACTIONS, EVENT_MCP_TOOL_CALL)

OUTCOMES = ("ok", "error", "limit", "needs_choice")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


_DAILY_ONLY = f"name <> '{EVENT_MCP_TOOL_CALL}'"


class UsageEvent(Base):
    """One thing an account did, on one Eastern day, from one source."""

    __tablename__ = "usage_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), Identity(), primary_key=True)
    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
        nullable=False,
        comment="Account that did it",
    )
    occurred_at = Column(DateTime(timezone=True), nullable=False, comment="When it was recorded")
    day = Column(Date, nullable=False, comment="America/New_York date of occurred_at")
    source = Column(String(8), nullable=False, comment="web or mcp")
    name = Column(String(32), nullable=False, comment="active, a web action or mcp_tool_call")
    tool = Column(String(64), comment="MCP tool name; mcp_tool_call only")
    outcome = Column(String(16), comment="ok, error, limit or needs_choice; mcp_tool_call only")
    latency_ms = Column(Integer, comment="End-to-end tool latency; mcp_tool_call only")

    __table_args__ = (
        CheckConstraint(_in_list("source", SOURCES), name="ck_usage_events_source"),
        CheckConstraint(_in_list("name", EVENT_NAMES), name="ck_usage_events_name"),
        CheckConstraint(
            f"outcome IS NULL OR {_in_list('outcome', OUTCOMES)}",
            name="ck_usage_events_outcome",
        ),
        # Makes every daily event idempotent; tool calls repeat freely.
        Index(
            "uq_usage_events_daily",
            "firebase_uid",
            "day",
            "source",
            "name",
            unique=True,
            sqlite_where=text(_DAILY_ONLY),
            postgresql_where=text(_DAILY_ONLY),
        ),
        Index("idx_usage_events_day_name", "day", "name"),
    )

    def __repr__(self):
        return f"<UsageEvent(firebase_uid='{self.firebase_uid}', day='{self.day}', source='{self.source}', name='{self.name}')>"
