"""Saved Filter Set storage, service rules, and CRUD routes (#194).

The service tests run against a real migrated SQLite database so the
per-account uniqueness index and the ``ON DELETE CASCADE`` back to ``users``
are exercised rather than described.  The route tests stay at the HTTP seam
with a stub service, matching ``test_user_routes``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.errors import ConflictError, InvalidInputError, ResourceNotFoundError
from app.migrations import run_migrations
from app.models.saved_filter_set import SavedFilterSet
from app.models.user import User
from app.services.user_service import (
    SAVED_FILTER_SET_LIMIT,
    SAVED_FILTER_SET_NAME_MAX_LENGTH,
    SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH,
    UserService,
)


OWNER = "owner-uid"
OTHER = "other-uid"
QUERY_STRING = "player=Nikola+Jokic&season_filter=2024-25&location_filter=Home"


@pytest.fixture
def saved_filter_set_engine(tmp_path):
    """A migrated application database holding two distinct accounts."""

    engine = create_engine(f"sqlite:///{tmp_path / 'saved-filter-sets.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        for uid in (OWNER, OTHER):
            connection.execute(
                User.__table__.insert(),
                {
                    "firebase_uid": uid,
                    "email": f"{uid}@example.com",
                    "display_name": uid,
                    "photo_url": None,
                    "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                    "last_login": datetime(2026, 8, 1, tzinfo=timezone.utc),
                    "is_active": True,
                },
            )
    yield engine
    engine.dispose()


@pytest.fixture
def saved_filter_sets(saved_filter_set_engine, runtime_settings):
    return UserService(saved_filter_set_engine, settings=runtime_settings)


def _seed(engine, firebase_uid, count, *, name_prefix="Set"):
    """Insert ``count`` rows directly so cap tests do not pay for the service."""

    created = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with engine.begin() as connection:
        for index in range(count):
            connection.execute(
                SavedFilterSet.__table__.insert(),
                {
                    "firebase_uid": firebase_uid,
                    "name": f"{name_prefix} {index}",
                    "query_string": QUERY_STRING,
                    "created_at": created + timedelta(minutes=index),
                    "updated_at": created + timedelta(minutes=index),
                },
            )


# --- storage ---------------------------------------------------------------


def test_the_table_is_owned_by_an_account_and_cascades_on_delete(
    saved_filter_set_engine,
):
    inspector = inspect(saved_filter_set_engine)
    foreign_keys = inspector.get_foreign_keys("saved_filter_sets")

    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "users"
    assert foreign_keys[0]["referred_columns"] == ["firebase_uid"]
    assert foreign_keys[0]["constrained_columns"] == ["firebase_uid"]
    assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"


def test_deleting_an_account_removes_its_saved_filter_sets(saved_filter_set_engine):
    _seed(saved_filter_set_engine, OWNER, 2)
    _seed(saved_filter_set_engine, OTHER, 1, name_prefix="Other")

    with saved_filter_set_engine.begin() as connection:
        connection.execute(
            User.__table__.delete().where(User.__table__.c.firebase_uid == OWNER)
        )

    with saved_filter_set_engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT firebase_uid FROM saved_filter_sets")
        ).scalars().all()
    assert remaining == [OTHER]


def test_a_saved_filter_set_cannot_name_an_account_that_does_not_exist(
    saved_filter_set_engine,
):
    with pytest.raises(Exception) as failure:
        _seed(saved_filter_set_engine, "ghost-uid", 1)

    assert "FOREIGN KEY" in str(failure.value).upper()


def test_the_database_refuses_a_duplicate_name_that_differs_only_in_case(
    saved_filter_set_engine,
):
    _seed(saved_filter_set_engine, OWNER, 1)

    with pytest.raises(IntegrityError):
        with saved_filter_set_engine.begin() as connection:
            connection.execute(
                SavedFilterSet.__table__.insert(),
                {
                    "firebase_uid": OWNER,
                    "name": "set 0",
                    "query_string": QUERY_STRING,
                    "created_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
                },
            )


def test_two_accounts_may_hold_the_same_name(saved_filter_set_engine):
    _seed(saved_filter_set_engine, OWNER, 1)
    _seed(saved_filter_set_engine, OTHER, 1)

    with saved_filter_set_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT COUNT(*) FROM saved_filter_sets WHERE name = 'Set 0'")
        ).scalar()
    assert stored == 2


# --- service: create and list ---------------------------------------------


def test_a_saved_filter_set_round_trips_through_the_list(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Jokic at home", query_string=QUERY_STRING
    )

    assert set(created) == {"id", "name", "query_string", "created_at", "updated_at"}
    assert created["name"] == "Jokic at home"
    assert created["query_string"] == QUERY_STRING
    assert saved_filter_sets.list_saved_filter_sets(OWNER) == [created]


def test_the_list_is_newest_first(saved_filter_sets):
    first = saved_filter_sets.create_saved_filter_set(
        OWNER, name="First", query_string=QUERY_STRING
    )
    second = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Second", query_string=QUERY_STRING
    )
    third = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Third", query_string=QUERY_STRING
    )

    listed = saved_filter_sets.list_saved_filter_sets(OWNER)

    assert [item["id"] for item in listed] == [third["id"], second["id"], first["id"]]


def test_a_name_is_stored_trimmed(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="  Padded name  ", query_string=QUERY_STRING
    )

    assert created["name"] == "Padded name"


def test_the_same_query_string_may_be_saved_under_another_name(saved_filter_sets):
    saved_filter_sets.create_saved_filter_set(
        OWNER, name="Original", query_string=QUERY_STRING
    )
    saved_filter_sets.create_saved_filter_set(
        OWNER, name="Duplicate query", query_string=QUERY_STRING
    )

    assert len(saved_filter_sets.list_saved_filter_sets(OWNER)) == 2


def test_a_duplicate_name_is_refused_case_insensitively(saved_filter_sets):
    saved_filter_sets.create_saved_filter_set(
        OWNER, name="Jokic at home", query_string=QUERY_STRING
    )

    with pytest.raises(ConflictError):
        saved_filter_sets.create_saved_filter_set(
            OWNER, name="jokic AT home", query_string=QUERY_STRING
        )

    assert len(saved_filter_sets.list_saved_filter_sets(OWNER)) == 1


def test_a_duplicate_name_in_another_account_is_allowed(saved_filter_sets):
    saved_filter_sets.create_saved_filter_set(
        OWNER, name="Shared name", query_string=QUERY_STRING
    )

    saved_filter_sets.create_saved_filter_set(
        OTHER, name="Shared name", query_string=QUERY_STRING
    )

    assert len(saved_filter_sets.list_saved_filter_sets(OTHER)) == 1


def test_the_account_cap_refuses_one_more_saved_filter_set(
    saved_filter_sets, saved_filter_set_engine
):
    _seed(saved_filter_set_engine, OWNER, SAVED_FILTER_SET_LIMIT)

    with pytest.raises(ConflictError):
        saved_filter_sets.create_saved_filter_set(
            OWNER, name="One too many", query_string=QUERY_STRING
        )

    assert (
        len(saved_filter_sets.list_saved_filter_sets(OWNER)) == SAVED_FILTER_SET_LIMIT
    )


def test_another_accounts_rows_do_not_count_towards_the_cap(
    saved_filter_sets, saved_filter_set_engine
):
    _seed(saved_filter_set_engine, OTHER, SAVED_FILTER_SET_LIMIT, name_prefix="Other")

    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Mine", query_string=QUERY_STRING
    )

    assert created["name"] == "Mine"


# --- service: validation ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        None,
        42,
        "x" * (SAVED_FILTER_SET_NAME_MAX_LENGTH + 1),
        " " + "x" * SAVED_FILTER_SET_NAME_MAX_LENGTH + "y ",
    ],
)
def test_an_unusable_name_is_refused(saved_filter_sets, name):
    with pytest.raises(InvalidInputError):
        saved_filter_sets.create_saved_filter_set(
            OWNER, name=name, query_string=QUERY_STRING
        )


def test_a_name_at_the_length_limit_is_accepted(saved_filter_sets):
    name = "x" * SAVED_FILTER_SET_NAME_MAX_LENGTH

    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name=name, query_string=QUERY_STRING
    )

    assert created["name"] == name


@pytest.mark.parametrize(
    "query_string",
    [
        "",
        None,
        42,
        "?player=Jokic",
        "https://statsplus.app/?player=Jokic",
        "http://statsplus.app/?player=Jokic",
        "//statsplus.app/?player=Jokic",
        "/game-logs?player=Jokic",
        "player=Jokic#saved",
        "player=Nikola Jokic",
        "player=Jokic\n",
        "x" * (SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH + 1),
    ],
)
def test_a_query_string_that_is_not_bare_is_refused(saved_filter_sets, query_string):
    with pytest.raises(InvalidInputError):
        saved_filter_sets.create_saved_filter_set(
            OWNER, name="Rejected", query_string=query_string
        )


def test_parameter_names_inside_the_query_string_are_not_judged(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER,
        name="Unknown parameters",
        query_string="not_a_real_filter=1&another=%5B%22a%22%5D&flag",
    )

    assert created["query_string"] == "not_a_real_filter=1&another=%5B%22a%22%5D&flag"


def test_a_query_string_at_the_length_limit_is_accepted(saved_filter_sets):
    query_string = "p=" + "x" * (SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH - 2)

    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="At the limit", query_string=query_string
    )

    assert created["query_string"] == query_string


# --- service: rename -------------------------------------------------------


def test_renaming_changes_the_name_and_advances_updated_at(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Before", query_string=QUERY_STRING
    )

    renamed = saved_filter_sets.rename_saved_filter_set(
        OWNER, created["id"], name="  After  "
    )

    assert renamed["name"] == "After"
    assert renamed["query_string"] == created["query_string"]
    assert renamed["created_at"] == created["created_at"]
    assert renamed["updated_at"] >= created["updated_at"]
    assert saved_filter_sets.list_saved_filter_sets(OWNER) == [renamed]


def test_renaming_to_a_case_variant_of_its_own_name_is_allowed(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Home splits", query_string=QUERY_STRING
    )

    renamed = saved_filter_sets.rename_saved_filter_set(
        OWNER, created["id"], name="HOME SPLITS"
    )

    assert renamed["name"] == "HOME SPLITS"


def test_renaming_onto_another_saved_name_is_refused(saved_filter_sets):
    saved_filter_sets.create_saved_filter_set(
        OWNER, name="Taken", query_string=QUERY_STRING
    )
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Free", query_string=QUERY_STRING
    )

    with pytest.raises(ConflictError):
        saved_filter_sets.rename_saved_filter_set(OWNER, created["id"], name="taken")


def test_renaming_rejects_an_unusable_name(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Before", query_string=QUERY_STRING
    )

    with pytest.raises(InvalidInputError):
        saved_filter_sets.rename_saved_filter_set(OWNER, created["id"], name="  ")


def test_renaming_an_unknown_saved_filter_set_is_not_found(saved_filter_sets):
    with pytest.raises(ResourceNotFoundError):
        saved_filter_sets.rename_saved_filter_set(OWNER, 4242, name="Anything")


# --- service: delete -------------------------------------------------------


def test_deleting_removes_it_from_the_list(saved_filter_sets):
    kept = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Kept", query_string=QUERY_STRING
    )
    removed = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Removed", query_string=QUERY_STRING
    )

    saved_filter_sets.delete_saved_filter_set(OWNER, removed["id"])

    assert saved_filter_sets.list_saved_filter_sets(OWNER) == [kept]


def test_deleting_frees_the_name_for_reuse(saved_filter_sets):
    created = saved_filter_sets.create_saved_filter_set(
        OWNER, name="Recycled", query_string=QUERY_STRING
    )
    saved_filter_sets.delete_saved_filter_set(OWNER, created["id"])

    replacement = saved_filter_sets.create_saved_filter_set(
        OWNER, name="recycled", query_string=QUERY_STRING
    )

    assert replacement["name"] == "recycled"


def test_deleting_an_unknown_saved_filter_set_is_not_found(saved_filter_sets):
    with pytest.raises(ResourceNotFoundError):
        saved_filter_sets.delete_saved_filter_set(OWNER, 4242)


# --- service: account scoping ---------------------------------------------


def test_another_accounts_saved_filter_set_is_never_listed(saved_filter_sets):
    saved_filter_sets.create_saved_filter_set(
        OTHER, name="Private", query_string=QUERY_STRING
    )

    assert saved_filter_sets.list_saved_filter_sets(OWNER) == []


def test_another_accounts_saved_filter_set_cannot_be_renamed(saved_filter_sets):
    foreign = saved_filter_sets.create_saved_filter_set(
        OTHER, name="Private", query_string=QUERY_STRING
    )

    with pytest.raises(ResourceNotFoundError):
        saved_filter_sets.rename_saved_filter_set(OWNER, foreign["id"], name="Stolen")

    assert saved_filter_sets.list_saved_filter_sets(OTHER)[0]["name"] == "Private"


def test_another_accounts_saved_filter_set_cannot_be_deleted(saved_filter_sets):
    foreign = saved_filter_sets.create_saved_filter_set(
        OTHER, name="Private", query_string=QUERY_STRING
    )

    with pytest.raises(ResourceNotFoundError):
        saved_filter_sets.delete_saved_filter_set(OWNER, foreign["id"])

    assert len(saved_filter_sets.list_saved_filter_sets(OTHER)) == 1


# --- routes ----------------------------------------------------------------


ITEM = {
    "id": 7,
    "name": "Jokic at home",
    "query_string": QUERY_STRING,
    "created_at": "2026-08-23T12:00:00+00:00",
    "updated_at": "2026-08-23T12:00:00+00:00",
}


def _assert_error(response, status, code, message):
    assert response.status_code == status
    assert response.get_json()["error"] == {"code": code, "message": message}


@pytest.fixture
def user_service(monkeypatch):
    """Swap the route module's service handle for a stub."""

    from app.routes import user_routes

    stub = SimpleNamespace()
    monkeypatch.setattr(user_routes, "user_service", stub)
    return stub


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/user/saved-filter-sets", "get"),
        ("/api/user/saved-filter-sets", "post"),
        ("/api/user/saved-filter-sets/7", "patch"),
        ("/api/user/saved-filter-sets/7", "delete"),
    ],
)
def test_saved_filter_set_routes_reject_missing_authentication(
    client, authenticate, path, method
):
    authenticate()

    response = getattr(client, method)(path)

    assert response.status_code == 401


def test_list_route_returns_the_callers_saved_filter_sets(
    client, authenticate, user_service
):
    headers = authenticate()
    asked = []
    user_service.list_saved_filter_sets = lambda uid: asked.append(uid) or [ITEM]

    response = client.get("/api/user/saved-filter-sets", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "saved_filter_sets": [ITEM]}
    assert asked == ["test-uid"]


def test_create_route_returns_the_created_item(client, authenticate, user_service):
    headers = authenticate()
    submitted = {}

    def capture(firebase_uid, *, name, query_string):
        submitted.update(
            firebase_uid=firebase_uid, name=name, query_string=query_string
        )
        return ITEM

    user_service.create_saved_filter_set = capture

    response = client.post(
        "/api/user/saved-filter-sets",
        headers=headers,
        json={"name": "Jokic at home", "query_string": QUERY_STRING},
    )

    assert response.status_code == 201
    assert response.get_json() == {"success": True, "saved_filter_set": ITEM}
    assert submitted == {
        "firebase_uid": "test-uid",
        "name": "Jokic at home",
        "query_string": QUERY_STRING,
    }


def test_create_route_rejects_a_body_that_is_not_an_object(
    client, authenticate, user_service
):
    headers = authenticate()

    response = client.post("/api/user/saved-filter-sets", headers=headers)

    _assert_error(
        response, 400, "invalid_input", "No saved filter set data was provided."
    )


def test_create_route_reports_a_duplicate_name_as_a_conflict(
    client, authenticate, user_service
):
    headers = authenticate()

    def refuse(firebase_uid, *, name, query_string):
        raise ConflictError("A saved filter set with that name already exists.")

    user_service.create_saved_filter_set = refuse

    response = client.post(
        "/api/user/saved-filter-sets",
        headers=headers,
        json={"name": "Taken", "query_string": QUERY_STRING},
    )

    _assert_error(
        response,
        409,
        "operation_conflict",
        "A saved filter set with that name already exists.",
    )


def test_create_route_reports_invalid_input_as_a_bad_request(
    client, authenticate, user_service
):
    headers = authenticate()

    def refuse(firebase_uid, *, name, query_string):
        raise InvalidInputError("Bad query string.")

    user_service.create_saved_filter_set = refuse

    response = client.post(
        "/api/user/saved-filter-sets",
        headers=headers,
        json={"name": "Rejected", "query_string": "?a=1"},
    )

    _assert_error(response, 400, "invalid_input", "Bad query string.")


def test_create_route_reports_an_unexpected_failure_safely(
    client, authenticate, user_service
):
    headers = authenticate()

    def explode(firebase_uid, *, name, query_string):
        raise RuntimeError("database is down")

    user_service.create_saved_filter_set = explode

    response = client.post(
        "/api/user/saved-filter-sets",
        headers=headers,
        json={"name": "Boom", "query_string": QUERY_STRING},
    )

    _assert_error(
        response, 500, "operation_failed", "Failed to save the filter set."
    )


def test_rename_route_returns_the_updated_item(client, authenticate, user_service):
    headers = authenticate()
    submitted = {}

    def capture(firebase_uid, saved_filter_set_id, *, name):
        submitted.update(
            firebase_uid=firebase_uid,
            saved_filter_set_id=saved_filter_set_id,
            name=name,
        )
        return ITEM

    user_service.rename_saved_filter_set = capture

    response = client.patch(
        "/api/user/saved-filter-sets/7", headers=headers, json={"name": "Renamed"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "saved_filter_set": ITEM}
    assert submitted == {
        "firebase_uid": "test-uid",
        "saved_filter_set_id": 7,
        "name": "Renamed",
    }


def test_rename_route_rejects_a_missing_body(client, authenticate, user_service):
    headers = authenticate()

    response = client.patch("/api/user/saved-filter-sets/7", headers=headers)

    _assert_error(
        response, 400, "invalid_input", "No saved filter set data was provided."
    )


def test_rename_route_reports_a_foreign_id_as_not_found(
    client, authenticate, user_service
):
    headers = authenticate()

    def refuse(firebase_uid, saved_filter_set_id, *, name):
        raise ResourceNotFoundError("The requested saved filter set was not found.")

    user_service.rename_saved_filter_set = refuse

    response = client.patch(
        "/api/user/saved-filter-sets/7", headers=headers, json={"name": "Stolen"}
    )

    _assert_error(
        response,
        404,
        "resource_not_found",
        "The requested saved filter set was not found.",
    )


def test_delete_route_confirms_the_removal(client, authenticate, user_service):
    headers = authenticate()
    removed = []
    user_service.delete_saved_filter_set = lambda uid, item_id: removed.append(
        (uid, item_id)
    )

    response = client.delete("/api/user/saved-filter-sets/7", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert removed == [("test-uid", 7)]


def test_delete_route_reports_a_foreign_id_as_not_found(
    client, authenticate, user_service
):
    headers = authenticate()

    def refuse(firebase_uid, saved_filter_set_id):
        raise ResourceNotFoundError("The requested saved filter set was not found.")

    user_service.delete_saved_filter_set = refuse

    response = client.delete("/api/user/saved-filter-sets/7", headers=headers)

    _assert_error(
        response,
        404,
        "resource_not_found",
        "The requested saved filter set was not found.",
    )


def test_a_non_numeric_identifier_is_not_found(client, authenticate, user_service):
    headers = authenticate()

    response = client.delete("/api/user/saved-filter-sets/not-an-id", headers=headers)

    assert response.status_code == 404
