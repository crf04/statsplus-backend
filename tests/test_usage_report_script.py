"""The read-only usage report (crf04/statsplus#261).

Seeds users and events in a migrated database, runs the command, and compares
the printed tables with literal expected text.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from scripts.usage_report import main, open_read_only_engine, percentile

#: Wednesday; its Monday-to-Sunday Eastern week starts on 2026-10-12.
TODAY = date(2026, 10, 14)

#: email -> UTC creation instant.  team@ stands for an account to leave out.
USERS = {
    "a@example.com": "2026-09-30 01:00:00",  # Tuesday evening Eastern, week of 09-28
    "b@example.com": "2026-09-30 12:00:00",
    "team@example.com": "2026-09-28 12:00:00",
    "c@example.com": "2026-10-06 12:00:00",
    "d@example.com": "2026-10-13 12:00:00",
    # Created Sunday 23:30 Eastern, which is Monday UTC: still the week of 10-05.
    "e@example.com": "2026-10-12 03:30:00",
    "old@example.com": "2026-08-01 12:00:00",
}

#: (email, day, source, name)
DAILY = [
    ("a@example.com", "2026-09-29", "web", "active"),
    ("a@example.com", "2026-10-06", "web", "active"),
    ("a@example.com", "2026-10-07", "mcp", "active"),
    ("a@example.com", "2026-10-13", "web", "active"),
    ("b@example.com", "2026-09-30", "web", "active"),
    ("c@example.com", "2026-10-06", "mcp", "active"),
    ("c@example.com", "2026-10-14", "web", "active"),
    ("d@example.com", "2026-10-13", "web", "active"),
    ("d@example.com", "2026-10-13", "mcp", "active"),
    ("e@example.com", "2026-10-11", "web", "active"),
    ("old@example.com", "2026-10-05", "web", "active"),
    ("old@example.com", "2026-10-14", "mcp", "active"),
    ("team@example.com", "2026-09-28", "web", "active"),
    ("team@example.com", "2026-10-13", "web", "active"),
    ("team@example.com", "2026-10-13", "mcp", "active"),
    ("a@example.com", "2026-09-29", "web", "game_logs_queried"),
    ("b@example.com", "2026-09-30", "web", "game_logs_queried"),
    ("a@example.com", "2026-10-06", "web", "matchup_viewed"),
    ("a@example.com", "2026-10-13", "web", "matchup_viewed"),
    ("c@example.com", "2026-10-14", "web", "matchup_viewed"),
    ("d@example.com", "2026-10-13", "web", "matchup_viewed"),
    ("team@example.com", "2026-10-13", "web", "matchup_viewed"),
    ("c@example.com", "2026-10-06", "web", "query_prompt_parsed"),
    ("d@example.com", "2026-10-14", "web", "backtest_viewed"),
    ("c@example.com", "2026-10-14", "web", "report_published"),
    ("team@example.com", "2026-10-13", "web", "report_published"),
    # Before the window: counted nowhere.
    ("a@example.com", "2026-09-27", "web", "active"),
]

#: (email, day, tool, outcome, latency_ms)
TOOL_CALLS = [
    ("a@example.com", "2026-10-06", "get_matchup", "ok", 100),
    ("a@example.com", "2026-10-07", "get_matchup", "ok", 200),
    ("c@example.com", "2026-10-06", "get_matchup", "error", 300),
    ("c@example.com", "2026-10-14", "get_matchup", "limit", 400),
    ("d@example.com", "2026-10-13", "get_matchup", "needs_choice", 500),
    ("a@example.com", "2026-10-07", "query_game_logs", "ok", 10),
    ("a@example.com", "2026-10-07", "query_game_logs", "ok", 20),
    ("a@example.com", "2026-10-13", "query_game_logs", "ok", 30),
    ("a@example.com", "2026-10-13", "query_game_logs", "error", 40),
    ("team@example.com", "2026-10-13", "query_game_logs", "error", 9999),
]

ALL_ACCOUNTS = """\
Usage events recorded since 2026-09-27

Weekly active users (week starting Monday, US Eastern)
week        web  mcp  combined
2026-09-28    3    0         3
2026-10-05    3    2         4
2026-10-12    4    3         5

Signup-cohort retention (share active, any source, N weeks after signup)
cohort      users   wk0  wk1  wk2
2026-09-28      3  100%  33%  67%
2026-10-05      2  100%  50%    -
2026-10-12      1  100%    -    -

Weekly users per web action
week        matchup_viewed  game_logs_queried  query_prompt_parsed  backtest_viewed  report_published
2026-09-28               0                  2                    0                0                 0
2026-10-05               1                  0                    1                0                 0
2026-10-12               4                  0                    0                1                 2

MCP tool calls, 2026-09-28 to today
tool             calls  users  error  limit  needs_choice  p50_ms  p95_ms
get_matchup          5      3    20%    20%           20%     300     500
query_game_logs      5      2    40%     0%            0%      30    9999
"""

WITHOUT_TEAM = """\
Usage events recorded since 2026-09-27

Weekly active users (week starting Monday, US Eastern)
week        web  mcp  combined
2026-09-28    2    0         2
2026-10-05    3    2         4
2026-10-12    3    2         4

Signup-cohort retention (share active, any source, N weeks after signup)
cohort      users   wk0  wk1  wk2
2026-09-28      2  100%  50%  50%
2026-10-05      2  100%  50%    -
2026-10-12      1  100%    -    -

Weekly users per web action
week        matchup_viewed  game_logs_queried  query_prompt_parsed  backtest_viewed  report_published
2026-09-28               0                  2                    0                0                 0
2026-10-05               1                  0                    1                0                 0
2026-10-12               3                  0                    0                1                 1

MCP tool calls, 2026-09-28 to today
tool             calls  users  error  limit  needs_choice  p50_ms  p95_ms
get_matchup          5      3    20%    20%           20%     300     500
query_game_logs      4      1    25%     0%            0%      20      40
"""


@pytest.fixture
def database_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'usage.db'}"
    engine = create_engine(url)
    run_migrations(engine)
    with engine.begin() as connection:
        for email, created in USERS.items():
            connection.execute(
                text(
                    "INSERT INTO users (firebase_uid, email, created_at, last_login, is_active) "
                    "VALUES (:uid, :uid, :created, :created, 1)"
                ),
                {"uid": email, "created": created},
            )
        for uid, day, source, name in DAILY:
            connection.execute(
                text(
                    "INSERT INTO usage_events (firebase_uid, occurred_at, day, source, name) "
                    "VALUES (:uid, CURRENT_TIMESTAMP, :day, :source, :name)"
                ),
                {"uid": uid, "day": day, "source": source, "name": name},
            )
        for uid, day, tool, outcome, latency in TOOL_CALLS:
            connection.execute(
                text(
                    "INSERT INTO usage_events "
                    "(firebase_uid, occurred_at, day, source, name, tool, outcome, latency_ms) "
                    "VALUES (:uid, CURRENT_TIMESTAMP, :day, 'mcp', 'mcp_tool_call', :tool, :outcome, :latency)"
                ),
                {"uid": uid, "day": day, "tool": tool, "outcome": outcome, "latency": latency},
            )
    engine.dispose()
    return url


def _run(capsys, *argv):
    assert main(list(argv), today=TODAY) == 0
    out = capsys.readouterr().out
    # Other tests may leave JSON logging on stdout; the report starts at its own first line.
    return out[min(i for i in (out.find("Usage events"), out.find("No usage events")) if i >= 0) :]


def _dump(database_url):
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return {
                table: connection.execute(text(f"SELECT * FROM {table} ORDER BY 1, 2, 3")).all()
                for table in ("users", "usage_events")
            }
    finally:
        engine.dispose()


def test_the_report_prints_the_four_tables(database_url, capsys):
    out = _run(capsys, "--database-url", database_url, "--weeks", "3")

    assert out == ALL_ACCOUNTS


def test_exclude_leaves_the_account_out_of_every_table(database_url, capsys):
    out = _run(capsys, "--database-url", database_url, "--weeks", "3", "--exclude", "team@example.com")

    assert out == WITHOUT_TEAM


def test_exclude_is_repeatable_and_matches_case_insensitively(database_url, capsys):
    both = _run(
        capsys,
        "--database-url",
        database_url,
        "--weeks",
        "3",
        "--exclude",
        "TEAM@example.com",
        "--exclude",
        "old@example.com",
    )

    # old@ was active in weeks 10-05 (web) and 10-12 (mcp), team@ in 09-28 and 10-12.
    assert both.split("\n\n")[1].splitlines()[2:] == [
        "2026-09-28    2    0         2",
        "2026-10-05    2    2         3",
        "2026-10-12    3    1         3",
    ]


def test_weeks_defaults_to_eight(database_url, capsys):
    out = _run(capsys, "--database-url", database_url)

    weekly = out.split("\n\n")[1].splitlines()
    assert [line.split()[0] for line in weekly[2:]] == [
        "2026-08-24",
        "2026-08-31",
        "2026-09-07",
        "2026-09-14",
        "2026-09-21",
        "2026-09-28",
        "2026-10-05",
        "2026-10-12",
    ]
    assert "wk7" in out


def test_an_empty_table_prints_empty_tables(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    run_migrations(create_engine(url))

    out = _run(capsys, "--database-url", url, "--weeks", "2")

    assert out.startswith("No usage events recorded yet\n")
    assert "2026-10-05    0    0         0" in out


def test_running_the_report_leaves_the_database_unchanged(database_url, capsys):
    before = _dump(database_url)

    _run(capsys, "--database-url", database_url, "--weeks", "3")

    assert _dump(database_url) == before


def test_the_report_connection_refuses_writes(database_url):
    from sqlalchemy.exc import OperationalError

    engine = open_read_only_engine(database_url)
    with pytest.raises(OperationalError), engine.begin() as connection:
        connection.execute(text("DELETE FROM usage_events"))


def test_a_database_target_is_required(capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as stopped:
        main([], today=TODAY)

    assert stopped.value.code == 2
    assert "a database target is required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [([5], 0.95, 5), ([1, 2, 3, 4], 0.5, 2), ([1, 2, 3, 4, 5], 0.5, 3), (list(range(1, 101)), 0.95, 95)],
)
def test_percentiles_are_nearest_rank(values, fraction, expected):
    assert percentile(values, fraction) == expected
