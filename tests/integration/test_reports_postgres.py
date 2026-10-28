"""Postgres checks for CourtAI reports (crf04/statsplus#342) and the Research feed (#372).

Covers what SQLite cannot: migrations 076, 077 and 080 and their reversal on
the production dialect, the ``JSONB`` columns, a publish and read round trip
of a realistic report through the service, the feed's keyset pages over
``timestamptz`` and its partial index, and the listing cap under concurrency.
"""

from __future__ import annotations

import contextlib
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.config.settings import load_settings
from app.errors import ConflictError, FlatBodyError
from app.migrations import (
    downgrade_reports,
    downgrade_research_feed,
    downgrade_user_public_handle,
    run_migrations,
)
from app.services.reports import ReportService
from app.services.user_service import UserService

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def _user(engine, uid):
    settings = load_settings(overrides={"DATABASE_URL": str(engine.url)})
    UserService(engine, settings=settings).create_or_update_user(
        {"uid": uid, "email": f"{uid}@example.com", "name": uid}
    )


def _body():
    return {
        "report": {
            "title": "Injury brief",
            "date": "2026-10-24",
            "opening": ["Three games carry a real absence."],
            "tabs": [
                {
                    "title": "Spots",
                    "layout": "rows",
                    "items": [{"title": "Brunson points", "tag": "lean", "cards": ["c1"]}],
                }
            ],
        },
        "calls": [
            {
                "id": "c1",
                "tool": "query_game_logs",
                "args": {"player": "Jalen Brunson"},
                "structured": {"rows": [{"PTS": 31, "MIN": "34:12"}], "note": "ünïcode ✓"},
                "card": "game-logs",
            }
        ],
        "cards": {"game-logs": "<html><script>render()</script></html>"},
        "show_author": True,
    }


def test_migrations_076_and_077_apply_reverse_and_reapply(engine):
    columns = {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("reports")}
    assert columns == {
        "id": "VARCHAR(12)",
        "firebase_uid": "VARCHAR(128)",
        "created_at": "TIMESTAMP",
        "title": "VARCHAR(120)",
        "document": "JSONB",
        "calls": "JSONB",
        "cards": "JSONB",
        "show_author": "BOOLEAN",
        "deleted_at": "TIMESTAMP",
        "taken_down_at": "TIMESTAMP",
        "listed_at": "TIMESTAMP",
        "featured_at": "TIMESTAMP",
        "covers": "JSONB",
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'reports' AND column_name = 'created_at'"
                )
            ).scalar()
            == "timestamp with time zone"
        )
        assert (
            connection.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'reports'::regclass AND contype = 'f'"
                )
            ).scalar()
            == "c"
        )
    users = {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("users")}
    assert users["public_handle"] == "VARCHAR(40)"

    with engine.begin() as connection:
        downgrade_reports(connection)
        downgrade_user_public_handle(connection)
    assert "reports" not in inspect(engine).get_table_names()
    assert "public_handle" not in {c["name"] for c in inspect(engine).get_columns("users")}
    assert run_migrations(engine).applied == ("076_create_reports", "077_user_public_handle")
    assert run_migrations(engine).applied == ()


def test_a_report_round_trips_and_goes_with_its_account(engine):
    _user(engine, "author")
    service = ReportService(engine)
    service.set_public_handle("author", "@bighatprops")

    published = service.publish("author", _body())
    read = service.read(published["id"], viewer_uid="author")

    assert read["author"] == {"label": "@bighatprops"}
    assert (read["listed"], read["featured"]) == (False, False)
    assert read["is_author"] is True
    assert read["created_at"].endswith("+00:00")
    assert read["calls"] == [
        {
            "id": "c1",
            "tool": "query_game_logs",
            "card": "game-logs",
            "structured": {"rows": [{"PTS": 31, "MIN": "34:12"}], "note": "ünïcode ✓"},
            "meta": None,
        }
    ]
    assert read["cards"] == {"game-logs": "<html><script>render()</script></html>"}
    assert service.meta(published["id"])["tags"] == {"lean": 1}
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT document -> 'tabs' -> 0 ->> 'layout' FROM reports WHERE id = :id"),
                {"id": published["id"]},
            ).scalar()
            == "rows"
        )

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE firebase_uid = 'author'"))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reports")).scalar() == 0


def test_two_concurrent_publishes_at_999_leave_exactly_1000_and_one_conflict(engine):
    _user(engine, "author")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reports (id, firebase_uid, created_at, title, document, calls, cards, show_author) "
                "SELECT 'seed' || lpad(n::text, 6, '0'), 'author', now(), 'Seeded', '{}', '[]', '{}', false "
                "FROM generate_series(1, 999) AS n"
            )
        )
    # Both publishes wait here, after counting, for the other to count too.  If
    # the count is not serialized both pass the cap; with the account lock the
    # second blocks before counting, so the first gives up waiting and commits.
    barrier = threading.Barrier(2, timeout=2)

    def clock():
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return datetime.now(UTC)

    service = ReportService(engine, clock=clock)
    outcomes = []

    def publish():
        try:
            service.publish("author", _body())
            outcomes.append("published")
        except ConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["conflict", "published"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reports")).scalar() == 1000


def _index_definition(engine, name):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": name}
        ).scalar()


def test_migration_080_normalises_handles_applies_reverses_and_reapplies(engine):
    for uid in ("founder", "other"):
        _user(engine, uid)
    with engine.begin() as connection:
        downgrade_research_feed(connection)
        connection.execute(
            text("UPDATE users SET public_handle = '@BigHatProps' WHERE firebase_uid = 'founder'")
        )
    assert _index_definition(engine, "idx_reports_feed") is None

    assert run_migrations(engine).applied == ("080_research_feed",)

    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT firebase_uid, public_handle FROM users")).all()) == {
            "founder": "bighatprops",
            "other": None,
        }
    assert _index_definition(engine, "idx_reports_feed") == (
        "CREATE INDEX idx_reports_feed ON public.reports USING btree (listed_at, id) WHERE "
        "((listed_at IS NOT NULL) AND (deleted_at IS NULL) AND (taken_down_at IS NULL))"
    )
    assert _index_definition(engine, "uq_users_public_handle") == (
        "CREATE UNIQUE INDEX uq_users_public_handle ON public.users USING btree (lower((public_handle)::text))"
    )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET public_handle = 'BIGHATPROPS' WHERE firebase_uid = 'other'")
        )
    assert run_migrations(engine).applied == ()


def test_migration_080_refuses_colliding_handles_and_rolls_back(engine):
    for uid in ("founder", "copycat"):
        _user(engine, uid)
    with engine.begin() as connection:
        downgrade_research_feed(connection)
        connection.execute(text("UPDATE users SET public_handle = '@bighat' WHERE firebase_uid = 'founder'"))
        connection.execute(text("UPDATE users SET public_handle = 'BigHat' WHERE firebase_uid = 'copycat'"))

    with pytest.raises(ValueError, match=r"collide once normalised: bighat \(copycat, founder\)"):
        run_migrations(engine)

    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT firebase_uid, public_handle FROM users")).all()) == {
            "founder": "@bighat",
            "copycat": "BigHat",
        }
        assert connection.execute(text("SELECT max(version) FROM schema_migrations")).scalar() == 79
    assert "listed_at" not in {c["name"] for c in inspect(engine).get_columns("reports")}


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 28, 12, 0, 0, 123456, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(hours=6, microseconds=1)
        return self.now


def test_the_feed_pages_by_listing_time_with_featured_on_top(engine):
    for uid in ("author", "second"):
        _user(engine, uid)
    ids = (f"pg{number:08d}" for number in range(1, 100))
    service = ReportService(engine, clock=_Clock(), new_id=lambda: next(ids))
    service.set_public_handle("author", "author")
    service.set_public_handle("second", "second")
    listed = []
    for index in range(23):
        uid = "author" if index % 2 else "second"
        report_id = service.publish(uid, _body())["id"]
        service.set_listed(uid, report_id, True)
        listed.append(report_id)
    service.set_featured(listed[3], True)
    deleted = listed[20]
    service.delete("author" if 20 % 2 else "second", deleted)

    first = service.feed()
    second = service.feed(first["next_cursor"])

    expected = [report_id for report_id in reversed(listed) if report_id not in (listed[3], deleted)]
    assert [entry["id"] for entry in first["featured"]] == [listed[3]]
    assert [entry["id"] for entry in first["reports"]] == expected[:20]
    assert [entry["id"] for entry in second["reports"]] == expected[20:] == [listed[0]]
    assert (second["featured"], second["next_cursor"]) == ([], None)
    assert first["reports"][0]["covers"] == {"games": [], "players": ["Jalen Brunson"]}
    assert first["reports"][0]["listed_at"].endswith("+00:00")
    author = service.author_feed("AUTHOR")
    assert [entry["id"] for entry in author["reports"]] == [
        report_id for report_id in reversed(listed[1::2]) if report_id != deleted
    ]


def test_two_concurrent_listings_at_4_leave_exactly_5_and_one_refusal(engine):
    _user(engine, "author")
    service = ReportService(engine)
    service.set_public_handle("author", "author")
    drafts = [service.publish("author", _body())["id"] for _ in range(6)]
    for report_id in drafts[:4]:
        service.set_listed("author", report_id, True)
    barrier = threading.Barrier(2, timeout=2)

    def clock():
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return datetime.now(UTC)

    racing = ReportService(engine, clock=clock)
    outcomes = []

    def list_one(report_id):
        try:
            racing.set_listed("author", report_id, True)
            outcomes.append("listed")
        except FlatBodyError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=list_one, args=(report_id,)) for report_id in drafts[4:]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["listed", "listed_limit"]
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM reports WHERE listed_at IS NOT NULL")).scalar() == 5
        )
        assert connection.execute(text("SELECT count(*) FROM report_listings")).scalar() == 5


def test_a_first_claim_racing_another_write_cannot_overwrite_it(engine):
    _user(engine, "author")
    service = ReportService(engine)
    outcome = []
    # Another first claim holds the row, uncommitted, while this one runs.
    with engine.connect() as other:
        transaction = other.begin()
        other.execute(text("UPDATE users SET public_handle = 'first' WHERE firebase_uid = 'author'"))

        def claim():
            try:
                outcome.append(service.claim_public_handle("author", "second"))
            except FlatBodyError as error:
                outcome.append(error.code)

        thread = threading.Thread(target=claim)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()  # waiting on the row lock
        transaction.commit()
    thread.join(timeout=30)

    assert outcome == ["handle_already_set"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT public_handle FROM users")).scalar() == "first"


def test_two_accounts_racing_for_one_handle_leave_one_owner_and_handle_taken(engine):
    for uid in ("first", "second"):
        _user(engine, uid)
    service = ReportService(engine)
    outcome = []
    with engine.connect() as other:
        transaction = other.begin()
        other.execute(text("UPDATE users SET public_handle = 'same' WHERE firebase_uid = 'first'"))

        def claim():
            try:
                outcome.append(service.claim_public_handle("second", "same"))
            except FlatBodyError as error:
                outcome.append(error.code)

        thread = threading.Thread(target=claim)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()  # waiting on the unique index entry
        transaction.commit()
    thread.join(timeout=30)

    assert outcome == ["handle_taken"]
    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT firebase_uid, public_handle FROM users")).all()) == {
            "first": "same",
            "second": None,
        }


def test_featuring_waits_for_an_unlisting_and_then_refuses(engine):
    _user(engine, "author")
    service = ReportService(engine)
    service.set_public_handle("author", "author")
    report_id = service.publish("author", {**_body(), "listed": True})["id"]
    outcome = []
    # The author's unlisting holds the account lock, uncommitted, as set_listed does.
    with engine.connect() as other:
        transaction = other.begin()
        other.execute(text("SELECT 1 FROM users WHERE firebase_uid = 'author' FOR UPDATE"))
        other.execute(
            text("UPDATE reports SET listed_at = NULL, featured_at = NULL WHERE id = :id"), {"id": report_id}
        )

        def feature():
            try:
                outcome.append(service.set_featured(report_id, True)["featured"])
            except ConflictError:
                outcome.append("conflict")

        thread = threading.Thread(target=feature)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()  # waiting on the account lock
        transaction.commit()
    thread.join(timeout=30)

    assert outcome == ["conflict"]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT listed_at, featured_at FROM reports WHERE id = :id"), {"id": report_id}
        ).one() == (None, None)
