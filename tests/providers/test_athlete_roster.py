"""Offline contract tests for the canonical athlete roster provider seam."""

from __future__ import annotations

import pandas as pd
import pytest

from app.config.settings import AuthenticationSettings, CacheSettings, RuntimeSettings
from app.errors import ProviderUnavailableError
from app.providers.nba_stats import NBAStatsAdapter, normalize_player_roster
from app.utils.telemetry import (
    clear_recorded_provider_events,
    get_recorded_provider_events,
)


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
    )


def test_roster_fetch_is_season_scoped_and_emits_closed_telemetry_operation():
    captured: dict[str, object] = {}

    class RecordedRosterEndpoint:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [
                        {
                            "PERSON_ID": 23,
                            "DISPLAY_FIRST_LAST": "LeBron James",
                            "ROSTERSTATUS": 1,
                            "FROM_YEAR": 2003,
                            "TO_YEAR": 2025,
                            "TEAM_ID": 1610612747,
                            "TEAM_NAME": "Los Angeles Lakers",
                            "TEAM_ABBREVIATION": "LAL",
                        }
                    ]
                )
            ]

    clear_recorded_provider_events()
    adapter = NBAStatsAdapter(
        settings=_settings(),
        roster_endpoint_factory=RecordedRosterEndpoint,
    )

    frame = adapter.get_player_roster(season="2024-25")

    assert captured["season"] == "2024-25"
    assert captured["is_only_current_season"] == 0
    assert frame.to_dict(orient="records") == [
        {
            "player_id": 23,
            "display_name": "LeBron James",
            "roster_status": "active",
            "is_active": True,
            "is_active_for_season": True,
            "season": "2024-25",
            "team_id": 1610612747,
            "team_name": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
        }
    ]
    events = get_recorded_provider_events()
    assert [event["operation"] for event in events] == ["player_roster"]


def test_roster_normalization_distinguishes_inactive_and_historical_players():
    frame = normalize_player_roster(
        pd.DataFrame(
            [
                {
                    "PERSON_ID": 1,
                    "DISPLAY_FIRST_LAST": "Active Player",
                    "ROSTERSTATUS": 1,
                    "FROM_YEAR": 2020,
                    "TO_YEAR": 2025,
                    "TEAM_ID": 10,
                    "TEAM_NAME": "Team A",
                    "TEAM_ABBREVIATION": "A",
                },
                {
                    "PERSON_ID": 2,
                    "DISPLAY_FIRST_LAST": "Inactive Player",
                    "ROSTERSTATUS": 0,
                    "FROM_YEAR": 2020,
                    "TO_YEAR": 2025,
                    "TEAM_ID": 20,
                    "TEAM_NAME": "Team B",
                    "TEAM_ABBREVIATION": "B",
                },
                {
                    "PERSON_ID": 3,
                    "DISPLAY_FIRST_LAST": "Historical Player",
                    "ROSTERSTATUS": 0,
                    "FROM_YEAR": 2010,
                    "TO_YEAR": 2018,
                    "TEAM_ID": 30,
                    "TEAM_NAME": "Team C",
                    "TEAM_ABBREVIATION": "C",
                },
            ]
        ),
        season="2024-25",
    )

    assert frame.set_index("player_id")["roster_status"].to_dict() == {
        1: "active",
        2: "inactive",
        3: "historical",
    }
    assert frame.set_index("player_id")["is_active_for_season"].to_dict() == {
        1: True,
        2: False,
        3: False,
    }


def test_empty_roster_is_malformed_and_records_provider_failure():
    class EmptyRosterEndpoint:
        def __init__(self, **_kwargs):
            pass

        def get_data_frames(self):
            return [pd.DataFrame(columns=["PERSON_ID", "DISPLAY_FIRST_LAST"])]

    clear_recorded_provider_events()
    adapter = NBAStatsAdapter(
        settings=_settings(), roster_endpoint_factory=EmptyRosterEndpoint
    )

    with pytest.raises(ProviderUnavailableError):
        adapter.get_player_roster(season="2024-25")

    events = get_recorded_provider_events()
    assert events[0]["operation"] == "player_roster"
    assert events[0]["outcome"] == "malformed"
