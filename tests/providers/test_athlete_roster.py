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


#: One CommonTeamRoster response, with the columns NBA.com sends, for each
#: listed-position code a team can carry.  Lakers: the codes that map; Celtics:
#: the codes that do not (blank, a fine position, a missing listing).
_TEAM_ROSTER_COLUMNS = [
    "TeamID",
    "SEASON",
    "LeagueID",
    "PLAYER",
    "NICKNAME",
    "PLAYER_SLUG",
    "NUM",
    "POSITION",
    "HEIGHT",
    "WEIGHT",
    "BIRTH_DATE",
    "AGE",
    "EXP",
    "SCHOOL",
    "PLAYER_ID",
    "HOW_ACQUIRED",
]
LAKERS, CELTICS = 1610612747, 1610612738
#: (player id, name, listed position, team id)
LISTED = [
    (1, "Guard Only", "G", LAKERS),
    (2, "Guard Forward", "G-F", LAKERS),
    (3, "Forward Guard", "F-G", LAKERS),
    (4, "Forward", "F", LAKERS),
    (5, "Forward Center", "F-C", LAKERS),
    (6, "Center Forward", "C-F", LAKERS),
    (7, "Center", "C", LAKERS),
    (8, "Blank Listing", "", CELTICS),
    (9, "Fine Position", "PG", CELTICS),
    (10, "Missing Listing", None, CELTICS),
]


def _team_roster(team_id):
    rows = [
        [
            team_id,
            "2024",
            "00",
            name,
            name,
            name.lower(),
            "0",
            position,
            "6-6",
            "200",
            "",
            25,
            "3",
            "",
            pid,
            "",
        ]
        for pid, name, position, team in LISTED
        if team == team_id
    ]
    return pd.DataFrame(rows, columns=_TEAM_ROSTER_COLUMNS)


def _all_players():
    return pd.DataFrame(
        [
            {
                "PERSON_ID": pid,
                "DISPLAY_FIRST_LAST": name,
                "ROSTERSTATUS": 1,
                "FROM_YEAR": 2020,
                "TO_YEAR": 2025,
                "TEAM_ID": team,
                "TEAM_NAME": "Team",
                "TEAM_ABBREVIATION": "TM",
            }
            for pid, name, _position, team in LISTED
        ]
    )


def test_roster_fetch_is_season_scoped_and_emits_closed_telemetry_operation():
    captured: dict[str, object] = {}
    team_calls: list[dict[str, object]] = []

    class RecordedRosterEndpoint:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_data_frames(self):
            return [_all_players()]

    class RecordedTeamRosterEndpoint:
        def __init__(self, **kwargs):
            team_calls.append(kwargs)
            self.frame = _team_roster(kwargs["team_id"])

        def get_data_frames(self):
            return [self.frame]

    clear_recorded_provider_events()
    adapter = NBAStatsAdapter(
        settings=_settings(),
        roster_endpoint_factory=RecordedRosterEndpoint,
        team_roster_endpoint_factory=RecordedTeamRosterEndpoint,
    )

    frame = adapter.get_player_roster(season="2024-25")

    assert captured["season"] == "2024-25"
    assert captured["is_only_current_season"] == 0
    # One roster call per team, all for the requested season.
    assert len(team_calls) == 30
    assert {call["season"] for call in team_calls} == {"2024-25"}
    assert len({call["team_id"] for call in team_calls}) == 30
    # Each listed code lands on its player as the group the fixed rule gives it.
    assert frame.set_index("player_id")["position"].to_dict() == {
        1: "Guard",
        2: "Wing",
        3: "Wing",
        4: "Wing",
        5: "Big",
        6: "Big",
        7: "Big",
        8: None,
        9: None,
        10: None,
    }
    assert list(frame.columns) == [
        "player_id",
        "display_name",
        "roster_status",
        "is_active",
        "is_active_for_season",
        "season",
        "team_id",
        "team_name",
        "team_abbreviation",
        "position",
    ]
    events = get_recorded_provider_events()
    assert [event["operation"] for event in events] == ["player_roster"] + ["team_roster"] * 30


def test_a_player_the_team_rosters_never_list_has_no_position():
    class OnePlayerRoster:
        def __init__(self, **_kwargs):
            pass

        def get_data_frames(self):
            return [_all_players().iloc[[0]]]

    class EmptyTeamRoster:
        def __init__(self, **kwargs):
            pass

        def get_data_frames(self):
            return [pd.DataFrame(columns=_TEAM_ROSTER_COLUMNS)]

    adapter = NBAStatsAdapter(
        settings=_settings(),
        roster_endpoint_factory=OnePlayerRoster,
        team_roster_endpoint_factory=EmptyTeamRoster,
    )

    assert adapter.get_player_roster(season="2024-25")["position"].tolist() == [None]


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
    adapter = NBAStatsAdapter(settings=_settings(), roster_endpoint_factory=EmptyRosterEndpoint)

    with pytest.raises(ProviderUnavailableError):
        adapter.get_player_roster(season="2024-25")

    events = get_recorded_provider_events()
    assert events[0]["operation"] == "player_roster"
    assert events[0]["outcome"] == "malformed"
