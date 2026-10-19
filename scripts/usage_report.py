"""Read-only usage report from the ``usage_events`` table (crf04/statsplus#261).

Prints four plain-text tables over the last N US Eastern weeks (Monday-start,
the newest being the week in progress):

1. weekly active users, by source and combined (distinct across both);
2. signup-cohort retention: the share of each creation week's users with an
   ``active`` row, on any source, in week 0 through week N after signup;
3. weekly distinct users of each web action;
4. per MCP tool over the whole window: calls, distinct users, outcome shares
   and p50/p95 latency (nearest rank).

The connection is read-only (a read-only transaction on PostgreSQL, query-only
on SQLite), so the command cannot change data and a rerun prints the same
numbers.  ``--exclude`` leaves an account out of every table.

    python scripts/usage_report.py --database-url "$DATABASE_URL" \\
        --weeks 8 --exclude me@example.com --exclude teammate@example.com
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine

from app.domain.slate_time import slate_date_for_instant
from app.models.usage_event import (
    EVENT_ACTIVE,
    EVENT_MCP_TOOL_CALL,
    OUTCOMES,
    SOURCE_MCP,
    SOURCE_WEB,
    WEB_ACTIONS,
    UsageEvent,
)
from app.models.user import User
from app.utils.db import _normalize_database_url

DEFAULT_WEEKS = 8


def open_read_only_engine(database_url: str) -> Engine:
    """An engine whose connections reject every write."""

    url = _normalize_database_url(database_url)
    if url.startswith("sqlite"):
        engine = create_engine(url)

        @event.listens_for(engine, "connect")
        def _query_only(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA query_only = ON")

        return engine
    return create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})


def week_start(day: date) -> date:
    """The Monday of the Monday-to-Sunday week containing ``day``."""

    return day - timedelta(days=day.weekday())


def percentile(sorted_values: list[int], fraction: float) -> int:
    """Nearest-rank percentile: the ceil(fraction * n)-th smallest value."""

    return sorted_values[max(math.ceil(fraction * len(sorted_values)), 1) - 1]


def _share(count: int, total: int) -> str:
    return f"{round(100 * count / total)}%"


def _table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(line[i])) for line in [headers, *rows]) for i in range(len(headers))]
    lines = [title]
    for line in [headers, *rows]:
        cells = [
            str(cell).ljust(widths[0]) if i == 0 else str(cell).rjust(widths[i])
            for i, cell in enumerate(line)
        ]
        lines.append("  ".join(cells))
    return "\n".join(lines)


def build_report(engine: Engine, *, weeks: int, exclude: list[str], today: date) -> str:
    """The four tables, as one string."""

    starts = [week_start(today) - timedelta(weeks=weeks - 1 - offset) for offset in range(weeks)]
    excluded = {email.strip().lower() for email in exclude}

    with engine.connect() as connection:
        users = [
            (uid, slate_date_for_instant(created_at))
            for uid, email, created_at in connection.execute(
                select(User.firebase_uid, User.email, User.created_at)
            )
            if (email or "").lower() not in excluded
        ]
        kept = {uid for uid, _ in users}
        events = [
            row
            for row in connection.execute(
                select(
                    UsageEvent.firebase_uid,
                    UsageEvent.day,
                    UsageEvent.source,
                    UsageEvent.name,
                    UsageEvent.tool,
                    UsageEvent.outcome,
                    UsageEvent.latency_ms,
                ).where(UsageEvent.day >= starts[0])
            )
            if row.firebase_uid in kept
        ]
        first_day = connection.execute(select(UsageEvent.day).order_by(UsageEvent.day).limit(1)).scalar()

    def users_by_week(predicate) -> dict[date, set[str]]:
        found: dict[date, set[str]] = defaultdict(set)
        for row in events:
            if predicate(row):
                found[week_start(row.day)].add(row.firebase_uid)
        return found

    # 1. Weekly active users.
    active = users_by_week(lambda row: row.name == EVENT_ACTIVE)
    web = users_by_week(lambda row: row.name == EVENT_ACTIVE and row.source == SOURCE_WEB)
    mcp = users_by_week(lambda row: row.name == EVENT_ACTIVE and row.source == SOURCE_MCP)
    weekly_active = _table(
        "Weekly active users (week starting Monday, US Eastern)",
        ["week", SOURCE_WEB, SOURCE_MCP, "combined"],
        [[str(start), len(web[start]), len(mcp[start]), len(active[start])] for start in starts],
    )

    # 2. Signup-cohort retention.
    active_weeks: dict[str, set[date]] = defaultdict(set)
    for row in events:
        if row.name == EVENT_ACTIVE:
            active_weeks[row.firebase_uid].add(week_start(row.day))
    cohorts: dict[date, list[str]] = defaultdict(list)
    for uid, created in users:
        cohorts[week_start(created)].append(uid)
    retention_rows = []
    for start in starts:
        members = cohorts.get(start, [])
        if not members:
            continue
        cells = []
        for k in range(weeks):
            later = start + timedelta(weeks=k)
            if later > starts[-1]:
                cells.append("-")
                continue
            cells.append(_share(sum(later in active_weeks[uid] for uid in members), len(members)))
        retention_rows.append([str(start), len(members), *cells])
    retention = _table(
        "Signup-cohort retention (share active, any source, N weeks after signup)",
        ["cohort", "users", *[f"wk{k}" for k in range(weeks)]],
        retention_rows,
    )

    # 3. Weekly users of each web action.
    by_action = {action: users_by_week(lambda row, a=action: row.name == a) for action in WEB_ACTIONS}
    features = _table(
        "Weekly users per web action",
        ["week", *WEB_ACTIONS],
        [[str(start), *[len(by_action[action][start]) for action in WEB_ACTIONS]] for start in starts],
    )

    # 4. MCP tools over the whole window.
    calls: dict[str, list] = defaultdict(list)
    for row in events:
        if row.name == EVENT_MCP_TOOL_CALL:
            calls[row.tool].append(row)
    tool_rows = []
    for tool in sorted(calls):
        rows = calls[tool]
        latencies = sorted(row.latency_ms for row in rows)
        tool_rows.append(
            [
                tool,
                len(rows),
                len({row.firebase_uid for row in rows}),
                *[_share(sum(row.outcome == outcome for row in rows), len(rows)) for outcome in OUTCOMES[1:]],
                percentile(latencies, 0.5),
                percentile(latencies, 0.95),
            ]
        )
    tools = _table(
        f"MCP tool calls, {starts[0]} to today",
        ["tool", "calls", "users", "error", "limit", "needs_choice", "p50_ms", "p95_ms"],
        tool_rows,
    )

    since = f"Usage events recorded since {first_day}" if first_day else "No usage events recorded yet"
    return "\n\n".join([since, weekly_active, retention, features, tools])


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", help="SQLAlchemy URL (or set DATABASE_URL)")
    parser.add_argument("--weeks", type=int, default=DEFAULT_WEEKS, help="weeks to report (default 8)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="EMAIL",
        help="leave this account out of every table; repeatable",
    )
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a database target is required: pass --database-url or set DATABASE_URL")
    if args.weeks < 1:
        parser.error("--weeks must be at least 1")

    engine = open_read_only_engine(database_url)
    try:
        print(
            build_report(
                engine,
                weeks=args.weeks,
                exclude=args.exclude,
                today=today or slate_date_for_instant(datetime.now(UTC)),
            )
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
