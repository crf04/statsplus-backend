"""Offline contract tests for player-profile provider integration."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.domain.nba_events import REGULAR_SEASON_TYPE
from app.services.player_diet import (
    PlayerDietBaseline,
    PlayerDietResult,
    ShotTypeShooting,
    StoredPlayerDietFact,
)
from app.services.player_game_log_repository import PlayerGameLogRecord
from app.services.player_service import PlayerProfileReader, PlayerService


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        cache={"enabled": False},
        providers={"nba_stats_timeout_seconds": 2.5},
        nba=NBASeasonSettings(current_season="2025-26"),
    )


def _archetype_engine(tmp_path):
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
    return engine


def _game_log_record(**overrides):
    values = {
        "season": "2025-26",
        "season_type": REGULAR_SEASON_TYPE,
        "player_id": 2544,
        "game_id": "0022500001",
        "player_name": "LeBron James",
        "game_date": date(2025, 10, 22),
        "team_id": 1610612747,
        "team_tricode": "LAL",
        "opponent_team_id": 1610612738,
        "opponent_team_tricode": "BOS",
        "is_home": False,
        "minutes": 30.0,
        "points": 17,
        "rebounds": 8,
        "assists": 7,
        "field_goals_made": 5,
        "field_goals_attempted": 10,
        "three_pointers_made": 2,
        "three_pointers_attempted": 4,
        "free_throws_made": 3,
        "free_throws_attempted": 4,
        "turnovers": 1,
    }
    values.update(overrides)
    return PlayerGameLogRecord(**values)


class _StoredArchetypeLogs:
    """Read-only test seam for the governed player game-log publication."""

    def __init__(self, records):
        self.records = tuple(records)
        self.calls: list[dict] = []
        self.snapshots: list[str] = []

    def read_publication_snapshot(self, season):
        self.snapshots.append(season)
        return f"snapshot:{season}"

    def list_archetype_rows(
        self, season, player_ids, opponent_team_id, *, publication_snapshot=None
    ):
        self.calls.append(
            {
                "season": season,
                "player_ids": list(player_ids),
                "opponent_team_id": opponent_team_id,
                "publication_snapshot": publication_snapshot,
            }
        )
        return self.records


def test_archetype_profile_reads_the_stored_cluster_rows(tmp_path, monkeypatch):
    engine = _archetype_engine(tmp_path)
    logs = _StoredArchetypeLogs([_game_log_record()])
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
        game_logs=logs,
    )

    result = service.get_player_profile(
        "LeBron James", "Archetype", "Boston Celtics"
    )

    assert result[0]["PLAYER_NAME"] == "LeBron James"
    assert result[0]["GAME_DATE"] == "2025-10-22"
    assert result[0]["MIN"] == 30
    assert result[0]["FGM/36MIN"] == 6.0
    assert result[0]["FGM/36MIN_DIFF"] == pytest.approx(0.0)
    # The selected player stays in the comparison alongside the cluster, and
    # the read is scoped by the governed publication snapshot.
    assert logs.calls == [
        {
            "season": "2025-26",
            "player_ids": [2544, 201939],
            "opponent_team_id": 1610612738,
            "publication_snapshot": "snapshot:2025-26",
        }
    ]
    assert not hasattr(service, "nba_stats")


def test_archetype_profile_keeps_only_regular_season_rows(tmp_path, monkeypatch):
    engine = _archetype_engine(tmp_path)
    logs = _StoredArchetypeLogs(
        [
            _game_log_record(),
            _game_log_record(
                game_id="0042500001",
                season_type="Playoffs",
                game_date=date(2026, 4, 20),
                field_goals_made=9,
            ),
        ]
    )
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
        game_logs=logs,
    )

    result = service.get_player_profile(
        "LeBron James", "Archetype", "Boston Celtics"
    )

    assert [row["GAME_DATE"] for row in result] == ["2025-10-22"]


def test_archetype_profile_omits_a_row_without_usable_season_baselines(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'players.sqlite3'}")
    pd.DataFrame([{"full_name": "LeBron James", "id": 2544}]).to_sql(
        "player_information", engine, index=False
    )
    pd.DataFrame(
        [{"PlayerName": "LeBron James", "ClusterID": 7, "PlayerID": 2544}]
    ).to_sql("player_clusters", engine, index=False)
    pd.DataFrame(
        [
            {
                "PLAYER_ID": 2544,
                "FGM": 6.0,
                "FGA": 12.0,
                "FG3M": 0.0,
                "FG3A": 0.0,
                "FTM": 4.0,
                "FTA": 5.0,
                "PTS": 18.0,
                "TOV": 2.0,
            }
        ]
    ).to_sql("player_per36_stats", engine, index=False)
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
        game_logs=_StoredArchetypeLogs([_game_log_record()]),
    )

    result = service.get_player_profile(
        "LeBron James", "Archetype", "Boston Celtics"
    )

    # Dividing by a zero season rate has no answer to report, and the client
    # renders every returned cell as a number, so the row is omitted rather
    # than published with a value that would read as "no change".
    assert result == []


def test_archetype_profile_keeps_cluster_rows_that_do_have_baselines(
    tmp_path, monkeypatch
):
    # Omission is per row, not per request: one cluster member's unusable
    # baseline must not discard the members whose comparison is well defined.
    engine = _archetype_engine(tmp_path)
    pd.DataFrame(
        [
            {
                "PLAYER_ID": 201939,
                "FGM": 6.0,
                "FGA": 12.0,
                "FG3M": 0.0,
                "FG3A": 5.0,
                "FTM": 4.0,
                "FTA": 5.0,
                "PTS": 18.0,
                "TOV": 2.0,
            }
        ]
    ).to_sql("player_per36_stats", engine, index=False, if_exists="append")
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
        game_logs=_StoredArchetypeLogs([
            _game_log_record(),
            _game_log_record(
                player_id=201939,
                player_name="Stephen Curry",
                game_id="0022500002",
            ),
        ]),
    )

    result = service.get_player_profile(
        "LeBron James", "Archetype", "Boston Celtics"
    )

    assert [row["PLAYER_NAME"] for row in result] == ["LeBron James"]
    assert all(
        isinstance(row[column], float)
        for row in result
        for column in row
        if column.endswith("_DIFF")
    )


def test_archetype_profile_is_empty_without_a_stored_game_log_reader(
    tmp_path, monkeypatch
):
    engine = _archetype_engine(tmp_path)
    monkeypatch.setattr(
        PlayerService,
        "_get_teams",
        staticmethod(
            lambda: [{"full_name": "Boston Celtics", "id": 1610612738}]
        ),
    )
    service = PlayerService(
        engine, PlayerProfileReader.unavailable(), settings=_settings()
    )

    assert service.get_player_profile(
        "LeBron James", "Archetype", "Boston Celtics"
    ) == []


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

    service = PlayerService(
        object(),
        settings=_settings(),
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


def _shot_type_fact(slice_key, *, shooting, share=0.475, volume=515.0, games=81):
    return StoredPlayerDietFact(
        player_id=111,
        base="shot_types",
        slice_key=slice_key,
        share=share,
        volume=volume,
        games_played=games,
        volume_unit="field_goal_attempts",
        provider="nba_stats",
        shooting=shooting,
        retrieved_at=pd.Timestamp("2026-08-23", tz="UTC").to_pydatetime(),
    )


def _catch_and_shoot_shooting():
    """The recorded ``LeagueDashPTShots`` row, as stored Totals."""

    return ShotTypeShooting(
        makes=222.0,
        two_point_makes=13.0,
        two_point_attempts=22.0,
        two_point_share=0.02,
        three_point_makes=209.0,
        three_point_attempts=493.0,
        three_point_share=0.455,
    )


def _shot_type_reader(facts):
    return _DurableProfileReader(
        [_catalog_row(111, "Kon Knueppel", "CHA")],
        PlayerDietResult(
            season="2025-26",
            players={111: tuple(facts)},
            observations=(),
            baselines={},
        ),
    )


def test_shooting_type_profile_renders_stored_facts_as_per_game_values():
    reader = _shot_type_reader(
        [_shot_type_fact("Catch and Shoot", shooting=_catch_and_shoot_shooting())]
    )
    service = PlayerService(object(), reader, settings=_settings())

    rows = service.get_player_profile("Kon Knueppel", "Shooting Type")

    assert len(rows) == 1
    row = rows[0]
    assert row["SHOT_TYPE"] == "C&S"
    # Frequencies and percentages stay fractions; counts are Totals divided
    # by the fact's own games played.
    assert row["FGA_FREQUENCY"] == pytest.approx(0.475)
    assert row["FGA"] == pytest.approx(round(515 / 81, 1))
    assert row["FGM"] == pytest.approx(round(222 / 81, 1))
    assert row["FG_PCT"] == pytest.approx(0.431, abs=0.001)
    assert row["FG2A_FREQUENCY"] == pytest.approx(0.02)
    assert row["FG2M"] == pytest.approx(round(13 / 81, 1))
    assert row["FG2A"] == pytest.approx(round(22 / 81, 1))
    assert row["FG2_PCT"] == pytest.approx(0.591, abs=0.001)
    assert row["FG3A_FREQUENCY"] == pytest.approx(0.455)
    assert row["FG3M"] == pytest.approx(round(209 / 81, 1))
    assert row["FG3A"] == pytest.approx(round(493 / 81, 1))
    assert row["FG3_PCT"] == pytest.approx(0.424, abs=0.001)
    assert reader.diet_calls == [("2025-26", (111,))]


def test_shooting_type_profile_labels_every_stored_slice_vocabulary():
    reader = _shot_type_reader(
        [
            _shot_type_fact(
                "catch_and_shoot", shooting=_catch_and_shoot_shooting()
            ),
            _shot_type_fact(
                "pullups", shooting=_catch_and_shoot_shooting()
            ),
            _shot_type_fact(
                "Less Than 10 ft", shooting=_catch_and_shoot_shooting()
            ),
        ]
    )
    service = PlayerService(object(), reader, settings=_settings())

    rows = service.get_player_profile("Kon Knueppel", "Shooting Type")

    # Published slices arrive under stored keys and legacy table rows under
    # display keys; both read as the labels the tab has always shown.
    assert [row["SHOT_TYPE"] for row in rows] == ["C&S", "Pullup", "<10 Ft"]


def test_shooting_type_profile_omits_a_slice_without_a_stored_split():
    reader = _shot_type_reader(
        [
            _shot_type_fact("Catch and Shoot", shooting=None),
            _shot_type_fact("Pullups", shooting=_catch_and_shoot_shooting()),
        ]
    )
    service = PlayerService(object(), reader, settings=_settings())

    rows = service.get_player_profile("Kon Knueppel", "Shooting Type")

    # A share and a volume cannot say how the attempts divided into twos and
    # threes, so the slice is unavailable rather than invented.
    assert [row["SHOT_TYPE"] for row in rows] == ["Pullup"]


def test_shooting_type_profile_is_empty_without_stored_shot_type_facts():
    reader = _shot_type_reader([])
    service = PlayerService(object(), reader, settings=_settings())

    assert service.get_player_profile("Kon Knueppel", "Shooting Type") == []


def test_player_service_requires_an_explicit_profile_reader():
    with pytest.raises(TypeError, match="profile_reader"):
        PlayerService(object(), settings=_settings())


@pytest.mark.parametrize(
    ("category", "method_name", "expected"),
    (
        ("Archetype", "_get_archetype_gamelogs", [{"PLAYER_NAME": "Legacy Name"}]),
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

    service = PlayerService(
        RefusingEngine(),
        settings=_settings(),
        profile_reader=reader,
    )
    # The service owns no provider client at all, so no profile category can
    # reach one however it is dispatched.
    assert not hasattr(service, "nba_stats")

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


def test_sparse_assist_facts_preserve_keys_as_explicitly_unavailable():
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

    assist_keys = {
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }
    assert set(profile) == {
        "Name",
        *assist_keys,
        *(f"{key}+" for key in assist_keys),
    }
    assert profile["AtRimAssists"] == pytest.approx(30.0)
    assert profile["AtRimAssists+"] == pytest.approx(1.5)
    assert profile["ShortMidRangeAssists"] is None
    assert profile["ShortMidRangeAssists+"] is None
    assert profile["TwoPtAssists"] is None
    assert profile["TwoPtAssists+"] is None
    assert profile["ThreePtAssists"] is None
    assert profile["ThreePtAssists+"] is None


def test_assist_plus_values_require_complete_league_baselines():
    player_id = 111
    facts = tuple(
        _fact(player_id, "assist_locations", slice_key, share)
        for slice_key, share in {
            "Arc3Assists": 0.20,
            "Corner3Assists": 0.10,
            "AtRimAssists": 0.30,
            "ShortMidRangeAssists": 0.15,
            "LongMidRangeAssists": 0.05,
        }.items()
    )
    reader = _DurableProfileReader(
        [_catalog_row(player_id, "Jayson Tatum")],
        PlayerDietResult(
            season="2025-26",
            players={player_id: facts},
            observations=(),
            baselines={
                ("assist_locations", "Arc3Assists"): PlayerDietBaseline(
                    0.1, 0.1
                ),
                ("assist_locations", "Corner3Assists"): PlayerDietBaseline(
                    0.1, 0.1
                ),
                ("assist_locations", "AtRimAssists"): PlayerDietBaseline(
                    0.2, 0.1
                ),
                ("assist_locations", "ShortMidRangeAssists"): PlayerDietBaseline(
                    None, None
                ),
            },
        ),
    )
    service = PlayerService(object(), reader, settings=_settings())

    profile = service.get_player_profile("Jayson Tatum", "assists")[0]

    assert profile["TwoPtAssists"] == pytest.approx(50.0)
    assert profile["TwoPtAssists+"] is None
    assert profile["ThreePtAssists+"] == pytest.approx(1.5)
    assert profile["ShortMidRangeAssists+"] is None
    assert profile["LongMidRangeAssists+"] is None


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
