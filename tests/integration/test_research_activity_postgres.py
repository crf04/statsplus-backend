"""Postgres checks for research activity counts (crf04/statsplus#213).

Covers what SQLite cannot: concurrent first counts for one account and date,
the bounded wait on a locked row, the check constraints, cascade deletes and
the report's joins on real column types.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.config.settings import load_settings
from app.migrations import _create_research_activity_tables, run_migrations
from app.services.research_activity import (
    COUNT_STATEMENT_TIMEOUT_MS,
    ResearchActivityService,
)
from app.services.user_service import UserService

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

DAY = date(2026, 10, 14)
NOON_WEDNESDAY = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=16)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def service(engine):
    return ResearchActivityService(engine, clock=lambda: NOON_WEDNESDAY)


def _user(engine, uid):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    UserService(engine, settings=settings).create_or_update_user(
        {"uid": uid, "email": f"{uid}@example.com", "name": uid}
    )


def _actions(engine, uid):
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT actions FROM research_activity_days WHERE firebase_uid = :uid"),
                {"uid": uid},
            )
            .scalars()
            .all()
        )


def test_migration_068_reruns_as_a_noop_and_keeps_rows(engine):
    _user(engine, "u1")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO research_activity_days VALUES ('u1', '2026-10-14', 3)"))
        connection.execute(text("INSERT INTO account_cohorts VALUES ('u1', 'friend', now())"))
        _create_research_activity_tables(connection)
    assert run_migrations(engine).applied == ()

    assert _actions(engine, "u1") == [3]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT cohort FROM account_cohorts")).scalar() == "friend"


def test_the_tables_refuse_values_the_service_never_writes(engine):
    _user(engine, "u1")

    for statement in (
        "INSERT INTO research_activity_days VALUES ('u1', '2026-10-14', 0)",
        "INSERT INTO account_cohorts VALUES ('u1', 'vip', now())",
        "INSERT INTO research_activity_days VALUES ('ghost', '2026-10-14', 1)",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement))


def test_deleting_an_account_deletes_its_counts_and_tag(engine, service):
    _user(engine, "u1")
    service.record("u1", DAY)
    service.set_cohort("u1", "friend")

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'u1'"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM research_activity_days")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM account_cohorts")).scalar() == 0


def test_concurrent_first_counts_for_one_account_and_date_all_land(engine, service):
    _user(engine, "u1")
    workers = 12
    barrier = threading.Barrier(workers)
    failures = []

    def count():
        barrier.wait()
        try:
            service.record("u1", DAY)
        except Exception as error:  # pragma: no cover - reported below
            failures.append(error)

    threads = [threading.Thread(target=count) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert _actions(engine, "u1") == [workers]


def test_a_count_blocked_by_a_locked_row_gives_up_without_raising(engine, service):
    _user(engine, "u1")
    service.record("u1", DAY)
    holder = engine.connect()
    transaction = holder.begin()
    holder.execute(text("SELECT 1 FROM research_activity_days WHERE firebase_uid = 'u1' FOR UPDATE"))
    bound = COUNT_STATEMENT_TIMEOUT_MS / 1000
    counter = threading.Thread(target=service.record_quietly, args=("u1", DAY))
    started = time.monotonic()
    counter.start()
    counter.join(timeout=bound + 3)
    still_waiting = counter.is_alive()
    waited = time.monotonic() - started
    transaction.rollback()
    holder.close()
    counter.join()

    assert not still_waiting, "the count outwaited its bound"
    assert bound <= waited < bound + 3
    assert _actions(engine, "u1") == [1]
    service.record("u1", DAY)
    assert _actions(engine, "u1") == [2]


def test_the_report_and_the_admin_list_read_real_column_types(engine, service):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    users = UserService(engine, settings=settings)
    for uid in ("u1", "u2"):
        _user(engine, uid)
    users.record_first_touch("u1", {"ref": "x-picks"})
    service.set_cohort("u2", "internal")
    service.record("u1", date(2026, 10, 13))
    service.record("u1", date(2026, 10, 14))
    service.record("u1", date(2026, 10, 14))

    report = service.report()

    by_id = {row["id"]: row for row in report["accounts"]}
    u1 = by_id["63de1d3f69de"]
    assert (u1["source"], u1["cohort"], u1["active_weeks"]) == ("x-picks", None, 1)
    assert u1["weeks"][3] == {"start": "2026-10-12", "game_days": 2, "actions": 3, "active": True}
    u2 = by_id["4d5df4c841d7"]
    assert (u2["cohort"], u2["source"], u2["active_weeks"]) == ("internal", None, 0)
    listing = {u["uid"]: u for u in users.list_users_by_access_status("active")}
    assert (listing["u2"]["cohort"], listing["u1"]["cohort"]) == ("internal", None)
