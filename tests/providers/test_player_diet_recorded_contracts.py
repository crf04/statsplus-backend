"""Pinned offline provider-contract fixtures for Season player Diet collection."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.nba_stats_adapter import parse_recorded_player_diet
from app.services.pbp_stats_adapter import PBPTotalsAdapter


FIXTURES = Path(__file__).parents[1] / "fixtures" / "player_diets"


def _payload(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_recorded_synergy_fixture_pins_canonical_player_possession_fields():
    frame = parse_recorded_player_diet(
        _payload("synergy_isolation.json"), kind="synergy"
    )

    assert frame.loc[0, [
        "PLAYER_ID", "PLAY_TYPE", "TYPE_GROUPING", "GP", "POSS_PCT", "POSS"
    ]].to_dict() == {
        "PLAYER_ID": 1628983,
        "PLAY_TYPE": "Isolation",
        "TYPE_GROUPING": "Offensive",
        "GP": 63,
        "POSS_PCT": 0.28,
        "POSS": 449,
    }


def test_recorded_player_shot_type_fixture_pins_frequency_volume_and_games():
    frame = parse_recorded_player_diet(
        _payload("shot_type_catch_and_shoot.json"), kind="shot_type"
    )

    assert frame.loc[0, ["PLAYER_ID", "GP", "FGA_FREQUENCY", "FGA"]].to_dict() == {
        "PLAYER_ID": 1642851.0,
        "GP": 81.0,
        "FGA_FREQUENCY": 0.475,
        "FGA": 515.0,
    }


def test_recorded_player_shot_zone_fixture_pins_multilevel_identity_and_fga():
    frame = parse_recorded_player_diet(
        _payload("shot_zones.json"), kind="shot_zones"
    )

    assert frame.loc[0, ("", "PLAYER_ID")] == 1630639
    assert frame.loc[0, ("Restricted Area", "FGA")] == 27
    assert frame.loc[0, ("Corner 3", "FGA")] == 32


def test_recorded_pbp_fixture_pins_canonical_entity_games_and_assist_locations():
    frame = PBPTotalsAdapter.parse_player_diet_totals(
        _payload("pbp_player_totals.json")
    )

    assert frame.loc[0].to_dict() == {
        "EntityId": "1641708",
        "Name": "Amen Thompson",
        "GamesPlayed": 79,
        "Assists": 420,
        "Arc3Assists": 126,
        "Corner3Assists": 74,
        "AtRimAssists": 117,
        "ShortMidRangeAssists": 73,
        "LongMidRangeAssists": 30,
    }
