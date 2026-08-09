"""Offline contract tests for player-profile provider integration."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.services.player_service import PlayerService


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        cache={"enabled": False},
        providers={"nba_stats_timeout_seconds": 2.5},
        nba=NBASeasonSettings(current_season="2025-26"),
    )


def test_archetype_profile_uses_injected_nba_stats_provider(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'players.sqlite3'}")
    pd.DataFrame([{"full_name": "LeBron James", "id": 2544}]).to_sql(
        "player_information", engine, index=False
    )
    pd.DataFrame(
        [
            {"PlayerName": "LeBron James", "ClusterID": 7, "PlayerID": 2544},
            {"PlayerName": "Stephen Curry", "ClusterID": 7, "PlayerID": 201939},
        ]
    ).to_sql("player_clusters", engine, index=False)
    pd.DataFrame(
        [
            {
                "PLAYER_ID": 2544,
                "FGM": 6.0,
                "FGA": 12.0,
                "FG3M": 2.0,
                "FG3A": 5.0,
                "FTM": 4.0,
                "FTA": 5.0,
                "PTS": 18.0,
                "TOV": 2.0,
            }
        ]
    ).to_sql("player_per36_stats", engine, index=False)

    class FakeNBAStatsProvider:
        def __init__(self):
            self.calls: list[dict] = []

        def get_archetype_game_logs(
            self,
            *,
            player_ids,
            opponent_team_id,
            season,
            season_type="Regular Season",
        ):
            self.calls.append(
                {
                    "player_ids": list(player_ids),
                    "opponent_team_id": opponent_team_id,
                    "season": season,
                    "season_type": season_type,
                }
            )
            return pd.DataFrame(
                [
                    {
                        "PLAYER_ID": 2544,
                        "PLAYER_NAME": "LeBron James",
                        "GAME_DATE": "2025-10-22",
                        "MIN": 30.0,
                        "FGM": 5.0,
                        "FGA": 10.0,
                        "FG3M": 2.0,
                        "FG3A": 4.0,
                        "FTM": 3.0,
                        "FTA": 4.0,
                        "PTS": 17.0,
                        "TOV": 1.0,
                    }
                ]
            )

    provider = FakeNBAStatsProvider()
    monkeypatch.setattr(
        PlayerService,
        "_get_teams",
        staticmethod(
            lambda: [{"full_name": "Boston Celtics", "id": 1610612738}]
        ),
    )
    service = PlayerService(engine, settings=_settings(), nba_stats_provider=provider)

    result = service.get_player_profile(
        "LeBron James", "Archetype", "Boston Celtics"
    )

    assert result[0]["PLAYER_NAME"] == "LeBron James"
    assert result[0]["FGM/36MIN"] == 6.0
    assert provider.calls == [
        {
            "player_ids": [2544, 201939],
            "opponent_team_id": 1610612738,
            "season": "2025-26",
            "season_type": "Regular Season",
        }
    ]
