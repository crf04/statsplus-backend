"""Offline contract tests that run recorded fixtures through the production
parse seams (#12).

The fixtures capture the wire shapes as they arrive from the providers.  Each
parse seam below is the exact code a live response flows through, so these
tests pin the provider response contract without any network access: a valid
fixture produces a normalized frame, and malformed or error fixtures surface as
``ProviderResponseError`` (a provider ``malformed`` failure, not an application
error).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.providers.nba_stats import REQUIRED_GAME_LOG_COLUMNS
from app.providers.pbp_game_logs import (
    PBPGameLogAdapter,
    PBP_GAME_LOG_COLUMNS,
)
from app.services.nba_stats_adapter import parse_recorded_game_logs
from app.services.pbp_stats_adapter import PBPTotalsAdapter
from app.utils import telemetry

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    with (FIXTURES_ROOT / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.mark.parametrize(
    "fixture",
    [
        "nba_stats/game_logs.valid.json",
        "nba_stats/player_game_logs.playoffs.json",
        "pbp_stats/totals.valid.json",
        "pbp_stats/game_logs.valid.json",
        "pbp_stats/game_stats.valid.json",
    ],
)
def test_recorded_fixtures_have_the_documented_shape(fixture):
    payload = _load(fixture)
    assert isinstance(payload, dict)
    assert json.dumps(payload)  # fixture must remain valid JSON


def test_recorded_nba_game_logs_parse_through_the_live_path():
    telemetry.clear_recorded_provider_events()
    frame = parse_recorded_game_logs(_load("nba_stats/game_logs.valid.json"))

    assert isinstance(frame, pd.DataFrame)
    assert len(frame) == 2
    assert list(frame.columns) == [
        "SEASON_YEAR",
        "PLAYER_ID",
        "PLAYER_NAME",
        "TEAM_ABBREVIATION",
        "GAME_ID",
        "GAME_DATE",
        "MATCHUP",
        "MIN",
        "FGM",
        "FGA",
        "FG_PCT",
        "PTS",
        "REB",
        "AST",
    ]
    assert set(frame["PLAYER_NAME"]) == {"LeBron James"}
    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == telemetry.PROVIDER_NBA_STATS
    assert event["outcome"] == telemetry.OUTCOME_SUCCESS


def test_recorded_playoff_player_logs_parse_through_the_live_path():
    payload = _load("nba_stats/player_game_logs.playoffs.json")
    frame = parse_recorded_game_logs(payload)

    assert payload["parameters"] == {
        "Season": "2025-26",
        "SeasonType": "Playoffs",
    }
    assert len(frame) == 10
    assert set(frame["PLAYER_ID"]) == {
        101,
        103,
        104,
        105,
        106,
        202,
        107,
        108,
        109,
        110,
    }
    assert set(frame["PLAYER_NAME"]) == {
        "Canonical One",
        "Canonical 103",
        "Canonical 104",
        "Canonical 105",
        "Canonical 106",
        "Canonical Two",
        "Canonical 107",
        "Canonical 108",
        "Canonical 109",
        "Canonical 110",
    }
    assert set(REQUIRED_GAME_LOG_COLUMNS).union({"PLAYER_ID"}).issubset(
        frame.columns
    )
    assert set(frame["GAME_ID"]) == {"0042500001"}
    assert frame["TEAM_ID"].value_counts().to_dict() == {1: 5, 2: 5}
    assert (frame["MIN"] > 0).all()


def test_malformed_nba_game_logs_raise_provider_response_error():
    telemetry.clear_recorded_provider_events()
    with pytest.raises(telemetry.ProviderResponseError):
        parse_recorded_game_logs(_load("nba_stats/game_logs.malformed.json"))

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == telemetry.PROVIDER_NBA_STATS
    assert event["outcome"] == telemetry.OUTCOME_MALFORMED


def test_recorded_pbp_totals_parse_through_the_live_path():
    frame = PBPTotalsAdapter.parse_totals(_load("pbp_stats/totals.valid.json"))

    assert isinstance(frame, pd.DataFrame)
    assert len(frame) == 2
    assert set(frame.columns) == {
        "Name",
        "Season",
        "Team",
        "GP",
        "Assists",
        "AssistPoints",
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }


def test_recorded_bounded_opponent_totals_carry_window_verification_fields():
    frame = PBPTotalsAdapter.parse_totals(
        _load("pbp_stats/opponent_bos_bounded.valid.json"),
        data_type="opponent",
    )

    assert frame.loc[0, "TeamId"] == "1610612738"
    assert frame.loc[0, "SecondsPlayed"] == 63660.0
    assert frame.loc[0, "GamesPlayed"] == 22
    assert frame.loc[0, "Assists"] == 525


def test_malformed_pbp_totals_are_provider_response_error():
    with pytest.raises(telemetry.ProviderResponseError):
        PBPTotalsAdapter.parse_totals(_load("pbp_stats/totals.malformed.json"))


def test_error_only_pbp_totals_are_provider_response_error():
    with pytest.raises(telemetry.ProviderResponseError):
        PBPTotalsAdapter.parse_totals(_load("pbp_stats/totals.error.json"))


def test_nba_fixture_has_two_rows_and_two_games():
    payload = _load("nba_stats/game_logs.valid.json")
    rows = payload["resultSets"][0]["rowSet"]
    assert len(rows) == 2
    assert rows[0][2] == "LeBron James"


def test_pbp_fixture_carries_shareable_columns_only():
    payload = _load("pbp_stats/totals.valid.json")
    assert set(payload["multi_row_table_data"][0]) == {
        "Name",
        "Season",
        "Team",
        "GP",
        "Assists",
        "AssistPoints",
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }


def test_recorded_pbp_game_logs_parse_through_the_live_path():
    payload = _load("pbp_stats/game_logs.valid.json")
    frame = PBPGameLogAdapter.parse_game_logs(
        payload,
        entity_id="2544",
        player_name="LeBron James",
    )

    assert isinstance(frame, pd.DataFrame)
    assert len(frame) == 2
    assert set(PBP_GAME_LOG_COLUMNS).issubset(frame.columns)
    # The live per-player rows do not carry player identity on the wire.
    assert "EntityId" not in payload["multi_row_table_data"][0]
    assert "Name" not in payload["multi_row_table_data"][0]
    assert "TeamId" not in payload["multi_row_table_data"][0]
    assert frame.loc[0, "EntityId"] == "2544"
    assert frame.loc[0, "Name"] == "LeBron James"
    assert frame.loc[0, "Minutes"] == "34:12"
    assert frame.loc[0, "Team"] == "LAL"
    assert frame.loc[0, "Opponent"] == "SAS"
    assert frame.loc[0, "GameId"] == "0022400001"
    assert frame.loc[0, "Date"] == "2024-11-15"
    assert frame.loc[0, "FtPoints"] == 4
    assert frame.loc[0, "FTA"] == 4
    assert frame.loc[0, "DefRebounds"] == 7
    assert frame.loc[0, "Fouls"] == 2


def test_recorded_pbp_game_stats_parse_exclude_team_summary_and_attach_identity():
    payload = _load("pbp_stats/game_stats.valid.json")
    frame = PBPGameLogAdapter.parse_game_stats(payload, game_id="0022400001")

    # The provider's team-summary row (EntityId 0 / Name Team) is excluded.
    assert len(frame) == 3
    assert "0" not in set(frame["EntityId"].astype(str))
    assert frame.loc[0, "EntityId"] == "2544"
    assert frame.loc[0, "Name"] == "LeBron James"
    assert frame.loc[0, "GameId"] == "0022400001"
    assert frame.loc[0, "Date"] == "2024-11-15"
    assert frame.loc[0, "Team"] == "LAL"
    assert frame.loc[0, "Opponent"] == "SAS"
    assert frame.loc[2, "Team"] == "SAS"
    assert frame.loc[2, "Opponent"] == "LAL"
    assert frame.loc[2, "Points"] == 25


@pytest.mark.parametrize(
    "payload",
    [
        {"multi_row_table_data": "nope"},
        {"multi_row_table_data": [{"GameId": "1"}]},
        {"multi_row_table_data": [None]},
        None,
        {"stats": {"Home": {"FullGame": [None]}, "Away": {"FullGame": []}}},
        {"stats": {"Home": {"FullGame": []}, "Away": {}}},
    ],
)
def test_malformed_pbp_game_log_shapes_are_provider_response_error(payload):
    with pytest.raises(telemetry.ProviderResponseError):
        PBPGameLogAdapter.parse_game_logs(
            payload, entity_id="2544", player_name="LeBron James"
        )
    with pytest.raises(telemetry.ProviderResponseError):
        PBPGameLogAdapter.parse_game_stats(payload, game_id="0022400001")
