"""Offline contract tests for the NBA Stats provider boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest
import requests
from sqlalchemy import create_engine

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import ProviderUnavailableError
from app.providers.nba_stats import (
    DERIVED_GAME_LOG_COLUMNS,
    NBAStatsAdapter,
    REQUIRED_GAME_LOG_COLUMNS,
    normalize_player_game_logs,
)
from app.services.game_service import GameService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nba_stats_player_game_logs.json"


def _recorded_provider_frame() -> pd.DataFrame:
    payload = json.loads(FIXTURE_PATH.read_text())
    return pd.DataFrame(payload["rowSet"], columns=payload["headers"])


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

    assert list(normalized["GAME_DATE"]) == ["2025-10-22", "2025-10-24"]
    assert "GAME_DATE_EST" not in normalized.columns
    assert "MATCH_UP" not in normalized.columns
    assert "PROVIDER_ADDED_COLUMN" not in normalized.columns
    assert set(REQUIRED_GAME_LOG_COLUMNS).issubset(normalized.columns)
    assert set(DERIVED_GAME_LOG_COLUMNS).issubset(normalized.columns)
    assert normalized.loc[0, "MIN"] == 35
    assert normalized.loc[0, "PRA"] == 40
    assert normalized.loc[0, "FG2M"] == 6
    assert normalized.loc[1, "FG2A"] == 14
    assert list(raw_frame.columns) == json.loads(FIXTURE_PATH.read_text())["headers"]


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

    assert normalized.loc[0, "PRA"] == 40
    assert "PROVIDER_ADDED_COLUMN" not in normalized.columns


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
    assert list(logs["PRA"]) == [40, 45]
    assert fake.calls == [
        {"player_id": 2544, "season": "2025-26", "season_type": "Regular Season"}
    ]
