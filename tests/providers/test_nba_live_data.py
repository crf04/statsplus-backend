import json
from pathlib import Path

import pytest

from app.providers.nba_live_data import (
    NBA_LIVE_DATA_BOXSCORE_URL,
    NBALiveDataBoxscoreAdapter,
    compose_pbp_live_data_observation,
)
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
            "fieldGoalsMade",
            "fieldGoalsAttempted",
            "threePointersMade",
            "threePointersAttempted",
            "freeThrowsMade",
            "freeThrowsAttempted",
            "reboundsOffensive",
            "reboundsDefensive",
            "reboundsTotal",
            "assists",
            "steals",
            "blocks",
            "turnovers",
            "foulsPersonal",
            "points",
            "twoPointersMade",
            "twoPointersAttempted",
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


def _pbp_from_live():
    observation = compose_pbp_live_data_observation(None, _live_payload(), event=_event())
    observation.pop("_ledger_provenance")
    return observation


def test_composite_preserves_pbp_values_and_fills_only_missing_counts():
    pbp = _pbp_from_live()
    del pbp["team_results"]["Home"]["FullGame"]["Blocks"]
    for row in pbp["stats"]["Home"]["FullGame"]:
        row.pop("Blocks", None)
    pbp["stats"]["Home"]["FullGame"][1]["Arc3Assists"] = 7
    pbp["stats"]["Home"]["FullGame"][1]["Assists"] = 4
    pbp["team_results"]["Home"]["FullGame"]["Assists"] = 4

    observation = compose_pbp_live_data_observation(pbp, _live_payload(), event=_event())
    game = canonical_game_from_pbp(observation, event=_event())

    assert game.team_facts[0].blocks == 3
    lebron = next(player for player in game.player_facts if player.player_id == 2544)
    assert lebron.arc3_assists == 7
    assert lebron.assists == 4
    assert observation["_ledger_provenance"]["provider"] == "pbp+nba_live_data"
    assert observation["_ledger_provenance"]["source_documents"]["pbp"] == pbp
    assert (
        observation["_ledger_provenance"]["source_documents"]["nba_live_data"]["game"]["gameId"]
        == "0022400001"
    )


def test_composite_adds_separately_reported_team_rebounds_to_components():
    live = _live_payload()
    totals = live["game"]["homeTeam"]["statistics"]
    totals.update(
        {
            "reboundsOffensive": 12,
            "reboundsDefensive": 31,
            "reboundsTotal": 51,
            "reboundsTeamOffensive": 5,
            "reboundsTeamDefensive": 3,
        }
    )

    observation = compose_pbp_live_data_observation(None, live, event=_event())
    game = canonical_game_from_pbp(observation, event=_event())
    home = next(fact for fact in game.team_facts if fact.team_id == 1610612747)

    assert home.offensive_rebounds == 17
    assert home.defensive_rebounds == 34
    assert home.rebounds == 51


def test_composite_uses_governed_live_home_away_and_complete_team_rows():
    pbp = _pbp_from_live()
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
    assert observation["_ledger_provenance"]["provider"] == "nba_live_data"
    assert observation["_ledger_provenance"]["source_documents"]["pbp"] == pbp


def test_live_only_fallback_is_accepted_without_claiming_pbp_provenance():
    observation = compose_pbp_live_data_observation(None, _live_payload(), event=_event())

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

    assert session.calls[0][0] == NBA_LIVE_DATA_BOXSCORE_URL.format(game_id="0022400001")
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


def test_adapter_fetches_play_by_play_and_records_telemetry():
    telemetry.clear_recorded_provider_events()
    payload = {"game": {"gameId": "0022400001", "actions": [{"actionNumber": 2}]}}
    session = _Session(payload)
    adapter = NBALiveDataBoxscoreAdapter(session=session)

    assert adapter.fetch_play_by_play("0022400001") == payload

    assert session.calls[0][0] == (
        "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/"
        "NBA/liveData/playbyplay/playbyplay_0022400001.json"
    )
    event = telemetry.get_recorded_provider_events()[-1]
    assert (event["provider"], event["operation"], event["outcome"]) == (
        "nba_live_data",
        "game_playbyplay",
        "success",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"game": {"gameId": "0022400002", "actions": []}},
        {"game": {"gameId": "0022400001"}},
        {"meta": {}},
    ],
)
def test_adapter_rejects_play_by_play_for_another_game_or_without_actions(payload):
    adapter = NBALiveDataBoxscoreAdapter(session=_Session(payload))

    with pytest.raises(ProviderResponseError, match="missing the game's actions"):
        adapter.fetch_play_by_play("0022400001")


def test_adapter_rejects_a_malformed_play_by_play_game_id_before_any_request():
    session = _Session({})

    with pytest.raises(ValueError, match="exactly 10 digits"):
        NBALiveDataBoxscoreAdapter(session=session).fetch_play_by_play("22400001")

    assert session.calls == []


def test_livedata_composite_identifies_itself_as_fallback_evidence_with_no_split():
    observation = compose_pbp_live_data_observation(None, _live_payload(), event=_event())
    game = canonical_game_from_pbp(observation, event=_event())

    team_rows = [row for row in game.raw_rows if row.row_type == "team"]
    assert {row.payload["LedgerEvidenceProvider"] for row in team_rows} == {"nba_live_data"}
    assert all(player.def_two_pt_rebounds is None for player in game.player_facts)


_FIXTURES = Path("tests/fixtures/nba_live_data")
_LOCATION_ATTRIBUTES = {
    "TwoPtAssists": "two_point_assists",
    "ThreePtAssists": "three_point_assists",
    "AtRimAssists": "at_rim_assists",
    "ShortMidRangeAssists": "short_mid_range_assists",
    "LongMidRangeAssists": "long_mid_range_assists",
    "Corner3Assists": "corner3_assists",
    "Arc3Assists": "arc3_assists",
    "DefTwoPtRebounds": "def_two_pt_rebounds",
    "DefThreePtRebounds": "def_three_pt_rebounds",
    "FTDefRebounds": "ft_def_rebounds",
    "OffTwoPtRebounds": "off_two_pt_rebounds",
    "OffThreePtRebounds": "off_three_pt_rebounds",
    "FTOffRebounds": "ft_off_rebounds",
}


def _recorded(game_id):
    box = json.loads((_FIXTURES / f"boxscore_{game_id}.json").read_text())
    play_by_play = json.loads((_FIXTURES / f"playbyplay_{game_id}.json").read_text())
    game = box["game"]
    event = {
        "nba_game_id": game_id,
        "season": "2026-27",
        "classification": "Regular Season",
        "scheduled_at": game["gameTimeUTC"],
        "home_team_id": game["homeTeam"]["teamId"],
        "home_team_tricode": game["homeTeam"]["teamTricode"],
        "away_team_id": game["awayTeam"]["teamId"],
        "away_team_tricode": game["awayTeam"]["teamTricode"],
        "status_code": 3,
        "status_text": "Final",
    }
    return box, play_by_play, event


def _location_counts(game):
    return {
        str(player.player_id): {
            wire: getattr(player, attribute) for wire, attribute in _LOCATION_ATTRIBUTES.items()
        }
        for player in game.player_facts
    }


@pytest.mark.parametrize("game_id", ["0022600003", "0022600005"])
def test_live_play_by_play_reproduces_pbpstats_assist_locations_and_rebound_types(game_id):
    box, play_by_play, event = _recorded(game_id)

    observation = compose_pbp_live_data_observation(None, box, event=event, live_play_by_play=play_by_play)
    game = canonical_game_from_pbp(observation, event=event)

    pbpstats = json.loads((_FIXTURES / f"pbpstats_location_counts_{game_id}.json").read_text())
    assert _location_counts(game) == pbpstats


@pytest.mark.parametrize(
    ("game_id", "team_totals"),
    [
        (
            "0022600009",
            {
                "HOU": (25, 11, 2, 3, 1, 8, 15, 16, 5, 5, 3, 0),
                "SAS": (21, 10, 0, 0, 3, 8, 19, 12, 0, 5, 4, 0),
            },
        ),
        (
            "0022600102",
            {
                "LAC": (24, 8, 3, 1, 4, 8, 7, 13, 3, 5, 5, 1),
                "LAL": (29, 17, 2, 0, 6, 4, 12, 13, 4, 5, 3, 0),
            },
        ),
    ],
)
def test_fallback_games_pbpstats_never_served_derive_complete_locations(game_id, team_totals):
    from app.services.ledger_derivations import derive_assist_location_facts

    box, play_by_play, event = _recorded(game_id)

    observation = compose_pbp_live_data_observation({}, box, event=event, live_play_by_play=play_by_play)
    game = canonical_game_from_pbp(observation, event=event)

    assert len(derive_assist_location_facts((game,))) == len(game.player_facts)
    fields = (
        "assists",
        "at_rim_assists",
        "short_mid_range_assists",
        "long_mid_range_assists",
        "corner3_assists",
        "arc3_assists",
        "def_two_pt_rebounds",
        "def_three_pt_rebounds",
        "ft_def_rebounds",
        "off_two_pt_rebounds",
        "off_three_pt_rebounds",
        "ft_off_rebounds",
    )
    assert {
        tricode: tuple(
            sum(getattr(player, field) for player in game.player_facts if player.team_tricode == tricode)
            for field in fields
        )
        for tricode in team_totals
    } == team_totals


def test_play_by_play_that_contradicts_the_box_leaves_the_fallback_without_a_split():
    box, play_by_play, event = _recorded("0022600009")
    rebound = next(
        action
        for action in play_by_play["game"]["actions"]
        if action["actionType"] == "rebound" and action.get("personId")
    )
    play_by_play["game"]["actions"].remove(rebound)
    assisted = next(action for action in play_by_play["game"]["actions"] if action.get("assistPersonId"))
    del assisted["assistPersonId"]

    observation = compose_pbp_live_data_observation({}, box, event=event, live_play_by_play=play_by_play)
    game = canonical_game_from_pbp(observation, event=event)

    assert {(player.at_rim_assists, player.def_two_pt_rebounds) for player in game.player_facts} == {
        (None, None)
    }


def test_a_rebound_off_a_heave_is_a_three_point_rebound():
    box, play_by_play, event = _recorded("0022600009")
    actions = play_by_play["game"]["actions"]
    shots = {action["actionNumber"]: action for action in actions}
    rebound = next(
        action
        for action in actions
        if action["actionType"] == "rebound"
        and action.get("personId")
        and shots[action["shotActionNumber"]]["actionType"] == "3pt"
    )
    shots[rebound["shotActionNumber"]]["actionType"] = "heave"

    observation = compose_pbp_live_data_observation({}, box, event=event, live_play_by_play=play_by_play)
    game = canonical_game_from_pbp(observation, event=event)

    assert (
        sum(player.def_three_pt_rebounds + player.off_three_pt_rebounds for player in game.player_facts) == 35
    )
