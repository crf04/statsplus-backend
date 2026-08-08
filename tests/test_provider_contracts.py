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
        "pbp_stats/totals.valid.json",
    ],
)
def test_recorded_fixtures_have_the_documented_shape(fixture):
    payload = _load(fixture)
    assert isinstance(payload, dict)
    assert json.dumps(payload)  # fixture must remain valid JSON


def test_recorded_nba_game_logs_parse_through_the_live_path():
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


def test_malformed_nba_game_logs_raise_provider_response_error():
    with pytest.raises(telemetry.ProviderResponseError):
        parse_recorded_game_logs(_load("nba_stats/game_logs.malformed.json"))


def test_recorded_pbp_totals_parse_through_the_live_path():
    frame = PBPTotalsAdapter.parse_totals(_load("pbp_stats/totals.valid.json"))

    assert isinstance(frame, pd.DataFrame)
    assert len(frame) == 2
    assert set(frame.columns) == {
        "Name",
        "Season",
        "Team",
        "GP",
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }


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
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }