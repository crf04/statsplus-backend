"""Offline contract tests for the NBA Stats provider boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest
import requests
from nba_api.stats.endpoints.playergamelogs import PlayerGameLogs
from sqlalchemy import create_engine

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import ProviderUnavailableError
from app.providers.nba_stats import (
    DERIVED_GAME_LOG_COLUMNS,
    NBAStatsAdapter,
    REQUIRED_GAME_LOG_COLUMNS,
    normalize_archetype_game_logs,
    normalize_player_game_logs,
)
from app.services.game_service import GameService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nba_stats_player_game_logs.json"


def _recorded_provider_frame() -> pd.DataFrame:
    payload = json.loads(FIXTURE_PATH.read_text())
    result_set = payload["resultSets"][0]
    return pd.DataFrame(result_set["rowSet"], columns=result_set["headers"])


def _recorded_provider_result_set() -> dict:
    payload = json.loads(FIXTURE_PATH.read_text())
    return payload["resultSets"][0]


def _test_settings() -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        cache={"enabled": False},
        providers={"nba_stats_timeout_seconds": 2.5},
        nba=NBASeasonSettings(current_season="2025-26"),
    )


def test_recorded_provider_fixture_is_normalized_across_schema_drift():
    raw_frame = _recorded_provider_frame()

    normalized = normalize_player_game_logs(raw_frame)

    assert list(normalized["GAME_DATE"]) == [
        "2018-03-20T00:00:00",
        "2018-03-18T00:00:00",
        "2018-03-17T00:00:00",
        "2018-03-15T00:00:00",
        "2018-03-13T00:00:00",
        "2018-03-11T00:00:00",
    ]
    assert "SEASON_YEAR" not in normalized.columns
    assert "TEAM_NAME" not in normalized.columns
    assert set(REQUIRED_GAME_LOG_COLUMNS).issubset(normalized.columns)
    assert set(DERIVED_GAME_LOG_COLUMNS).issubset(normalized.columns)
    assert normalized.loc[0, "MIN"] == 35
    assert normalized.loc[0, "PRA"] == 47
    assert normalized.loc[0, "FG2M"] == 13
    assert normalized.loc[1, "FG2A"] == 21
    assert list(raw_frame.columns) == _recorded_provider_result_set()["headers"]


def test_fixture_matches_current_player_game_logs_result_set_schema():
    result_set = _recorded_provider_result_set()

    payload = json.loads(FIXTURE_PATH.read_text())

    assert payload["resource"] == "gamelogs"
    assert payload["provenance"]["commit"] == (
        "03f6a064982edfc8c5d5905a6633a3af17569d54"
    )
    assert payload["provenance"]["repository"] == "eddiemay/NBAStats"
    assert result_set["name"] == "PlayerGameLogs"
    assert result_set["headers"] == PlayerGameLogs.expected_data["PlayerGameLogs"]


def test_archetype_normalization_preserves_player_ids_for_cluster_filtering():
    normalized = normalize_archetype_game_logs(_recorded_provider_frame())

    assert "PLAYER_ID" in normalized.columns
    assert list(normalized["PLAYER_ID"]) == [203076] * 6
    assert normalized.loc[0, "PRA"] == 47


def test_missing_required_provider_column_is_centralized_provider_error():
    raw_frame = _recorded_provider_frame().drop(columns=["PTS"])

    with pytest.raises(ProviderUnavailableError, match="unsupported game-log schema"):
        normalize_player_game_logs(raw_frame)


def test_adapter_owns_timeout_and_translates_provider_timeout():
    calls: list[dict] = []

    class TimedOutEndpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            raise requests.exceptions.ReadTimeout("stats.nba.com timed out")

    adapter = NBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=TimedOutEndpoint
    )

    with pytest.raises(ProviderUnavailableError) as error:
        adapter.get_player_game_logs(player_id=2544, season="2025-26")

    assert error.value.code == "provider_unavailable"
    assert calls == [
        {
            "player_id_nullable": 2544,
            "season_nullable": "2025-26",
            "season_type_nullable": "Regular Season",
            "timeout": 2.5,
        }
    ]


def test_adapter_normalizes_recorded_response_from_endpoint_factory():
    class RecordedEndpoint:
        def __init__(self, **kwargs):
            assert kwargs["player_id_nullable"] == 2544
            assert kwargs["season_nullable"] == "2025-26"
            assert kwargs["season_type_nullable"] == "Regular Season"
            assert kwargs["timeout"] == 2.5

        def get_data_frames(self):
            return [_recorded_provider_frame()]

    adapter = NBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=RecordedEndpoint
    )

    normalized = adapter.get_player_game_logs(player_id=2544, season="2025-26")

    assert normalized.loc[0, "PRA"] == 47
    assert "TEAM_NAME" not in normalized.columns


def test_adapter_fetches_and_filters_archetype_logs_through_provider_seam():
    calls: list[dict] = []

    class RecordedEndpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [_recorded_provider_frame()]

    adapter = NBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=RecordedEndpoint
    )

    normalized = adapter.get_archetype_game_logs(
        player_ids=[203076],
        opponent_team_id=1610612744,
        season="2025-26",
    )

    assert list(normalized["PLAYER_ID"]) == [203076] * 6
    assert normalized.loc[0, "PLAYER_NAME"] == "Anthony Davis"
    assert calls == [
        {
            "season_nullable": "2025-26",
            "season_type_nullable": "Regular Season",
            "opp_team_id_nullable": 1610612744,
            "timeout": 2.5,
        }
    ]


def test_game_service_uses_injected_fake_without_provider_patching(tmp_path):
    raw_frame = _recorded_provider_frame()
    normalized_frame = normalize_player_game_logs(raw_frame)

    class FakeNBAStatsAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        def get_player_game_logs(
            self, *, player_id, season, season_type="Regular Season"
        ):
            self.calls.append(
                {
                    "player_id": player_id,
                    "season": season,
                    "season_type": season_type,
                }
            )
            return normalized_frame.copy()

    fake = FakeNBAStatsAdapter()
    engine = create_engine(f"sqlite:///{tmp_path / 'players.sqlite3'}")
    pd.DataFrame([{"full_name": "LeBron James", "id": 2544}]).to_sql(
        "player_information", engine, index=False
    )

    service = GameService(
        engine,
        redis_client=False,
        settings=_test_settings(),
        nba_stats_adapter=fake,
    )

    logs, next_team = asyncio.run(service._get_game_logs("LeBron James", "2025-26"))

    assert next_team is None
    assert list(logs["PRA"]) == [47, 48, 40, 37, 48, 39]
    assert fake.calls == [
        {"player_id": 2544, "season": "2025-26", "season_type": "Regular Season"}
    ]
