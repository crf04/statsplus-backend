"""Every new account starts with the default Targets (DEFAULT_TARGETS).

They are ordinary Targets: the owner can delete them, and a later sign-in
never brings one back.  Accounts that existed before never receive them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.user import User
from app.services.user_service import UserService


@pytest.fixture
def service(tmp_path, runtime_settings):
    engine = create_engine(f"sqlite:///{tmp_path / 'defaults.sqlite3'}")
    run_migrations(engine)
    yield UserService(engine, settings=runtime_settings)
    engine.dispose()


def _summary(targets):
    return [(t["opponent"], t["title"], t["note"], t["conditions"], t["stat_preferences"]) for t in targets]


def test_a_new_account_starts_with_the_four_default_targets(service):
    service.create_or_update_user({"uid": "new", "email": "new@example.com"})

    assert _summary(service.list_targets("new")) == [
        (
            "POR",
            "POR vs P&R roll man ≥ 10.4%, Catch & shoot ≥ 22.5%",
            "CLINGAN drop",
            {
                "defender": {"player_id": 1642270, "comparator": "at_least", "minutes": 10},
                "from": None,
                "to": None,
                "player_minutes": 15,
            },
            {"columns": ["PTS", "FGA", "PTS/36", "FGA/36", "3PA", "3PA/36"], "graded_by": "PTS"},
        ),
        (
            "IND",
            "IND vs P&R ball handler ≥ 29.8%, Isolation ≥ 7.4%",
            None,
            {"defender": None, "from": None, "to": None, "player_minutes": 10},
            None,
        ),
        (
            "ORL",
            "ORL vs Catch & shoot ≥ 42%, Spot up ≥ 30.2%",
            None,
            {"defender": None, "from": None, "to": None, "player_minutes": 10},
            {"columns": ["PTS", "PTS/36", "FGA", "FGA/36", "3PA/36"], "graded_by": "PTS"},
        ),
        (
            "NYK",
            "NYK vs P&R roll man ≥ 18%, Spot up ≤ 20%",
            None,
            {"defender": None, "from": None, "to": None, "player_minutes": 15},
            {"columns": ["PTS", "FGA", "PTS/36", "FGA/36"], "graded_by": "PTS"},
        ),
    ]


def test_a_waitlisted_signup_also_gets_the_defaults(service):
    service.set_signups_open(False)
    service.create_or_update_user({"uid": "queued", "email": "queued@example.com"})

    assert [t["opponent"] for t in service.list_targets("queued")] == [
        "POR",
        "IND",
        "ORL",
        "NYK",
    ]


def test_a_deleted_default_stays_deleted_after_signing_in_again(service):
    service.create_or_update_user({"uid": "new", "email": "new@example.com"})
    por = service.list_targets("new")[0]
    service.delete_target("new", por["id"])

    service.create_or_update_user({"uid": "new", "email": "new@example.com"})

    assert [t["opponent"] for t in service.list_targets("new")] == ["IND", "ORL", "NYK"]


def test_an_existing_account_does_not_receive_the_defaults(service):
    with service.engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": "old",
                "email": "old@example.com",
                "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                "last_login": datetime(2026, 8, 1, tzinfo=timezone.utc),
                "is_active": True,
            },
        )

    service.create_or_update_user({"uid": "old", "email": "old@example.com"})

    assert service.list_targets("old") == []


def test_a_signup_whose_defaults_fail_stores_no_account(service, monkeypatch):
    """The user row and its defaults commit together, or not at all."""

    def failing(firebase_uid, now):
        raise RuntimeError("seeding failed")

    monkeypatch.setattr("app.services.user_service._default_targets", failing)

    assert service.create_or_update_user({"uid": "new", "email": "new@example.com"}) is None
    with service.engine.connect() as connection:
        assert connection.execute(User.__table__.select()).all() == []
