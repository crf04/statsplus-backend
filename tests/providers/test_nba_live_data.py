import json
from pathlib import Path

import pytest

from app.providers.nba_live_data import (
    NBA_LIVE_DATA_BOXSCORE_URL,
    NBALiveDataBoxscoreAdapter,
)
from app.providers.nba_live_data import compose_pbp_live_data_observation
from app.services.canonical_game_ledger import canonical_game_from_pbp
from app.utils import telemetry
from app.utils.telemetry import ProviderResponseError


def _pbp_payload():
    return json.loads(Path("tests/fixtures/pbp_stats/game_stats.valid.json").read_text())


def _live_payload():
    return {
        "meta": {"version": 1, "code": 200},
        "game": {
            "gameId": "0022400001",
            "gameStatus": 3,
            "gameStatusText": "Final",
            "gameTimeUTC": "2024-11-15T00:00:00Z",
            "homeTeam": _team(
                1610612747,
                "LAL",
                [
                    _player(2544, "LeBron James", 30, points=20, blocks=1),
                    _player(203507, "Player Two", 20, points=10, blocks=2),
                ],
            ),
            "awayTeam": _team(
                1610612759,
                "SAS",
                [_player(201935, "Player Three", 25, points=15, steals=2)],
            ),
        },
    }


def _player(person_id, name, minutes, *, points, blocks=0, steals=0):
    return {
        "personId": person_id,
        "name": name,
        "firstName": name.split()[0],
        "familyName": name.split()[-1],
        "played": "1",
        "statistics": {
            "minutes": f"PT{minutes}M00.00S",
            "fieldGoalsMade": points // 2,
            "fieldGoalsAttempted": points // 2,
            "threePointersMade": 0,
            "threePointersAttempted": 0,
            "freeThrowsMade": 0,
            "freeThrowsAttempted": 0,
            "reboundsOffensive": 0,
            "reboundsDefensive": 0,
            "reboundsTotal": 0,
            "assists": 0,
            "steals": steals,
            "blocks": blocks,
            "turnovers": 0,
            "foulsPersonal": 0,
            "points": points,
            "twoPointersMade": points // 2,
            "twoPointersAttempted": points // 2,
        },
    }


def _team(team_id, tricode, players):
    sums = {
        key: sum(player["statistics"][key] for player in players)
        for key in (
            "fieldGoalsMade", "fieldGoalsAttempted", "threePointersMade",
            "threePointersAttempted", "freeThrowsMade", "freeThrowsAttempted",
            "reboundsOffensive", "reboundsDefensive", "reboundsTotal", "assists",
            "steals", "blocks", "turnovers", "foulsPersonal", "points",
            "twoPointersMade", "twoPointersAttempted",
        )
    }
    return {
        "teamId": team_id,
        "teamTricode": tricode,
        "teamName": tricode,
        "players": players,
        "statistics": sums,
    }


def _event():
    return {
        "nba_game_id": "0022400001",
        "season": "2024-25",
        "classification": "Regular Season",
        "scheduled_at": "2024-11-15T00:00:00Z",
        "home_team_id": 1610612747,
        "home_team_tricode": "LAL",
        "away_team_id": 1610612759,
        "away_team_tricode": "SAS",
        "status_code": 3,
        "status_text": "Final",
    }


def test_composite_uses_live_traditional_evidence_and_retains_pbp_advanced_fields():
    pbp = _pbp_payload()
    del pbp["team_results"]["Home"]["FullGame"]["Blocks"]
    pbp["stats"]["Home"]["FullGame"][1]["Arc3Assists"] = 7

    observation = compose_pbp_live_data_observation(pbp, _live_payload(), event=_event())
    game = canonical_game_from_pbp(observation, event=_event())

    assert game.team_facts[0].blocks == 3
    assert next(player for player in game.player_facts if player.player_id == 2544).arc3_assists == 7
    assert observation["_ledger_provenance"]["provider"] == "pbp+nba_live_data"
    assert observation["_ledger_provenance"]["source_documents"]["pbp"] == pbp
    assert observation["_ledger_provenance"]["source_documents"]["nba_live_data"]["game"]["gameId"] == "0022400001"


def test_composite_uses_governed_live_home_away_and_complete_team_rows():
    pbp = _pbp_payload()
    pbp["home_team_id"], pbp["away_team_id"] = pbp["away_team_id"], pbp["home_team_id"]
    pbp["home_team_abbreviation"], pbp["away_team_abbreviation"] = (
        pbp["away_team_abbreviation"],
        pbp["home_team_abbreviation"],
    )
    pbp["stats"]["Home"]["FullGame"] = pbp["stats"]["Home"]["FullGame"][1:]

    observation = compose_pbp_live_data_observation(pbp, _live_payload(), event=_event())
    game = canonical_game_from_pbp(observation, event=_event())

    assert game.home_team_id == 1610612747
    assert game.away_team_id == 1610612759
    assert {row.team_id for row in game.raw_rows if row.row_type == "team"} == {
        1610612747,
        1610612759,
    }


def test_live_only_fallback_is_accepted_without_claiming_pbp_provenance():
    observation = compose_pbp_live_data_observation(
        None, _live_payload(), event=_event()
    )

    game = canonical_game_from_pbp(observation, event=_event())

    assert game.game_id == "0022400001"
    assert observation["_ledger_provenance"]["provider"] == "nba_live_data"
    assert observation["_ledger_provenance"]["source_documents"]["pbp"] is None


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        return _Response(self.payload)


def test_adapter_fetches_one_final_identity_and_records_telemetry():
    telemetry.clear_recorded_provider_events()
    session = _Session(_live_payload())
    adapter = NBALiveDataBoxscoreAdapter(session=session)

    assert adapter.fetch_game_stats("0022400001", "2024-25") == _live_payload()

    assert session.calls[0][0] == NBA_LIVE_DATA_BOXSCORE_URL.format(
        game_id="0022400001"
    )
    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == telemetry.PROVIDER_NBA_LIVE_DATA
    assert event["operation"] == "game_boxscore"
    assert event["outcome"] == "success"


def test_adapter_rejects_a_nonfinal_or_foreign_game():
    payload = _live_payload()
    payload["game"]["gameStatus"] = 2
    adapter = NBALiveDataBoxscoreAdapter(session=_Session(payload))

    with pytest.raises(ProviderResponseError, match="final governed game"):
        adapter.fetch_game_stats("0022400001", "2024-25")
