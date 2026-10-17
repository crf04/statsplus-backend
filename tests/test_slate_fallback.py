"""``fallback=previous_game_day`` on the Slate, the edges and Target resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import InvalidInputError
from app.services.slate_edges import SlateEdgesService
from app.services.slate_fallback import read_slate
from app.services.slate_service import SlateService
from app.services.target_resolution import TargetResolutionService
from tests.support.slate_catalog import NoNeighbourEvents

TODAY, PREVIOUS = "2026-04-15", "2026-04-12"


def _game(game_id):
    return {
        "game_id": game_id,
        "status": {"state": "final", "label": "Final"},
        "scheduled_at": "2026-04-12T23:00:00+00:00",
        "away_team": {"team_id": 100, "tricode": "AWY", "name": None},
        "home_team": {"team_id": 200, "tricode": "HOM", "name": None},
    }


class DaySlates:
    """A Slate holding games on chosen days; today is ``TODAY``."""

    def __init__(self, days, previous=PREVIOUS):
        self.days = days
        self.previous = previous
        self.calls = []

    def _slate_for(self, requested_date):
        self.calls.append(requested_date)
        date = requested_date or TODAY
        return {
            "slate_date": date,
            "previous_game_date": self.previous,
            "next_game_date": None,
            "games": list(self.days.get(date, [])),
            "provenance": {"generation": [], "sources": {}},
        }

    def get_slate(self, requested_date=None, *, fallback=None):
        return read_slate(self._slate_for, requested_date, fallback)


def test_an_empty_today_answers_as_the_previous_game_date():
    slates = DaySlates({PREVIOUS: [_game("G1")]})

    slate = slates.get_slate(fallback="previous_game_day")

    assert (slate["slate_date"], slate["fallback"]) == (PREVIOUS, "previous_game_day")
    assert [game["game_id"] for game in slate["games"]] == ["G1"]
    assert slates.calls == [None, PREVIOUS]


def test_a_today_with_games_answers_as_no_parameters():
    slates = DaySlates({TODAY: [_game("T1")], PREVIOUS: [_game("G1")]})

    slate = slates.get_slate(fallback="previous_game_day")

    assert (slate["slate_date"], slate["fallback"]) == (TODAY, None)
    assert slates.calls == [None]


def test_an_empty_today_without_a_previous_game_date_answers_as_no_parameters():
    slates = DaySlates({}, previous=None)

    slate = slates.get_slate(fallback="previous_game_day")

    assert (slate["slate_date"], slate["games"], slate["fallback"]) == (TODAY, [], None)
    assert slates.calls == [None]


def test_without_fallback_the_slate_says_so_and_reads_the_date_asked():
    slates = DaySlates({})

    slate = slates.get_slate("2026-04-01")

    assert (slate["slate_date"], slate["fallback"]) == ("2026-04-01", None)
    assert slates.calls == ["2026-04-01"]


@pytest.mark.parametrize(
    ("date", "fallback"),
    [
        ("2026-04-12", "previous_game_day"),
        (None, "next_game_day"),
        (None, ""),
        (None, "PREVIOUS_GAME_DAY"),
        ("2026-04-12", "bogus"),
    ],
)
def test_a_date_with_fallback_or_any_other_value_is_invalid_input(date, fallback):
    slates = DaySlates({PREVIOUS: [_game("G1")]})

    with pytest.raises(InvalidInputError):
        slates.get_slate(date, fallback=fallback)

    assert slates.calls == []


def _edges_payload():
    return {"players": [], "teams": []}


class Matchups:
    def __init__(self):
        self.calls = []

    def get_matchup(self, *, game_id):
        self.calls.append(game_id)
        return _edges_payload()


def test_edges_answer_the_previous_game_day_and_name_it():
    matchups = Matchups()
    slates = DaySlates({PREVIOUS: [_game("G1")]})

    edges = SlateEdgesService(slates=slates, matchups=matchups).get_edges(fallback="previous_game_day")

    assert edges["slate_date"] == PREVIOUS
    assert [game["game_id"] for game in edges["games"]] == ["G1"]
    assert matchups.calls == ["G1"]
    assert "fallback" not in edges


def test_edges_refuse_a_date_with_fallback():
    service = SlateEdgesService(slates=DaySlates({}), matchups=Matchups())

    with pytest.raises(InvalidInputError):
        service.get_edges("2026-04-12", fallback="previous_game_day")


def test_resolve_answers_the_previous_game_day_and_refuses_a_date_with_it():
    service = TargetResolutionService(
        targets=Mock(list_targets=Mock(return_value=[])),
        slates=DaySlates({PREVIOUS: [_game("G1")]}),
        matchups=Matchups(),
    )

    resolved = service.resolve("uid", fallback="previous_game_day")

    assert resolved["slate_date"] == PREVIOUS
    with pytest.raises(InvalidInputError):
        service.resolve("uid", requested_date=PREVIOUS, fallback="previous_game_day")


# --- the real SlateService and the HTTP seam --------------------------------


def _event(game_id, scheduled_at):
    return {
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "status_text": "Final",
        "status_code": 3,
        "is_postponed": False,
        "classification": "Regular Season",
        "away_team": {"id": 100, "name": "Away", "tricode": "AWY"},
        "home_team": {"id": 200, "name": "Home", "tricode": "HOM"},
    }


class Catalog(NoNeighbourEvents):
    def count_events(self, season):
        return 1

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2026-04-15T06:00:00+00:00", "event_count": 1}

    def get_events_between(self, season, starts_at, ends_at):
        if starts_at.date().isoformat() == "2026-04-12":
            return [_event("0022500999", "2026-04-12T23:00:00+00:00")]
        return []


@pytest.fixture
def real_slate(dependencies, monkeypatch):
    monkeypatch.setattr(
        "app.services.slate_service.neighbouring_game_dates",
        lambda settings, catalog, date: (PREVIOUS, None) if date.isoformat() == TODAY else (None, TODAY),
    )
    dependencies.slate_service = SlateService(
        Catalog(),
        settings=RuntimeSettings(environment="testing", nba=NBASeasonSettings(current_season="2025-26")),
        clock=lambda: datetime(2026, 4, 15, 16, tzinfo=UTC),
    )
    return dependencies.slate_service


def test_the_slate_route_falls_back_and_always_states_the_field(client, real_slate):
    fell_back = client.get("/api/games/slate?fallback=previous_game_day").get_json()
    plain = client.get("/api/games/slate").get_json()
    dated = client.get(f"/api/games/slate?date={PREVIOUS}").get_json()

    assert (fell_back["slate_date"], fell_back["fallback"]) == (PREVIOUS, "previous_game_day")
    assert [game["game_id"] for game in fell_back["games"]] == ["0022500999"]
    assert (plain["slate_date"], plain["games"], plain["fallback"]) == (TODAY, [], None)
    assert (dated["slate_date"], dated["fallback"]) == (PREVIOUS, None)


@pytest.mark.parametrize(
    "query",
    [
        f"?date={PREVIOUS}&fallback=previous_game_day",
        "?fallback=next_game_day",
        "?fallback=",
    ],
)
def test_the_slate_and_edges_routes_refuse_a_bad_fallback(client, dependencies, real_slate, query):
    dependencies.slate_edges_service = SlateEdgesService(slates=real_slate, matchups=Matchups())

    for path in ("/api/games/slate", "/api/games/slate/edges"):
        response = client.get(path + query)

        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "invalid_input"


def test_the_edges_route_falls_back_to_the_previous_game_day(client, dependencies, real_slate):
    matchups = Matchups()
    dependencies.slate_edges_service = SlateEdgesService(slates=real_slate, matchups=matchups)

    body = client.get("/api/games/slate/edges?fallback=previous_game_day").get_json()

    assert body["slate_date"] == PREVIOUS
    assert [game["game_id"] for game in body["games"]] == ["0022500999"]


def test_the_resolve_route_falls_back_and_refuses_a_bad_fallback(
    client, dependencies, authenticate, real_slate
):
    dependencies.target_resolution_service = TargetResolutionService(
        targets=Mock(list_targets=Mock(return_value=[])),
        slates=real_slate,
        matchups=Matchups(),
    )
    headers = authenticate()

    good = client.get("/api/user/targets/resolve?fallback=previous_game_day", headers=headers)
    both = client.get(
        f"/api/user/targets/resolve?date={PREVIOUS}&fallback=previous_game_day", headers=headers
    )
    other = client.get("/api/user/targets/resolve?fallback=tomorrow", headers=headers)

    assert (good.status_code, good.get_json()["slate_date"]) == (200, PREVIOUS)
    assert [both.status_code, other.status_code] == [400, 400]
    assert both.get_json()["error"]["code"] == "invalid_input"
