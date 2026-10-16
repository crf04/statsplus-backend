"""Rotation derivation from recorded NBA LiveData (crf04/statsplus#223).

The fixtures under ``tests/fixtures/nba_live_data`` are real 2025-26 LiveData
documents trimmed to what the derivation reads (substitution and period
actions; players' ids, names, starter flags and minutes):

* ``overtime``: 0022500123, WAS 134 - PHI 139 in one overtime.
* ``between_period``: 0022500500, MIL at SAC, with substitutions at the start
  of the second, third and fourth periods.
* ``corrupted``: ``between_period`` with the Q2-start substitution that put
  Ryan Rollins back on the floor deleted.
"""

import copy
import json
from pathlib import Path

import pytest

from app.services.rotation_derivation import (
    RotationRejected,
    Stint,
    clock_text,
    derive_rotation,
    parse_clock_cs,
)

FIXTURES = Path("tests/fixtures/nba_live_data")


def _load(name, play_by_play=None):
    box = json.loads((FIXTURES / f"boxscore_{name}.json").read_text())
    pbp = json.loads((FIXTURES / f"playbyplay_{play_by_play or name}.json").read_text())
    return box, pbp


def _player(rotation, name):
    return next(player for player in rotation.players if player.name == name)


def test_overtime_game_splits_minutes_by_period_and_marks_who_closed():
    rotation = derive_rotation(*_load("overtime"))

    assert (rotation.game_id, rotation.game_date, rotation.periods) == (
        "0022500123", "2025-10-28", 5,
    )
    assert (rotation.home_tricode, rotation.away_tricode) == ("WAS", "PHI")
    maxey = _player(rotation, "Tyrese Maxey")
    assert maxey.team_tricode == "PHI"
    assert maxey.started is True
    assert maxey.minutes_cs == 284600
    assert dict(maxey.period_cs) == {
        "Q1": 61500, "Q2": 72000, "Q3": 49100, "Q4": 72000, "OT": 30000,
    }
    assert maxey.closed is True
    assert len(maxey.stints) == 3
    # Joel Embiid started but was on the bench at the end of regulation.
    embiid = _player(rotation, "Joel Embiid")
    assert (embiid.started, embiid.closed, dict(embiid.period_cs)["OT"]) == (True, False, 0)
    # Bub Carrington was on court at the end of regulation and subbed out 9.3 s
    # before the overtime ended; Tre Johnson only came on late in overtime.
    carrington = _player(rotation, "Bub Carrington")
    assert dict(carrington.period_cs)["OT"] == 29070
    assert carrington.closed is True
    johnson = _player(rotation, "Tre Johnson")
    assert (dict(johnson.period_cs)["OT"], johnson.closed) == (930, False)


def test_overtime_game_keeps_every_box_score_player_including_those_who_did_not_play():
    rotation = derive_rotation(*_load("overtime"))

    assert len(rotation.players) == 35
    benched = [player for player in rotation.players if player.minutes_cs == 0]
    assert len(benched) == 13
    assert all(player.stints == () and not player.started and not player.closed
               for player in benched)
    assert sum(player.started for player in rotation.players) == 10
    assert sum(player.closed for player in rotation.players) == 10


def test_between_period_substitution_ends_one_stint_and_starts_another_at_the_boundary():
    rotation = derive_rotation(*_load("between_period"))

    # Kevin Porter Jr. leaves at the start of Q2: his Q1 stint ends at 0:00 of
    # Q1.  Ryan Rollins enters at the start of Q2 at 12:00.
    porter = _player(rotation, "Kevin Porter Jr.")
    assert porter.stints[0] == Stint(1, 72000, 1, 0, 72000)
    assert porter.stints[1].in_period == 2
    rollins = _player(rotation, "Ryan Rollins")
    assert rollins.stints[0] == Stint(1, 72000, 1, 40300, 31700)
    assert rollins.stints[1] == Stint(2, 72000, 2, 16500, 55500)
    assert rollins.minutes_cs == 203200
    assert dict(rollins.period_cs) == {
        "Q1": 31700, "Q2": 55500, "Q3": 44000, "Q4": 72000, "OT": 0,
    }


def test_a_player_on_court_at_a_period_boundary_keeps_one_stint_across_it():
    rotation = derive_rotation(*_load("between_period"))

    porter = _player(rotation, "Kevin Porter Jr.")
    # His Q2 stint began mid-period and ran through the break into Q3.
    assert porter.stints[1] == Stint(2, 34700, 3, 7300, 99400)
    assert dict(porter.period_cs)["Q2"] == 34700
    assert dict(porter.period_cs)["Q3"] == 64700


def test_rejects_a_game_whose_substitutions_contradict_each_other():
    with pytest.raises(RotationRejected, match="MIL has 4 on court at period action 140, not 5"):
        derive_rotation(*_load("between_period", play_by_play="corrupted"))


def test_rejects_a_game_whose_stints_are_off_the_box_score_minutes():
    box, pbp = _load("between_period")
    shifted = copy.deepcopy(pbp)
    action = next(
        action for action in shifted["game"]["actions"]
        if action["actionType"] == "substitution" and action["subType"] == "out"
        and action["period"] == 3
    )
    action["clock"] = "PT00M01.00S"

    with pytest.raises(RotationRejected, match="stints total"):
        derive_rotation(box, shifted)


def test_rejects_a_starter_flag_the_play_by_play_contradicts():
    box, pbp = _load("between_period")
    box = copy.deepcopy(box)
    starter = next(
        player for player in box["game"]["homeTeam"]["players"] if player["starter"] == "1"
    )
    starter["starter"] = "0"

    with pytest.raises(RotationRejected, match="SAC has 4 box-score starters, not 5"):
        derive_rotation(box, pbp)


def _synthetic_game(home_starters):
    """A 48-minute game with no substitutions; every starter plays all of it."""

    def team(team_id, tricode, first_id, starters):
        return {
            "teamId": team_id,
            "teamTricode": tricode,
            "players": [
                {
                    "personId": first_id + index,
                    "name": f"{tricode} {index}",
                    "starter": "1" if index < starters else "0",
                    "statistics": {
                        "minutes": "PT48M00.00S" if index < starters else "PT00M00.00S"
                    },
                }
                for index in range(8)
            ],
        }

    box = {"game": {
        "gameId": "0022500001", "gameStatus": 3, "period": 4, "gameEt": "2025-10-22T19:30:00-04:00",
        "homeTeam": team(1, "AAA", 100, home_starters),
        "awayTeam": team(2, "BBB", 200, 5),
    }}
    return box, {"game": {"gameId": "0022500001", "actions": []}}


def test_a_game_with_no_substitutions_is_accepted_with_five_starters_a_side():
    rotation = derive_rotation(*_synthetic_game(5))

    assert [player.minutes_cs for player in rotation.players if player.started] == [288000] * 10
    assert all(player.closed for player in rotation.players if player.started)


def _sub(action_number, kind, player_id, clock="PT10M00.00S", period=1):
    return {
        "actionNumber": action_number, "actionType": "substitution", "subType": kind,
        "personId": player_id, "period": period, "clock": clock,
    }


def _shot(action_number, clock="PT10M00.00S"):
    return {"actionNumber": action_number, "actionType": "2pt", "period": 1, "clock": clock}


def test_rejects_six_box_score_starters_even_when_one_exits_at_the_opening_tip():
    box, pbp = _synthetic_game(6)
    pbp["game"]["actions"] = [_sub(1, "out", 100, clock="PT12M00.00S")]

    with pytest.raises(RotationRejected, match="AAA has 6 box-score starters, not 5"):
        derive_rotation(box, pbp)


def test_rejects_six_on_court_during_a_shot_between_a_sub_in_and_a_sub_out_at_one_timestamp():
    box, pbp = _synthetic_game(5)
    pbp["game"]["actions"] = [
        _sub(1, "in", 105), _shot(2), _sub(3, "out", 100),
    ]

    with pytest.raises(RotationRejected, match="AAA has 6 on court at 2pt action 2, not 5"):
        derive_rotation(box, pbp)


def test_a_substitution_exchange_may_pass_through_four_or_six_between_actions():
    box, pbp = _synthetic_game(5)
    pbp["game"]["actions"] = [
        _sub(1, "out", 100, clock="PT10M00.00S", period=4),
        _sub(2, "in", 105, clock="PT10M00.00S", period=4),
    ]
    # Starter 100 plays Q1-Q3 and the first 2:00 of Q4; his replacement the rest.
    box["game"]["homeTeam"]["players"][0]["statistics"]["minutes"] = "PT38M00.00S"
    box["game"]["homeTeam"]["players"][5]["statistics"]["minutes"] = "PT10M00.00S"

    rotation = derive_rotation(box, pbp)

    assert [p.minutes_cs for p in rotation.players if p.player_id in (100, 105)] == [228000, 60000]


def test_rejects_four_on_court_for_a_second_between_two_substitutions():
    box, pbp = _synthetic_game(5)
    players = box["game"]["homeTeam"]["players"]
    # Starter 100 leaves at 10:00 of Q1; his replacement enters one second later
    # and nothing else happens in between.  Box minutes match the stints.
    players[0]["statistics"]["minutes"] = "PT02M00.00S"
    players[5]["statistics"]["minutes"] = "PT45M59.00S"
    pbp["game"]["actions"] = [
        _sub(1, "out", 100, clock="PT10M00.00S"),
        _sub(2, "in", 105, clock="PT09M59.00S"),
    ]

    with pytest.raises(RotationRejected, match="AAA has 4 on court at 12000 cs, not 5"):
        derive_rotation(box, pbp)


def test_rejects_six_on_court_at_the_final_whistle_from_a_zero_length_stint():
    box, pbp = _synthetic_game(5)
    # A sixth player enters as the clock hits 0:00 of Q4 and never leaves: no
    # time on court, so every minute still agrees and no interval has six.
    pbp["game"]["actions"] = [_sub(1, "in", 105, clock="PT00M00.00S", period=4)]

    with pytest.raises(RotationRejected, match="AAA has 6 on court at the end of the game, not 5"):
        derive_rotation(box, pbp)


@pytest.mark.parametrize("starter_minutes, bench_minutes, accepted", [
    ("PT48M01.00S", "PT00M00.00S", True),    # starter exactly 1.00 s over
    ("PT48M01.01S", "PT00M00.00S", False),   # just beyond 1 s over
    ("PT47M59.00S", "PT00M00.00S", True),    # starter exactly 1.00 s under
    ("PT47M58.99S", "PT00M00.00S", False),   # just beyond 1 s under
    ("PT48M00.00S", "PT00M01.00S", True),    # a bench player credited exactly 1.00 s
    ("PT48M00.00S", "PT00M01.01S", False),   # ... and just beyond
])
def test_box_score_minutes_are_accepted_within_exactly_one_second(
    starter_minutes, bench_minutes, accepted
):
    box, pbp = _synthetic_game(5)
    players = box["game"]["homeTeam"]["players"]
    players[0]["statistics"]["minutes"] = starter_minutes
    players[5]["statistics"]["minutes"] = bench_minutes

    if accepted:
        derive_rotation(box, pbp)
    else:
        with pytest.raises(RotationRejected, match="stints total"):
            derive_rotation(box, pbp)


@pytest.mark.parametrize("mutate, message", [
    (lambda box, pbp: pbp["game"].update(gameId="0022500999"), "different game"),
    (lambda box, pbp: box["game"].update(gameStatus=2), "not final"),
    (lambda box, pbp: pbp["game"].update(actions=None), "no actions"),
])
def test_rejects_documents_that_are_not_one_final_game(mutate, message):
    box, pbp = _load("between_period")
    box, pbp = copy.deepcopy(box), copy.deepcopy(pbp)
    mutate(box, pbp)

    with pytest.raises(RotationRejected, match=message):
        derive_rotation(box, pbp)


def test_clock_helpers_read_liveData_durations_and_print_arena_clock():
    assert parse_clock_cs("PT06M08.00S") == 36800
    assert parse_clock_cs("PT00M01.50S") == 150
    assert clock_text(72000) == "12:00"
    assert clock_text(16500) == "2:45"
    assert clock_text(0) == "0:00"
    with pytest.raises(RotationRejected):
        parse_clock_cs("12:00")
