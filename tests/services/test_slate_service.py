"""Behavioral tests for the current-season ET slate read."""

from datetime import datetime, timedelta, timezone

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import InvalidInputError, ProviderUnavailableError
from app.services.slate_service import SlateService


class RecordedCatalog:
    def __init__(self, events, freshness):
        self.events = events
        self.freshness = freshness

    def get_events(self, season):
        assert season == "2025-26"
        return list(self.events)

    def get_freshness(self, season, *, now=None):
        assert season == "2025-26"
        return dict(self.freshness)


def _event(game_id, scheduled_at, *, classification="Regular Season", **overrides):
    event = {
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "status_text": "7:00 pm ET",
        "status_code": 1,
        "is_postponed": False,
        "classification": classification,
        "away_team": {"id": 1, "name": "Away", "tricode": "AWY"},
        "home_team": {"id": 2, "name": "Home", "tricode": "HME"},
    }
    event.update(overrides)
    return event


def _service(events, *, freshness=None, now=None, schedule_max_age=None):
    settings = RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings(current_season="2025-26"),
    )
    catalog = RecordedCatalog(
        events,
        freshness
        or {
            "fresh": True,
            "last_success_at": "2026-01-02T10:00:00+00:00",
            "event_count": len(events),
        },
    )
    return SlateService(
        catalog,
        settings=settings,
        clock=lambda: now or datetime(2026, 1, 2, 15, tzinfo=timezone.utc),
        schedule_max_age=schedule_max_age,
    )


def test_slate_membership_uses_eastern_date_and_orders_tip_then_game_id():
    service = _service(
        [
            _event("003", "2026-01-03T04:30:00+00:00"),  # Jan 2, 11:30pm ET
            _event("002", "2026-01-03T00:00:00+00:00"),
            _event("001", "2026-01-03T00:00:00+00:00"),
            _event("004", "2026-01-03T05:00:00+00:00"),  # Jan 3, midnight ET
        ]
    )

    payload = service.get_slate("2026-01-02")

    assert payload["slate_date"] == "2026-01-02"
    assert [game["game_id"] for game in payload["games"]] == ["001", "002", "003"]


def test_slate_excludes_all_star_and_retains_unusual_and_postponed_games():
    service = _service(
        [
            _event("all-star", "2026-02-15T01:00:00+00:00", classification="All-Star Game"),
            _event("pre", "2026-02-15T02:00:00+00:00", classification="Preseason"),
            _event(
                "postponed",
                "2026-02-15T03:00:00+00:00",
                status_text="Postponed",
                is_postponed=True,
            ),
            _event(
                "final",
                "2026-02-15T04:00:00+00:00",
                status_text="Final",
                status_code=3,
                classification="Playoffs",
            ),
        ]
    )

    payload = service.get_slate("2026-02-14")

    assert [game["game_id"] for game in payload["games"]] == [
        "pre",
        "postponed",
        "final",
    ]
    assert payload["games"][0]["preseason"] is True
    assert payload["games"][0]["classification"] == "Preseason"
    assert payload["games"][1]["status"] == {
        "state": "postponed",
        "label": "Postponed",
    }
    assert payload["games"][2]["status"] == {"state": "final", "label": "Final"}


@pytest.mark.parametrize(
    "classification",
    ["All-Star Game", "All Star Celebrity Game", "Rising Stars All-Star Weekend"],
)
def test_slate_excludes_all_star_weekend_exhibitions(classification):
    service = _service(
        [_event("exhibition", "2026-02-15T01:00:00+00:00", classification=classification)]
    )

    assert service.get_slate("2026-02-14")["games"] == []


def test_slate_treats_unknown_as_ordinary_and_detects_preseason_game_id():
    service = _service(
        [
            _event("0022500001", "2025-10-23T00:00:00+00:00", classification="unknown"),
            _event("0012500001", "2025-10-23T01:00:00+00:00", classification="unknown"),
        ]
    )

    games = service.get_slate("2025-10-22")["games"]

    assert games[0]["classification"] is None
    assert games[0]["preseason"] is False
    assert games[1]["classification"] == "Preseason"
    assert games[1]["preseason"] is True


def test_game_id_kind_overrides_conflicting_provider_classification():
    service = _service(
        [
            _event(
                "0032500001",
                "2026-02-15T01:00:00+00:00",
                classification="Rising Stars",
            ),
            _event(
                "0012500001",
                "2026-02-15T02:00:00+00:00",
                classification="International Series",
            ),
        ]
    )

    games = service.get_slate("2026-02-14")["games"]

    assert [game["game_id"] for game in games] == ["0012500001"]
    assert games[0]["classification"] == "International Series"
    assert games[0]["preseason"] is True


def test_slate_defaults_to_today_et_and_reports_truthful_pool_degradation():
    service = _service(
        [_event("late", "2026-01-03T04:30:00+00:00")],
        now=datetime(2026, 1, 3, 4, 45, tzinfo=timezone.utc),
    )

    payload = service.get_slate()

    assert payload["slate_date"] == "2026-01-02"
    assert payload["freshness"] == {
        "schedule": {
            "status": "fresh",
            "retrieved_at": "2026-01-02T10:00:00+00:00",
        },
        "pool": {
            "status": "unavailable",
            "retrieved_at": None,
            "providers": {},
        },
    }
    assert payload["games"][0]["away_team"]["targetable_player_count"] == 0
    assert payload["games"][0]["home_team"]["targetable_player_count"] == 0


def test_schedule_freshness_uses_its_own_nightly_refresh_window():
    retrieved_at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    freshness = {
        "fresh": True,
        "last_success_at": retrieved_at.isoformat(),
        "event_count": 1,
    }

    at_boundary = _service(
        [], freshness=freshness, now=retrieved_at + timedelta(hours=30)
    ).get_slate("2026-01-02")
    past_boundary = _service(
        [],
        freshness=freshness,
        now=retrieved_at + timedelta(hours=30, microseconds=1),
    ).get_slate("2026-01-02")

    assert at_boundary["freshness"]["schedule"]["status"] == "fresh"
    assert past_boundary["freshness"]["schedule"]["status"] == "stale"


def test_schedule_freshness_accepts_a_valid_injected_max_age():
    retrieved_at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    payload = _service(
        [],
        freshness={"last_success_at": retrieved_at.isoformat(), "event_count": 1},
        now=retrieved_at + timedelta(hours=2),
        schedule_max_age=timedelta(hours=1),
    ).get_slate("2026-01-02")

    assert payload["freshness"]["schedule"]["status"] == "stale"


@pytest.mark.parametrize("schedule_max_age", ["one hour", timedelta(0)])
def test_schedule_freshness_rejects_invalid_injected_max_age(schedule_max_age):
    with pytest.raises(ValueError):
        _service([], schedule_max_age=schedule_max_age)


def test_slate_returns_an_empty_success_for_a_date_without_games():
    payload = _service(
        [_event("0022500001", "2026-01-03T00:00:00+00:00")]
    ).get_slate("2026-01-15")

    assert payload["games"] == []


@pytest.mark.parametrize("value", ["2026-1-02", "not-a-date"])
def test_slate_rejects_malformed_dates(value):
    with pytest.raises(InvalidInputError):
        _service([]).get_slate(value)


def test_slate_requires_a_stored_schedule_success_even_when_the_date_is_empty():
    service = _service(
        [],
        freshness={"fresh": False, "last_success_at": None, "event_count": 0},
    )

    with pytest.raises(ProviderUnavailableError):
        service.get_slate("2026-01-15")


def test_slate_requires_stored_events_even_when_success_metadata_exists():
    service = _service(
        [],
        freshness={
            "fresh": True,
            "last_success_at": "2026-01-02T10:00:00+00:00",
            "event_count": 0,
        },
    )

    with pytest.raises(ProviderUnavailableError):
        service.get_slate("2026-01-15")
