"""Offline tests for the legacy dataframe filter helpers."""

from __future__ import annotations

import pandas as pd

from app.utils import filters


class FakeNBAStatsProvider:
    def __init__(self, logs_by_player_id: dict[int, pd.DataFrame]):
        self.logs_by_player_id = logs_by_player_id
        self.calls: list[dict] = []

    def get_player_game_logs(
        self, *, player_id: int, season: str, season_type: str = "Regular Season"
    ) -> pd.DataFrame:
        self.calls.append(
            {
                "player_id": player_id,
                "season": season,
                "season_type": season_type,
            }
        )
        return self.logs_by_player_id[player_id].copy()


def test_get_games_to_exclude_uses_injected_provider(monkeypatch):
    monkeypatch.setattr(
        filters,
        "get_player_id",
        lambda player_name: {"Bench Player": 42, "Second Bench": 43}[player_name],
    )
    provider = FakeNBAStatsProvider(
        {
            42: pd.DataFrame({"GAME_ID": ["g1", "g2"]}),
            43: pd.DataFrame({"GAME_ID": ["g2", "g3"]}),
        }
    )

    excluded = filters.get_games_to_exclude(
        pd.DataFrame(),
        ["Bench Player", "Second Bench"],
        season="2025-26",
        nba_stats_provider=provider,
    )

    assert excluded == {"g1", "g2", "g3"}
    assert provider.calls == [
        {"player_id": 42, "season": "2025-26", "season_type": "Regular Season"},
        {"player_id": 43, "season": "2025-26", "season_type": "Regular Season"},
    ]


def test_get_common_games_uses_injected_provider(monkeypatch):
    monkeypatch.setattr(
        filters,
        "get_player_id",
        lambda player_name: {"Teammate": 44}[player_name],
    )
    provider = FakeNBAStatsProvider(
        {
            44: pd.DataFrame(
                {
                    "GAME_ID": ["g1", "g3"],
                    "TEAM_ABBREVIATION": ["LAL", "LAL"],
                }
            )
        }
    )

    common = filters.get_common_games(
        pd.DataFrame(
            {
                "GAME_ID": ["g1", "g2"],
                "TEAM_ABBREVIATION": ["LAL", "LAL"],
            }
        ),
        ["Teammate"],
        season="2025-26",
        nba_stats_provider=provider,
    )

    assert common == {"g1"}
    assert provider.calls == [
        {"player_id": 44, "season": "2025-26", "season_type": "Regular Season"}
    ]
