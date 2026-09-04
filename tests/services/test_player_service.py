"""Offline contract tests for player-profile provider integration."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.services.player_diet import (
    PlayerDietBaseline,
    PlayerDietResult,
    StoredPlayerDietFact,
)
from app.services.player_service import PlayerProfileReader, PlayerService


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
    service = PlayerService(
        engine,
        PlayerProfileReader.unavailable(),
        settings=_settings(),
        nba_stats_provider=provider,
    )

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


class _DurableProfileReader:
    """Read-only test seam for the season catalog and Player Diet facts."""

    def __init__(self, catalog, result):
        self.catalog = catalog
        self.result = result
        self.catalog_calls = []
        self.diet_calls = []

    def get_catalog(self, season, *, active_only=False):
        self.catalog_calls.append((season, active_only))
        return list(self.catalog)

    def get_for_players(self, season, player_ids):
        self.diet_calls.append((season, tuple(player_ids)))
        return self.result


def _catalog_row(player_id, display_name, team="BOS", *, active=True):
    return {
        "season": "2025-26",
        "player_id": player_id,
        "display_name": display_name,
        "roster_status": "active" if active else "inactive",
        "is_active": active,
        "is_active_for_season": active,
        "team_id": 1610612738,
        "team_name": "Boston Celtics",
        "team_abbreviation": team,
    }


def _fact(player_id, base, slice_key, share, *, volume=100.0):
    return StoredPlayerDietFact(
        player_id=player_id,
        base=base,
        slice_key=slice_key,
        share=share,
        volume=volume,
        games_played=20,
        volume_unit="possessions" if base == "play_types" else "assists",
        provider="nba_synergy" if base == "play_types" else "pbp_stats",
        retrieved_at=pd.Timestamp("2026-08-23", tz="UTC").to_pydatetime(),
    )


def _durable_profile_reader():
    player_id = 111
    facts = [
        _fact(player_id, "play_types", "Transition", 0.2),
        _fact(player_id, "play_types", "Isolation", 0.05),
        _fact(player_id, "assist_locations", "Arc3Assists", 0.20),
        _fact(player_id, "assist_locations", "Corner3Assists", 0.10),
        _fact(player_id, "assist_locations", "AtRimAssists", 0.30),
        _fact(player_id, "assist_locations", "ShortMidRangeAssists", 0.15),
        _fact(player_id, "assist_locations", "LongMidRangeAssists", 0.05),
    ]
    baselines = {
        ("assist_locations", key): PlayerDietBaseline(share, 0.1)
        for key, share in {
            "Arc3Assists": 0.10,
            "Corner3Assists": 0.10,
            "AtRimAssists": 0.20,
            "ShortMidRangeAssists": 0.10,
            "LongMidRangeAssists": 0.05,
        }.items()
    }
    return _DurableProfileReader(
        [_catalog_row(player_id, "Jayson Tatum", "BOS")],
        PlayerDietResult(
            season="2025-26",
            players={player_id: tuple(facts)},
            observations=(),
            baselines=baselines,
        ),
    )


def test_profiles_and_player_list_use_durable_catalog_and_diet_facts(monkeypatch):
    reader = _durable_profile_reader()

    class RefusingProvider:
        def __getattr__(self, name):
            raise AssertionError(f"provider call reached profile read: {name}")

    service = PlayerService(
        object(),
        settings=_settings(),
        nba_stats_provider=RefusingProvider(),
        profile_reader=reader,
    )

    assert service.get_all_players() == ["Jayson Tatum"]
    playtypes = service.get_player_profile("JAYSON-TATUM", "Playtypes")
    assert playtypes["PLAYER_NAME"] == "Jayson Tatum"
    assert playtypes["TEAM_ABBREVIATION"] == "BOS"
    assert playtypes["Transition%"] == pytest.approx(20.0)
    assert playtypes["Isolation%"] == pytest.approx(5.0)
    assert playtypes["Postup%"] == 0

    assists = service.get_player_profile("Jayson Tatum", "assists")
    assert len(assists) == 1
    assert assists[0]["Name"] == "Jayson Tatum"
    assert assists[0]["ThreePtAssists"] == pytest.approx(30.0)
    assert assists[0]["TwoPtAssists"] == pytest.approx(50.0)
    assert assists[0]["TwoPtAssists+"] == pytest.approx(50.0 / 35.0)
    assert assists[0]["ThreePtAssists+"] == pytest.approx(30.0 / 20.0)

    assert reader.catalog_calls
    assert reader.diet_calls


def test_player_service_requires_an_explicit_profile_reader():
    with pytest.raises(TypeError, match="profile_reader"):
        PlayerService(object(), settings=_settings())


@pytest.mark.parametrize(
    ("category", "method_name", "expected"),
    (
        ("Archetype", "_get_archetype_gamelogs", [{"PLAYER_NAME": "Legacy Name"}]),
        ("Shooting Type", "_get_shooting_type", [{"SHOT_TYPE": "C&S"}]),
        ("Zone Shooting", "_get_player_zone_shooting", {"Restricted Area": 0.7}),
    ),
)
def test_legacy_profile_categories_keep_their_historical_name_lookup(
    monkeypatch, category, method_name, expected
):
    class RefusingProfileReader:
        def get_catalog(self, season, *, active_only=False):
            raise AssertionError("legacy profile category consulted Athlete Catalog")

        def get_for_players(self, season, player_ids):
            raise AssertionError("legacy profile category consulted Player Diet")

    service = PlayerService(
        object(),
        RefusingProfileReader(),
        settings=_settings(),
    )

    def fuzzy_match(player_name):
        return "Legacy Name"

    def handler(*args):
        return expected

    monkeypatch.setattr(service, "_fuzzy_match_player_name", fuzzy_match)
    monkeypatch.setattr(service, method_name, handler)

    assert service.get_player_profile(
        "catalog spelling", category, "Boston Celtics"
    ) == expected


def test_profile_read_does_not_touch_legacy_tables_or_provider(monkeypatch):
    reader = _durable_profile_reader()

    class RefusingEngine:
        def connect(self):
            raise AssertionError("legacy profile table read reached the service")

    class RefusingProvider:
        def __getattr__(self, name):
            raise AssertionError(f"provider call reached profile read: {name}")

    service = PlayerService(
        RefusingEngine(),
        settings=_settings(),
        nba_stats_provider=RefusingProvider(),
        profile_reader=reader,
    )

    assert service.get_all_players() == ["Jayson Tatum"]
    assert service.get_player_profile("Jayson Tatum", "Playtypes")["Transition%"] == 20.0
    assert service.get_player_profile("Jayson Tatum", "assists")[0]["Name"] == "Jayson Tatum"


def test_player_list_includes_the_complete_current_season_fact_population():
    player_count = 439
    catalog = [
        _catalog_row(
            player_id,
            f"Player {player_id}",
            active=player_id != player_count,
        )
        for player_id in range(1, player_count + 1)
    ]
    result = PlayerDietResult(
        season="2025-26",
        players={
            player_id: (_fact(player_id, "play_types", "Isolation", 0.1),)
            for player_id in range(1, player_count + 1)
        },
        observations=(),
    )
    reader = _DurableProfileReader(catalog, result)
    service = PlayerService(object(), settings=_settings(), profile_reader=reader)

    players = service.get_all_players()

    assert len(players) == player_count
    assert players[-1] == "Player 439"
    assert reader.catalog_calls == [("2025-26", False)]


def test_sparse_assist_facts_remain_absent_from_profile_and_derived_totals():
    player_id = 111
    reader = _DurableProfileReader(
        [_catalog_row(player_id, "Jayson Tatum")],
        PlayerDietResult(
            season="2025-26",
            players={
                player_id: (
                    _fact(
                        player_id,
                        "assist_locations",
                        "AtRimAssists",
                        0.3,
                    ),
                )
            },
            observations=(),
            baselines={
                ("assist_locations", "AtRimAssists"): PlayerDietBaseline(
                    0.2, 0.1
                )
            },
        ),
    )
    service = PlayerService(object(), reader, settings=_settings())

    profile = service.get_player_profile("Jayson Tatum", "assists")[0]

    assert profile["AtRimAssists"] == pytest.approx(30.0)
    assert profile["AtRimAssists+"] == pytest.approx(1.5)
    assert "ShortMidRangeAssists" not in profile
    assert "ShortMidRangeAssists+" not in profile
    assert "TwoPtAssists" not in profile
    assert "TwoPtAssists+" not in profile
    assert "ThreePtAssists" not in profile
    assert "ThreePtAssists+" not in profile


def test_player_list_does_not_hide_durable_reader_failures():
    class FailingReader:
        def get_catalog(self, season, *, active_only=False):
            raise RuntimeError("player catalog schema is unavailable")

        def get_for_players(self, season, player_ids):
            raise AssertionError("facts must not be read after catalog failure")

    service = PlayerService(
        object(),
        settings=_settings(),
        profile_reader=FailingReader(),
    )

    with pytest.raises(RuntimeError, match="catalog schema is unavailable"):
        service.get_all_players()


def test_traded_player_profile_uses_combined_fact_and_current_catalog_team():
    player_id = 201935
    combined_share = 0.402376754416591
    reader = _DurableProfileReader(
        [_catalog_row(player_id, "James Harden", "LAC")],
        PlayerDietResult(
            season="2025-26",
            players={
                player_id: (
                    _fact(
                        player_id,
                        "play_types",
                        "Isolation",
                        combined_share,
                        volume=600,
                    ),
                )
            },
            observations=(),
        ),
    )
    service = PlayerService(object(), settings=_settings(), profile_reader=reader)

    profile = service.get_player_profile("James Harden", "Playtypes")

    assert profile["TEAM_ABBREVIATION"] == "LAC"
    assert profile["Isolation%"] == pytest.approx(40.2376754416591)


@pytest.mark.parametrize(
    ("canonical_name", "ascii_name"),
    (
        ("Dennis Schr\u00f6der", "Dennis Schroder"),
        ("Nikola Vu\u010devi\u0107", "Nikola Vucevic"),
        ("Kristaps Porzi\u0146\u0123is", "Kristaps Porzingis"),
        ("Luka Don\u010di\u0107", "Luka Doncic"),
    ),
)
def test_accent_and_ascii_names_resolve_to_the_same_durable_profile(
    canonical_name, ascii_name
):
    player_id = 77
    facts = (
        _fact(player_id, "play_types", "Isolation", 0.42),
        _fact(player_id, "assist_locations", "AtRimAssists", 1.0),
    )
    reader = _DurableProfileReader(
        [_catalog_row(player_id, canonical_name, "DAL")],
        PlayerDietResult(
            season="2025-26",
            players={player_id: facts},
            observations=(),
            baselines={
                ("assist_locations", "AtRimAssists"): PlayerDietBaseline(
                    0.5, 0.1
                )
            },
        ),
    )
    service = PlayerService(object(), settings=_settings(), profile_reader=reader)

    accented = service.get_player_profile(canonical_name, "Playtypes")
    ascii_profile = service.get_player_profile(ascii_name, "Playtypes")

    assert accented == ascii_profile
    assert accented["PLAYER_NAME"] == canonical_name
    assert accented["TEAM_ABBREVIATION"] == "DAL"
    assert accented["Isolation%"] == pytest.approx(42.0)
    assert service.get_player_profile(ascii_name, "assists")[0][
        "AtRimAssists+"
    ] == pytest.approx(2.0)
