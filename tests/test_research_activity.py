"""Weekly active researchers (crf04/statsplus#213).

These tests drive the real Flask routes with a real ``UserService`` and
``ResearchActivityService`` on a migrated SQLite database; only the Firebase
token verifier, the clock and the research services behind each route are
replaced.  Counting happens after a response is sent, so every request here is
closed before the database is read.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, text

from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.services.research_activity import (
    RESEARCH_ENDPOINTS,
    ResearchActivityService,
)
from app.services.user_service import UserService

ADMIN_CLAIMS = {"admin": True, "role": "admin", "roles": ["admin"]}
UTC = timezone.utc

#: Wednesday 12:00 Eastern; its Monday-to-Sunday week starts on 2026-10-12.
NOON_WEDNESDAY = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 14)

CORNER_THREE = {
    "base": "shot_zones",
    "slice_key": "Corner 3",
    "comparator": "at_or_above",
    "threshold": 0.4,
}

#: Every route that counts, as a user (or the connector) calls it.
COUNTED = [
    ("get", "/api/games/matchup?game_id=0022600001", {}),
    ("get", "/api/games/matchup/selection?game_id=0022600001&player_id=7", {}),
    ("get", "/api/matchups/unscheduled?player_id=7&opponent=BOS", {}),
    ("get", "/api/players/profile?player_name=LeBron%20James&category=Shooting", {}),
    ("get", "/api/teams/stats?category=Traditional&team=Boston%20Celtics", {}),
    ("get", "/api/games/game_logs?player_name=LeBron%20James", {}),
    ("post", "/api/nl-query", {"json": {"query": "lebron over 25 points"}}),
    (
        "post",
        "/api/user/targets/preview",
        {"json": {"opponent": "OKC", "qualifiers": [CORNER_THREE]}},
    ),
    ("get", "/api/user/targets/7/backtest", {}),
    ("get", "/api/user/targets/backtests", {}),
]

#: Successful authenticated reads that are not research actions.
NOT_COUNTED = [
    ("get", "/api/games/slate", {}),
    ("get", "/api/games/slate/edges", {}),
    # The Slate page reads this on every load, so it cannot count.
    ("get", "/api/user/targets/resolve", {}),
    ("get", "/api/games/seasons", {}),
    ("get", "/api/players", {}),
    ("get", "/api/teams", {}),
    ("get", "/api/diet/baselines", {}),
    ("get", "/api/user/targets", {}),
    ("get", "/api/user/profile", {}),
    ("post", "/api/user/activity/ping", {}),
]


OK_ITEM = {"target_id": 7, "status": "ok", "backtest": {"players": []}}
UNCACHED_ITEM = {"target_id": 8, "status": "uncached"}
ERROR_ITEM = {"target_id": 9, "status": "error", "error": {"code": "x", "message": "y"}}


class Clock:
    """A settable stand-in for the wall clock."""

    def __init__(self, instant: datetime):
        self.instant = instant

    def __call__(self) -> datetime:
        if self.instant is None:
            raise RuntimeError("clock unavailable")
        return self.instant


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'research.db'}")
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def clock():
    return Clock(NOON_WEDNESDAY)


@pytest.fixture
def activity(engine, clock):
    return ResearchActivityService(engine, clock=clock)


@pytest.fixture
def client(app, dependencies, engine, runtime_settings, activity, monkeypatch):
    import app.utils.auth as auth

    dependencies.user_service = UserService(engine, settings=runtime_settings)
    dependencies.research_activity_service = activity
    dependencies.matchup_service = Mock(name="matchup_service")
    dependencies.matchup_service.get_matchup.return_value = {"game_id": "0022600001"}
    dependencies.matchup_service.get_unscheduled_matchup.return_value = {"unscheduled": True}
    dependencies.matchup_selection_service = Mock(name="matchup_selection_service")
    dependencies.matchup_selection_service.get_selection.return_value = {"selection": True}
    dependencies.slate_service.get_slate.return_value = {"games": []}
    dependencies.slate_edges_service = Mock(name="slate_edges_service")
    dependencies.slate_edges_service.get_edges.return_value = {"edges": []}
    dependencies.player_service.get_player_profile.return_value = [{"PTS": 1}]
    dependencies.team_service.get_team_stats.return_value = [{"TEAM": "BOS"}]
    dependencies.game_service.get_filtered_logs.return_value = {"game_logs": []}
    dependencies.game_service.list_seasons.return_value = {"seasons": []}
    dependencies.nl_service.process_query.return_value = {"filters": {}}
    dependencies.diet_baselines_service = Mock(name="diet_baselines_service")
    dependencies.diet_baselines_service.get.return_value = {"shares": {}}
    dependencies.target_resolution_service = Mock(name="target_resolution_service")
    dependencies.target_resolution_service.resolve.return_value = {"targets": []}
    dependencies.target_backtest_service = Mock(name="target_backtest_service")
    dependencies.target_backtest_service.backtest.return_value = ({"backtest": 1}, "hit")
    dependencies.target_backtest_service.backtest_all.return_value = (
        {"backtests": [OK_ITEM]},
        "hit",
    )
    dependencies.target_preview_service = Mock(name="target_preview_service")
    dependencies.target_preview_service.preview.side_effect = lambda draft, season=None: {
        "target": draft,
        "today": None,
    }
    dependencies.player_service.get_all_players.return_value = []
    dependencies.team_service.get_all_teams.return_value = []
    app.extensions["dependencies"] = dependencies

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": token,
            "name": f"Name {token}",
            "email": f"{token}@example.com",
            **(ADMIN_CLAIMS if token.startswith("admin") else {}),
        },
    )
    return app.test_client()


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _call(client, method, path, token=None, **kwargs):
    """Make a request, finish it the way a server does, and return it."""
    headers = _as(token) if token else {}
    response = getattr(client, method)(path, headers=headers, **kwargs)
    response.get_data()
    response.close()
    return response


def _tally(engine) -> list[tuple[str, date, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT firebase_uid, game_day, actions FROM research_activity_days "
                "ORDER BY firebase_uid, game_day"
            )
        ).all()
    return [(uid, date.fromisoformat(str(day)), actions) for uid, day, actions in rows]


def _report(client, query=""):
    response = _call(client, "get", f"/api/admin/research-activity{query}", "admin-1")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


#: The anonymized id each test account has in the report: the first 12 hex
#: digits of SHA-256("statsplus-research-report:<uid>"), written out so the
#: scheme cannot change unnoticed.
REPORT_ID = {
    "two-days": "37b584b9e385",
    "one-day": "ab91f9370d2f",
    "straddler": "8d9f1eb89cb4",
    "same-week": "a924dbc2d9da",
    "old": "9d5114d3aa8e",
    "oldest-in": "4e9d2d66b988",
    "u1": "63de1d3f69de",
    "alice": "f3053176d27f",
    "from-ref": "66291d2f638b",
    "from-utm": "c5686fe40fb2",
    "none": "1641db313df5",
    "never-landed": "9711e077e025",
    "outside": "c3b21ff26d99",
    "insider": "d94a143b9e38",
    "buddy": "46cb6a0a888d",
    "friend-of-friend": "a4abc23f7df2",
    "three": "ba23580554f1",
    "four": "de0b1d5685ce",
    "two": "bb906e10de63",
    "gap": "48a6a07c862e",
}


def _account(report, uid):
    """The report row for ``uid``."""
    [row] = [row for row in report["accounts"] if row["id"] == REPORT_ID[uid]]
    return row


def _make_user(activity, uid):
    UserService(activity.engine).create_or_update_user(
        {"uid": uid, "email": f"{uid}@example.com", "name": uid}
    )


# --- which routes count ------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "kwargs"), COUNTED)
def test_a_successful_authenticated_research_read_adds_one_to_todays_count(
    client, engine, method, path, kwargs
):
    response = _call(client, method, path, "u1", **kwargs)

    assert response.status_code == 200
    assert _tally(engine) == [("u1", TODAY, 1)]


@pytest.mark.parametrize(("method", "path", "kwargs"), NOT_COUNTED)
def test_slate_home_and_reference_reads_do_not_count(client, engine, method, path, kwargs):
    response = _call(client, method, path, "u1", **kwargs)

    assert response.status_code == 200
    assert _tally(engine) == []


def test_the_counted_table_covers_every_counted_endpoint(app):
    adapter = app.url_map.bind("localhost")
    covered = set()
    for method, path, _ in COUNTED:
        endpoint, _arguments = adapter.match(path.split("?")[0], method=method.upper())
        covered.add((method.upper(), endpoint))

    assert covered == RESEARCH_ENDPOINTS


def test_an_anonymous_read_of_an_optional_auth_route_counts_for_nobody(
    client, engine, activity
):
    # Accounts the count could wrongly attach to if it guessed an owner.
    for uid in ("u1", "admin-1", "dev-user"):
        _make_user(activity, uid)
    profile = _call(client, "get", "/api/players/profile?player_name=A&category=Shooting")
    team = _call(client, "get", "/api/teams/stats?category=Traditional&team=Boston")

    assert (profile.status_code, team.status_code) == (200, 200)
    assert _tally(engine) == []


def test_reads_that_fail_are_not_counted(client, engine, dependencies):
    dependencies.matchup_service.get_matchup.side_effect = ResourceNotFoundError("gone")

    missing = _call(client, "get", "/api/games/matchup?game_id=0022600001", "u1")
    invalid = _call(client, "get", "/api/games/matchup", "u1")
    unauthenticated = _call(client, "get", "/api/games/matchup?game_id=0022600001")

    assert (missing.status_code, invalid.status_code, unauthenticated.status_code) == (
        404,
        400,
        401,
    )
    assert _tally(engine) == []


def test_a_server_error_is_not_counted(client, engine, dependencies):
    dependencies.matchup_service.get_matchup.side_effect = RuntimeError("boom")

    response = _call(client, "get", "/api/games/matchup?game_id=0022600001", "u1")

    assert response.status_code == 500
    assert _tally(engine) == []


def test_a_rate_limited_read_is_not_counted(client, engine):
    body = {"json": {"opponent": "OKC", "qualifiers": [CORNER_THREE]}}
    for _ in range(30):
        assert _call(client, "post", "/api/user/targets/preview", "u1", **body).status_code == 200

    limited = _call(client, "post", "/api/user/targets/preview", "u1", **body)

    assert limited.status_code == 429
    assert _tally(engine) == [("u1", TODAY, 30)]


@pytest.mark.parametrize(
    "items",
    [
        [],
        [UNCACHED_ITEM],
        [ERROR_ITEM],
        [UNCACHED_ITEM, ERROR_ITEM],
    ],
)
def test_a_batch_backtest_that_served_no_backtest_is_not_research(
    client, engine, dependencies, items
):
    dependencies.target_backtest_service.backtest_all.return_value = (
        {"backtests": items},
        "miss",
    )

    response = _call(client, "get", "/api/user/targets/backtests", "u1")

    assert response.status_code == 200
    assert _tally(engine) == []


def test_a_batch_backtest_that_served_any_backtest_counts_once(client, engine, dependencies):
    dependencies.target_backtest_service.backtest_all.return_value = (
        {"backtests": [UNCACHED_ITEM, ERROR_ITEM, OK_ITEM]},
        "miss",
    )

    _call(client, "get", "/api/user/targets/backtests", "u1")

    assert _tally(engine) == [("u1", TODAY, 1)]


def test_a_waitlisted_account_is_refused_and_not_counted(client, engine):
    UserService(engine).set_signups_open(False)

    response = _call(client, "get", "/api/games/game_logs?player_name=A", "queued")

    assert response.status_code == 403
    assert _tally(engine) == []


def test_the_local_auth_bypass_user_is_not_counted(client, engine, activity, monkeypatch):
    import app.utils.auth as auth

    # A row the count could attach to, as a bypass user has in local development.
    _make_user(activity, "dev-user")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)
    monkeypatch.setattr(auth, "_auth_bypass_enabled", lambda: True)

    response = _call(client, "get", "/api/games/matchup?game_id=0022600001")

    assert response.status_code == 200
    assert _tally(engine) == []


# --- the tally ---------------------------------------------------------------


def test_reads_on_one_date_share_a_row_and_each_account_has_its_own(client, engine):
    for _ in range(3):
        _call(client, "get", "/api/games/game_logs?player_name=A", "u1")
    _call(client, "get", "/api/games/matchup?game_id=0022600001", "u1")
    _call(client, "get", "/api/games/matchup?game_id=0022600001", "u2")

    assert _tally(engine) == [("u1", TODAY, 4), ("u2", TODAY, 1)]


@pytest.mark.parametrize(
    ("instant", "game_day"),
    [
        # Sunday night to Monday morning, EDT (UTC-4).
        (datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC), date(2026, 10, 18)),
        (datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC), date(2026, 10, 19)),
        # The same boundary in EST (UTC-5).
        (datetime(2026, 12, 7, 4, 59, 59, tzinfo=UTC), date(2026, 12, 6)),
        (datetime(2026, 12, 7, 5, 0, 0, tzinfo=UTC), date(2026, 12, 7)),
        # Clocks fall back on Nov 1, so that Eastern date lasts 25 hours, and
        # 01:30 happens twice (EDT, then EST).
        (datetime(2026, 11, 1, 3, 59, 59, tzinfo=UTC), date(2026, 10, 31)),
        (datetime(2026, 11, 1, 4, 0, 0, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 5, 30, 0, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 6, 30, 0, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 2, 5, 0, 0, tzinfo=UTC), date(2026, 11, 2)),
        # Clocks spring forward on Mar 14, 2027: 02:00 EST becomes 03:00 EDT.
        (datetime(2027, 3, 14, 6, 59, 59, tzinfo=UTC), date(2027, 3, 14)),
        (datetime(2027, 3, 14, 7, 0, 0, tzinfo=UTC), date(2027, 3, 14)),
        (datetime(2027, 3, 15, 3, 59, 59, tzinfo=UTC), date(2027, 3, 14)),
        (datetime(2027, 3, 15, 4, 0, 0, tzinfo=UTC), date(2027, 3, 15)),
    ],
)
def test_the_game_day_is_the_us_eastern_calendar_date(client, engine, clock, instant, game_day):
    clock.instant = instant

    _call(client, "get", "/api/games/game_logs?player_name=A", "u1")

    assert _tally(engine) == [("u1", game_day, 1)]


def test_the_date_is_taken_when_the_read_arrives_not_when_it_is_counted(
    client, engine, clock
):
    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)
    response = client.get("/api/games/game_logs?player_name=A", headers=_as("u1"))
    clock.instant = datetime(2026, 10, 19, 4, 0, 1, tzinfo=UTC)
    response.close()

    assert _tally(engine) == [("u1", date(2026, 10, 18), 1)]


def test_the_date_is_the_day_the_read_arrived_even_if_the_route_runs_past_midnight(
    client, engine, clock, dependencies
):
    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)  # Sunday 23:59:59 ET

    def slow_matchup(*, game_id):
        clock.instant = datetime(2026, 10, 19, 4, 0, 30, tzinfo=UTC)  # now Monday
        return {"game_id": game_id}

    dependencies.matchup_service.get_matchup.side_effect = slow_matchup

    _call(client, "get", "/api/games/matchup?game_id=0022600001", "u1")

    assert _tally(engine) == [("u1", date(2026, 10, 18), 1)]


def test_the_date_is_the_day_the_read_arrived_even_if_sign_in_runs_past_midnight(
    client, engine, clock, monkeypatch
):
    import app.utils.auth as auth

    clock.instant = datetime(2026, 10, 19, 3, 59, 59, tzinfo=UTC)  # Sunday 23:59:59 ET

    def verify_after_midnight(token):
        clock.instant = datetime(2026, 10, 19, 4, 0, 1, tzinfo=UTC)  # now Monday
        return {"uid": token, "name": token, "email": f"{token}@example.com"}

    monkeypatch.setattr(auth, "verify_firebase_token", verify_after_midnight)

    _call(client, "get", "/api/games/game_logs?player_name=A", "u1")

    assert _tally(engine) == [("u1", date(2026, 10, 18), 1)]


@pytest.mark.parametrize("method", ["head", "options"])
def test_only_the_documented_methods_count(client, engine, method):
    response = _call(client, method, "/api/games/game_logs?player_name=A", "u1")

    assert response.status_code == 200
    assert _tally(engine) == []


def test_a_failure_scheduling_the_count_cannot_fail_the_read(
    client, engine, dependencies, clock, caplog
):
    class NoWriter:
        counting = True

        def today(self):
            return date(2026, 10, 14)

    dependencies.research_activity_service = NoWriter()

    with caplog.at_level(logging.WARNING, logger="app"):
        response = _call(client, "get", "/api/games/game_logs?player_name=A", "u1")

    assert response.status_code == 200
    assert response.get_json() == {"game_logs": []}
    assert _tally(engine) == []
    assert "Could not schedule a research action count" in caplog.text


def test_counting_waits_until_the_response_has_been_sent(client, engine):
    response = client.get("/api/games/game_logs?player_name=A", headers=_as("u1"))

    assert response.status_code == 200
    assert _tally(engine) == []

    response.close()

    assert _tally(engine) == [("u1", TODAY, 1)]


def test_a_failed_count_never_changes_the_read(client, engine, caplog):
    baseline = _call(client, "get", "/api/games/game_logs?player_name=A", "u1")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE research_activity_days"))

    with caplog.at_level(logging.WARNING, logger="app.services.research_activity"):
        response = _call(client, "get", "/api/games/game_logs?player_name=A", "u1")

    assert response.status_code == 200
    assert response.get_data() == baseline.get_data()
    assert "Could not count a research action" in caplog.text


def test_a_clock_failure_cannot_fail_the_read(client, engine, clock, caplog):
    clock.instant = None

    with caplog.at_level(logging.WARNING, logger="app"):
        response = _call(client, "get", "/api/games/game_logs?player_name=A", "u1")

    assert response.status_code == 200
    assert response.get_json() == {"game_logs": []}
    assert _tally(engine) == []
    assert "Could not schedule a research action count" in caplog.text


def test_counting_is_off_for_the_read_only_demo_database(app, dependencies, engine, client):
    dependencies.research_activity_service = ResearchActivityService(engine, counting=False)

    response = _call(client, "get", "/api/games/game_logs?player_name=A", "u1")

    assert response.status_code == 200
    assert _tally(engine) == []


def test_a_lost_first_insert_race_still_counts_both_reads(activity, engine):
    _make_user(activity, "u1")
    activity.record("u1", TODAY)
    skipped = []

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def hide_the_row_from_the_first_update(
        connection, cursor, statement, parameters, context, executemany
    ):
        # The competing request committed the day's row after this request's
        # UPDATE looked for it: make the first UPDATE miss it, so the INSERT
        # collides with the primary key and the retry finds the row.
        if statement.startswith("UPDATE research_activity_days") and not skipped:
            skipped.append(True)
            statement = statement.replace("WHERE", "WHERE 1 = 0 AND", 1)
        return statement, parameters

    activity.record("u1", TODAY)

    assert skipped == [True]
    assert _tally(engine) == [("u1", TODAY, 2)]


def test_a_count_for_an_unknown_account_fails_without_raising_when_quiet(activity, engine):
    with pytest.raises(Exception):
        activity.record("ghost", TODAY)

    activity.record_quietly("ghost", TODAY)

    assert _tally(engine) == []


# --- the report --------------------------------------------------------------


def _act(activity, uid, *days):
    """Give ``uid`` one research action on each Eastern date in ``days``."""
    _make_user(activity, uid)
    for day in days:
        activity.record(uid, day)


def _week(report, uid, index):
    return _account(report, uid)["weeks"][index]


def test_two_game_days_in_a_week_make_it_active_and_one_does_not(client, activity):
    _act(activity, "two-days", date(2026, 10, 13), date(2026, 10, 14))
    _act(activity, "one-day", date(2026, 10, 14))
    for _ in range(5):
        activity.record("one-day", date(2026, 10, 14))

    report = _report(client)

    two_days = _week(report, "two-days", 3)
    assert (two_days["start"], two_days["game_days"], two_days["actions"]) == (
        "2026-10-12",
        2,
        2,
    )
    assert two_days["active"] is True
    one_day = _week(report, "one-day", 3)
    assert (one_day["game_days"], one_day["actions"], one_day["active"]) == (1, 6, False)


def test_weeks_run_monday_to_sunday_eastern(client, activity):
    # Sunday and the Monday after fall in different weeks; Monday and the
    # Sunday six days later fall in the same one.
    _act(activity, "straddler", date(2026, 10, 11), date(2026, 10, 12))
    _act(activity, "same-week", date(2026, 10, 5), date(2026, 10, 11))

    report = _report(client)

    assert [week["active"] for week in _account(report, "straddler")["weeks"]] == [
        False,
        False,
        False,
        False,
    ]
    assert [week["game_days"] for week in _account(report, "straddler")["weeks"]] == [
        0,
        0,
        1,
        1,
    ]
    assert [week["active"] for week in _account(report, "same-week")["weeks"]] == [
        False,
        False,
        True,
        False,
    ]


def test_the_report_covers_the_last_four_weeks_and_nothing_older(client, activity):
    _act(activity, "old", date(2026, 9, 19), date(2026, 9, 20))  # the week before
    _act(activity, "oldest-in", date(2026, 9, 21), date(2026, 9, 27))

    report = _report(client)

    assert [week["start"] for week in report["weeks"]] == [
        "2026-09-21",
        "2026-09-28",
        "2026-10-05",
        "2026-10-12",
    ]
    assert [week["end"] for week in report["weeks"]] == [
        "2026-09-27",
        "2026-10-04",
        "2026-10-11",
        "2026-10-18",
    ]
    assert [week["complete"] for week in report["weeks"]] == [True, True, True, False]
    assert _account(report, "old")["active_weeks"] == 0
    assert _week(report, "oldest-in", 0)["active"] is True


def test_the_default_report_ends_with_the_current_eastern_week(client, clock):
    # Sunday 23:30 Eastern is already Monday in UTC.
    clock.instant = datetime(2026, 10, 19, 3, 30, tzinfo=UTC)

    report = _report(client)

    assert report["weeks"][-1] == {"start": "2026-10-12", "end": "2026-10-18", "complete": False}

    clock.instant = datetime(2026, 10, 19, 4, 1, tzinfo=UTC)  # Monday 00:01 Eastern

    report = _report(client)

    assert report["weeks"][-2] == {"start": "2026-10-12", "end": "2026-10-18", "complete": True}
    assert report["weeks"][-1] == {"start": "2026-10-19", "end": "2026-10-25", "complete": False}


def test_as_of_picks_the_last_week_and_ignores_later_activity(client, activity):
    _act(activity, "u1", date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 14))

    report = _report(client, "?as_of=2026-10-11")

    assert [week["start"] for week in report["weeks"]] == [
        "2026-09-14",
        "2026-09-21",
        "2026-09-28",
        "2026-10-05",
    ]
    assert [week["complete"] for week in report["weeks"]] == [True, True, True, True]
    assert [week["active"] for week in _account(report, "u1")["weeks"]] == [
        False,
        False,
        False,
        True,
    ]


def test_the_last_weeks_sunday_counts_and_the_next_mondays_activity_does_not(client, activity):
    # Mon 10-05 and Sun 10-11 close out the final week for as_of Sunday 10-11;
    # Mon 10-12 starts the next week and is outside the report.
    _act(activity, "same-week", date(2026, 10, 5), date(2026, 10, 11))
    _act(activity, "straddler", date(2026, 10, 11), date(2026, 10, 12))

    report = _report(client, "?as_of=2026-10-11")

    assert [w["game_days"] for w in _account(report, "same-week")["weeks"]] == [0, 0, 0, 2]
    assert [w["active"] for w in _account(report, "same-week")["weeks"]] == [False, False, False, True]
    assert [w["game_days"] for w in _account(report, "straddler")["weeks"]] == [0, 0, 0, 1]


@pytest.mark.parametrize(
    "query",
    ["?as_of=yesterday", "?as_of=2026-02-30", "?as_of=20261011", "?as_of=", "?week=1"],
)
def test_the_report_refuses_a_bad_query(client, query):
    response = _call(client, "get", f"/api/admin/research-activity{query}", "admin-1")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_the_report_shows_each_accounts_first_touch_source(client, activity):
    for uid, body in (
        ("from-ref", {"ref": "x-picks", "utm_source": "twitter"}),
        ("from-utm", {"utm_source": "reddit"}),
        ("none", {}),
    ):
        _make_user(activity, uid)
        response = _call(client, "post", "/api/user/attribution", uid, json=body)
        assert response.status_code == 201
    _make_user(activity, "never-landed")

    report = _report(client)

    def source(uid):
        row = _account(report, uid)
        return (row["source"], row["ref"], row["utm_source"])

    assert source("from-ref") == ("x-picks", "x-picks", "twitter")
    assert source("from-utm") == ("reddit", None, "reddit")
    assert source("none") == (None, None, None)
    assert source("never-landed") == (None, None, None)


def test_the_report_names_no_one(client, activity):
    _act(activity, "alice", date(2026, 10, 13), date(2026, 10, 14))

    response = _call(client, "get", "/api/admin/research-activity", "admin-1")

    body = response.get_data(as_text=True)
    assert "alice" not in body
    assert "example.com" not in body
    assert REPORT_ID["alice"] in [row["id"] for row in response.get_json()["accounts"]]


def _three_weeks(activity, uid, weeks=(0, 1, 2)):
    """Two game days in each of the listed report weeks (0 is the oldest)."""
    first_monday = date(2026, 9, 21)
    days = []
    for week in weeks:
        monday = first_monday + timedelta(weeks=week)
        days += [monday, monday + timedelta(days=1)]
    _act(activity, uid, *days)


def _tag(client, uid, cohort):
    response = _call(client, "put", f"/api/admin/users/{uid}/cohort", "admin-1", json={"cohort": cohort})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_the_goal_counts_outside_accounts_active_in_three_of_four_weeks(client, activity):
    _three_weeks(activity, "three", weeks=(0, 1, 2))
    _three_weeks(activity, "four", weeks=(0, 1, 2, 3))
    _three_weeks(activity, "two", weeks=(1, 3))
    _three_weeks(activity, "gap", weeks=(0, 2, 3))

    goal = _report(client)["goal"]

    assert goal == {
        "target": 30,
        "min_active_weeks": 3,
        "friend_of_friend_cap": 10,
        "untagged_accounts": 3,
        "friend_of_friend_accounts": 0,
        "total": 3,
    }


def test_internal_and_friend_accounts_are_excluded_from_the_goal_total(client, activity):
    for uid in ("outside", "insider", "buddy", "friend-of-friend"):
        _three_weeks(activity, uid)
    assert _report(client)["goal"]["total"] == 4

    assert _tag(client, "insider", "internal")["cohort"] == "internal"
    assert _tag(client, "buddy", "friend")["cohort"] == "friend"
    assert _tag(client, "friend-of-friend", "friend_of_friend")["cohort"] == "friend_of_friend"

    report = _report(client)
    assert report["goal"]["untagged_accounts"] == 1
    assert report["goal"]["friend_of_friend_accounts"] == 1
    assert report["goal"]["total"] == 2
    assert [_account(report, uid)["cohort"] for uid in ("outside", "insider", "buddy", "friend-of-friend")] == [
        None,
        "internal",
        "friend",
        "friend_of_friend",
    ]
    # The excluded accounts are still reported, with their activity.
    assert [_account(report, uid)["active_weeks"] for uid in ("insider", "buddy")] == [3, 3]


def test_only_friends_of_friends_active_three_weeks_count(client, activity):
    _three_weeks(activity, "outside")
    _three_weeks(activity, "friend-of-friend")
    _three_weeks(activity, "two", weeks=(1, 3))
    _tag(client, "friend-of-friend", "friend_of_friend")
    _tag(client, "two", "friend_of_friend")

    goal = _report(client)["goal"]

    assert (goal["untagged_accounts"], goal["friend_of_friend_accounts"], goal["total"]) == (1, 1, 2)


def test_clearing_a_tag_returns_the_account_to_the_goal(client, activity):
    _three_weeks(activity, "u1")
    _tag(client, "u1", "friend")
    assert _report(client)["goal"]["total"] == 0

    assert _tag(client, "u1", None)["cohort"] is None

    assert _report(client)["goal"]["total"] == 1


def test_friends_of_friends_count_up_to_ten(client, activity):
    for index in range(12):
        uid = f"fof-{index:02d}"
        _three_weeks(activity, uid)
        _tag(client, uid, "friend_of_friend")
    _three_weeks(activity, "outside")

    goal = _report(client)["goal"]

    assert goal["friend_of_friend_accounts"] == 12
    assert goal["untagged_accounts"] == 1
    assert goal["total"] == 11


def test_accounts_with_no_activity_are_listed_with_inactive_weeks(client, activity):
    _make_user(activity, "u1")

    row = _account(_report(client), "u1")

    assert row["active_weeks"] == 0
    assert [week["game_days"] for week in row["weeks"]] == [0, 0, 0, 0]


def test_tagging_returns_the_report_id_and_the_admin_list_shows_the_tag(client, activity):
    _make_user(activity, "alice")

    tagged = _tag(client, "alice", "friend")

    assert tagged == {"uid": "alice", "cohort": "friend", "report_id": REPORT_ID["alice"]}
    listing = _call(client, "get", "/api/admin/users?status=active", "admin-1").get_json()
    entry = next(user for user in listing["users"] if user["uid"] == "alice")
    assert (entry["cohort"], entry["report_id"]) == ("friend", REPORT_ID["alice"])
    assert _tag(client, "alice", "friend") == tagged  # idempotent


def test_retagging_replaces_the_tag(client, activity):
    _make_user(activity, "alice")
    _tag(client, "alice", "friend")

    _tag(client, "alice", "internal")

    assert _account(_report(client), "alice")["cohort"] == "internal"


@pytest.mark.parametrize(
    "body",
    [{}, {"cohort": "vip"}, {"cohort": 3}, {"cohort": ["friend"]}, {"cohort": ""}, [], "friend"],
)
def test_tagging_refuses_anything_but_a_cohort_or_null(client, activity, body):
    _make_user(activity, "alice")

    response = _call(client, "put", "/api/admin/users/alice/cohort", "admin-1", json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    assert _account(_report(client), "alice")["cohort"] is None


def test_tagging_an_unknown_account_is_404(client):
    response = _call(client, "put", "/api/admin/users/ghost/cohort", "admin-1", json={"cohort": "friend"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("put", "/api/admin/users/alice/cohort", {"json": {"cohort": "friend"}}),
        ("get", "/api/admin/research-activity", {}),
    ],
)
def test_the_new_admin_endpoints_reject_non_admins_and_anonymous(
    client, activity, method, path, kwargs
):
    _make_user(activity, "alice")

    denied = _call(client, method, path, "plain-user", **kwargs)
    anonymous = _call(client, method, path, **kwargs)

    assert (denied.status_code, denied.get_json()["error"]["code"]) == (403, "forbidden")
    assert anonymous.status_code == 401
    assert _account(_report(client), "alice")["cohort"] is None
