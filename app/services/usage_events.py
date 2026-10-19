"""Usage events: who is active, what they use, how the connector fares (#261).

The service writes the ``usage_events`` table and nothing else.  Every write is
quiet: a failure is logged at warning level and swallowed, because a
measurement must never fail the request it describes.

``active`` and the four web actions are daily events.  Each is an
insert-or-nothing against the table's partial unique index, fronted by a
process-local memo so a key already written today costs no query.  The memo key
carries the source, so web activity never hides the connector's own first
request of the day.  ``mcp_tool_call`` rows are not deduplicated.

Days are US Eastern calendar dates.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Final

from flask import request
from sqlalchemy import insert, text
from sqlalchemy.dialects import postgresql, sqlite

from app.domain.slate_time import slate_date_for_instant
from app.models import get_session
from app.models.usage_event import (
    EVENT_ACTIVE,
    EVENT_BACKTEST_VIEWED,
    EVENT_GAME_LOGS_QUERIED,
    EVENT_MATCHUP_VIEWED,
    EVENT_MCP_TOOL_CALL,
    EVENT_QUERY_PROMPT_PARSED,
    SOURCE_MCP,
    SOURCE_WEB,
    UsageEvent,
)

logger = logging.getLogger(__name__)

#: The request header the MCP server sends; only the exact value ``mcp`` counts.
CLIENT_HEADER: Final[str] = "X-CourtAI-Client"
MCP_CLIENT: Final[str] = "mcp"

#: ``(HTTP method, Flask endpoint)`` of each route behind a web action.  A test
#: proves every endpoint still names a real route.
WEB_ACTION_ENDPOINTS: Final[dict[tuple[str, str], str]] = {
    ("GET", "games.get_matchup"): EVENT_MATCHUP_VIEWED,
    ("GET", "games.get_game_logs"): EVENT_GAME_LOGS_QUERIED,
    ("POST", "nl.process_natural_language_query"): EVENT_QUERY_PROMPT_PARSED,
    ("GET", "users.backtest_target"): EVENT_BACKTEST_VIEWED,
    ("GET", "users.backtest_targets"): EVENT_BACKTEST_VIEWED,
}

#: Upper bound on a write's wait for a lock or slow statement (PostgreSQL only).
WRITE_STATEMENT_TIMEOUT_MS: Final[int] = 2000

_DAILY_INDEX_WHERE = text(f"name <> '{EVENT_MCP_TOOL_CALL}'")
_DAILY_INDEX_COLUMNS = ("firebase_uid", "day", "source", "name")


def request_source() -> str:
    """``mcp`` for the connector's exact header value; ``web`` for anything else."""

    return SOURCE_MCP if request.headers.get(CLIENT_HEADER) == MCP_CLIENT else SOURCE_WEB


class UsageEventService:
    """Records usage events for verified accounts."""

    def __init__(
        self,
        engine,
        *,
        recording: bool = True,
        clock: Callable[[], datetime] | None = None,
    ):
        """Bind to ``engine``.

        ``recording=False`` (the read-only demo database) makes every write a
        no-op.  ``clock`` returns the current timezone-aware instant and exists
        so tests can fix the date.
        """
        self.engine = engine
        self.recording = recording
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._written: set[tuple[date, str, str, str]] = set()

    # -- daily events ---------------------------------------------------------

    def now(self) -> datetime:
        """The current timezone-aware instant."""

        return self._clock()

    def record_daily(self, firebase_uid: str, source: str, name: str, at: datetime | None = None) -> None:
        """Record ``name`` once for ``firebase_uid`` on ``at``'s Eastern day (default now).

        A web action passes the instant its response was produced, not the
        later instant the deferred write runs.
        """

        if not self.recording:
            return
        try:
            now = at or self._clock()
            day = slate_date_for_instant(now)
            key = (day, firebase_uid, source, name)
            with self._lock:
                if key in self._written:
                    return
            self._insert_daily(firebase_uid, source, name, now, day)
            with self._lock:
                # Yesterday's keys can never match again.
                self._written = {kept for kept in self._written if kept[0] == day}
                self._written.add(key)
        except Exception as error:
            logger.warning("Could not record a %s usage event: %s", name, error)

    def _insert_daily(self, firebase_uid, source, name, now, day) -> None:
        session = get_session(self.engine)
        try:
            self._bound_the_wait(session)
            dialect = postgresql if session.get_bind().dialect.name == "postgresql" else sqlite
            statement = (
                dialect.insert(UsageEvent)
                .values(
                    firebase_uid=firebase_uid,
                    occurred_at=now,
                    day=day,
                    source=source,
                    name=name,
                )
                .on_conflict_do_nothing(
                    index_elements=list(_DAILY_INDEX_COLUMNS),
                    index_where=_DAILY_INDEX_WHERE,
                )
            )
            session.execute(statement)
            session.commit()
        finally:
            session.close()

    def record_active(self, firebase_uid: str) -> None:
        """Record today's ``active`` row for the current request's source."""

        self.record_daily(firebase_uid, request_source(), EVENT_ACTIVE)

    # -- MCP tool calls -------------------------------------------------------

    def record_tool_call(self, firebase_uid: str, tool: str, outcome: str, latency_ms: int) -> None:
        """Record one connector tool call; calls are never deduplicated."""

        if not self.recording:
            return
        session = get_session(self.engine)
        try:
            now = self._clock()
            self._bound_the_wait(session)
            session.execute(
                insert(UsageEvent).values(
                    firebase_uid=firebase_uid,
                    occurred_at=now,
                    day=slate_date_for_instant(now),
                    source=SOURCE_MCP,
                    name=EVENT_MCP_TOOL_CALL,
                    tool=tool,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
            )
            session.commit()
        except Exception as error:
            session.rollback()
            logger.warning("Could not record an MCP tool call: %s", error)
        finally:
            session.close()

    @staticmethod
    def _bound_the_wait(session) -> None:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(f"SET LOCAL statement_timeout = {WRITE_STATEMENT_TIMEOUT_MS}"))
