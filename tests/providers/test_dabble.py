"""Offline contract tests for the Dabble DFS line adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest
import requests

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError
from app.providers.dabble import DabbleAdapter
from app.utils import telemetry


FIXTURES = Path(__file__).parents[1] / "fixtures" / "dabble"


def _payload(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


@pytest.fixture(autouse=True)
def _clear_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


@pytest.fixture
def adapter():
    session = Mock()
    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        providers={
            "dabble_connect_timeout_seconds": 1.5,
            "dabble_read_timeout_seconds": 4.0,
            "dabble_max_fixtures_per_request": 4,
        },
    )
    return DabbleAdapter(settings, session=session), session


def test_fetch_lines_resolves_competition_and_normalizes_player_props(adapter):
    dabble, session = adapter
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(_payload("fixture_details.valid.json")),
    ]

    lines = dabble.fetch_lines(competition="NBA", fixture_limit=2)

    assert lines == [
        {
            "provider": "dabble",
            "fixture_id": "fixture-1",
            "fixture": "Los Angeles Lakers @ Chicago Bulls",
            "starts_at": "2026-08-09T16:30:00.000Z",
            "competition_id": "090c2877-4d13-4f6e-8faf-886092153c58",
            "competition": "NBA",
            "sport_id": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f",
            "sport": "Basketball",
            "player_id": "player-1",
            "player_name": "LeBron James",
            "team_id": "team-1",
            "team": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
            "position": "F",
            "market_id": "market-1",
            "selection_id": "selection-over",
            "stats": ["points", "rebounds", "assists"],
            "stat": "points+rebounds+assists",
            "line": 45.5,
            "direction": "over",
            "multiplier": 1.5,
            "multiplier_scope": "selection",
        },
        {
            "provider": "dabble",
            "fixture_id": "fixture-1",
            "fixture": "Los Angeles Lakers @ Chicago Bulls",
            "starts_at": "2026-08-09T16:30:00.000Z",
            "competition_id": "090c2877-4d13-4f6e-8faf-886092153c58",
            "competition": "NBA",
            "sport_id": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f",
            "sport": "Basketball",
            "player_id": "player-1",
            "player_name": "LeBron James",
            "team_id": "team-1",
            "team": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
            "position": "F",
            "market_id": "market-1",
            "selection_id": "selection-under",
            "stats": ["points", "rebounds", "assists"],
            "stat": "points+rebounds+assists",
            "line": 45.5,
            "direction": "under",
            "multiplier": None,
            "multiplier_scope": None,
        },
        {
            "provider": "dabble",
            "fixture_id": "fixture-1",
            "fixture": "Los Angeles Lakers @ Chicago Bulls",
            "starts_at": "2026-08-09T16:30:00.000Z",
            "competition_id": "090c2877-4d13-4f6e-8faf-886092153c58",
            "competition": "NBA",
            "sport_id": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f",
            "sport": "Basketball",
            "player_id": "player-2",
            "player_name": "Coby White",
            "team_id": "team-2",
            "team": "Chicago Bulls",
            "team_abbreviation": "CHI",
            "position": "G",
            "market_id": "market-2",
            "selection_id": "selection-points-over",
            "stats": ["points"],
            "stat": "points",
            "line": 22.0,
            "direction": "over",
            "multiplier": None,
            "multiplier_scope": None,
        },
    ]
    assert session.get.call_args_list == [
        call(
            f"{DabbleAdapter.BASE_URL}/competitions",
            params={"name": "NBA"},
            timeout=(1.5, 4.0),
        ),
        call(
            f"{DabbleAdapter.BASE_URL}/frontend-api/competitions/"
            "090c2877-4d13-4f6e-8faf-886092153c58/sport-fixtures",
            params={"includeInPlay": "false"},
            timeout=(1.5, 4.0),
        ),
        call(
            f"{DabbleAdapter.BASE_URL}/frontend-api/sport-fixtures/details/fixture-1",
            params=None,
            timeout=(1.5, 4.0),
        ),
    ]
    assert [event["operation"] for event in telemetry.get_recorded_provider_events()] == [
        "competition_lookup",
        "competition_fixtures",
        "fixture_details",
    ]


def test_fetch_lines_can_target_one_fixture_without_discovery(adapter):
    dabble, session = adapter
    session.get.return_value = FakeResponse(_payload("fixture_details.valid.json"))

    lines = dabble.fetch_lines(fixture_id="fixture-1")

    assert len(lines) == 3
    session.get.assert_called_once()


def test_list_competitions_resolves_a_friendly_sport_name(adapter):
    dabble, session = adapter
    session.get.side_effect = [
        FakeResponse(
            {
                "status": "success",
                "data": [
                    {
                        "id": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f",
                        "name": "Basketball",
                    }
                ],
            }
        ),
        FakeResponse(
            {
                "status": "success",
                "data": {
                    "activeCompetitions": [
                        {
                            "id": "090c2877-4d13-4f6e-8faf-886092153c58",
                            "name": "NBA",
                            "sportId": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f",
                            "country": "USA",
                        }
                    ]
                },
            }
        ),
    ]

    competitions = dabble.list_competitions(sport="basketball")

    assert competitions == [
        {
            "id": "090c2877-4d13-4f6e-8faf-886092153c58",
            "name": "NBA",
            "sport_id": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f",
            "sport": "Basketball",
            "country": "USA",
            "featured": False,
        }
    ]
    assert session.get.call_args_list[1] == call(
        f"{DabbleAdapter.BASE_URL}/competitions/active",
        params={"sportId": "01408294-cb34-4cc0-8ab1-504f5c4c6e1f"},
        timeout=(1.5, 4.0),
    )


def test_malformed_player_props_becomes_safe_provider_error(adapter):
    dabble, session = adapter
    session.get.return_value = FakeResponse(
        {"sportFixtureDetail": {"id": "fixture-1", "playerProps": "bad"}}
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        dabble.fetch_lines(fixture_id="fixture-1")

    assert raised.value.code == "provider_unavailable"
    assert telemetry.get_recorded_provider_events()[0]["outcome"] == "malformed"


@pytest.mark.parametrize("multiplier", [0, -1, float("inf"), "1.5x"])
def test_invalid_line_multiplier_becomes_safe_provider_error(adapter, multiplier):
    dabble, session = adapter
    payload = _payload("fixture_details.valid.json")
    payload["sportFixtureDetail"]["playerProps"][0]["multiplier"] = multiplier
    session.get.return_value = FakeResponse(payload)

    with pytest.raises(ProviderUnavailableError):
        dabble.fetch_lines(fixture_id="fixture-1")

    assert telemetry.get_recorded_provider_events()[0]["outcome"] == "malformed"


def test_timeout_becomes_safe_provider_error(adapter):
    dabble, session = adapter
    session.get.side_effect = requests.ReadTimeout("private upstream detail")

    with pytest.raises(ProviderUnavailableError) as raised:
        dabble.fetch_lines(fixture_id="fixture-1")

    assert raised.value.code == "provider_unavailable"
    assert telemetry.get_recorded_provider_events()[0]["outcome"] == "timeout"
