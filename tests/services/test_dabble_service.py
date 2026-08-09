from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.errors import InvalidInputError
from app.services.dabble_service import DabbleService


def test_service_filters_dabble_lines():
    provider = Mock()
    provider.fetch_lines.return_value = [
        {"player_name": "LeBron James", "stat": "points", "line": 25.5},
        {"player_name": "LeBron James", "stat": "rebounds", "line": 7.5},
        {"player_name": "Coby White", "stat": "points", "line": 22.5},
    ]
    service = DabbleService(provider, max_fixtures_per_request=5)

    result = service.get_lines(
        competition="NBA",
        player="lebron",
        stat="POINTS",
        fixture_limit=2,
    )

    assert result == {
        "provider": "dabble",
        "count": 1,
        "lines": [
            {"player_name": "LeBron James", "stat": "points", "line": 25.5}
        ],
    }
    provider.fetch_lines.assert_called_once_with(
        competition="NBA",
        competition_id=None,
        fixture_id=None,
        fixture_limit=2,
        include_in_play=False,
    )


def test_service_requires_exactly_one_fixture_selector():
    service = DabbleService(Mock(), max_fixtures_per_request=5)

    with pytest.raises(InvalidInputError):
        service.get_lines()
    with pytest.raises(InvalidInputError):
        service.get_lines(competition="NBA", fixture_id="fixture-1")


def test_service_caps_fixture_fanout():
    service = DabbleService(Mock(), max_fixtures_per_request=3)

    with pytest.raises(InvalidInputError):
        service.get_lines(competition="NBA", fixture_limit=4)


def test_service_accepts_statsplus_combination_aliases():
    provider = Mock()
    provider.fetch_lines.return_value = [
        {
            "player_name": "LeBron James",
            "stat": "points+rebounds+assists",
            "line": 45.5,
        },
        {"player_name": "LeBron James", "stat": "points", "line": 25.5},
    ]
    service = DabbleService(provider, max_fixtures_per_request=5)

    result = service.get_lines(competition="NBA", stat="PRA")

    assert result["count"] == 1
    assert result["lines"][0]["line"] == 45.5
