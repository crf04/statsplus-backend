"""Each game's ESPN line, stored for the Matchup (crf04/statsplus#493)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.services.game_context import GameContextRepository
from app.services.game_lines import collect, read_game_line
from tests.support.game_context_world import migrated_engine, schedule

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "espn" / "scoreboard_2026-11-10.json"
SEASON = "2026-27"
BEFORE_TIP = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 10, 23, 55, tzinfo=UTC)
AFTER_TIP = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
#: The fixture's six games as the Event Catalog names them: away, home, tip.
GAMES = {
    "0022600150": ("MIL", "NYK", "2026-11-11T00:30:00+00:00"),
    "0022600151": ("BOS", "PHI", "2026-11-11T01:00:00+00:00"),
    "0022600152": ("LAC", "PHX", "2026-11-11T02:00:00+00:00"),
    "0022600153": ("MIA", "UTA", "2026-11-11T02:00:00+00:00"),
    "0022600154": ("ORL", "SAC", "2026-11-11T03:00:00+00:00"),
    "0022600155": ("OKC", "POR", "2026-11-11T04:00:00+00:00"),
}
NYK_GAME = "0022600150"


def team(tricode):
    return (NBA_TEAM_TRICODE_TO_ID[tricode], tricode)


def scoreboard():
    return json.loads(FIXTURE.read_text())


def espn_event(payload, abbreviation):
    return next(
        event
        for event in payload["events"]
        if any(
            side["team"]["abbreviation"] == abbreviation for side in event["competitions"][0]["competitors"]
        )
    )


def world(tmp_path, games=GAMES):
    engine = migrated_engine(tmp_path)
    for game_id, (away, home, tip) in games.items():
        schedule(engine, game_id, tip, team(away), team(home), season=SEASON)
    return engine


def run(engine, payload, clock):
    """One collection whose fetch serves ``payload`` for the Eastern date 2026-11-10 only."""

    return collect(
        engine,
        fetch=lambda day: payload if day == date(2026, 11, 10) else {"events": []},
        clock=lambda: clock,
    )


def line(engine, game_id):
    with engine.connect() as connection:
        return read_game_line(connection, game_id)


def test_the_fixture_lines_are_stored_for_their_event_catalog_games(tmp_path):
    engine = world(tmp_path)

    summary = run(engine, scoreboard(), BEFORE_TIP)

    assert summary == {
        "date": "2026-11-10",
        "events": 6,
        "lines": 6,
        "inserted": 6,
        "updated": 0,
        "unchanged": 0,
        "stale": 0,
        "unmatched": [],
    }
    assert line(engine, NYK_GAME) == {
        "status": "available",
        "source": "espn_scoreboard",
        "provider": "DraftKings",
        "read_at": "2026-11-10T22:00:00+00:00",
        "favorite_team_id": 1610612752,
        "favorite_tricode": "NYK",
        "spread": -15.5,
        "total": 222.5,
        "teams": [
            {
                "team_id": 1610612749,
                "tricode": "MIL",
                "is_home": False,
                "spread": 15.5,
                "implied_total": 103.5,
            },
            {
                "team_id": 1610612752,
                "tricode": "NYK",
                "is_home": True,
                "spread": -15.5,
                "implied_total": 119.0,
            },
        ],
    }
    summaries = {}
    for game_id in GAMES:
        stored = line(engine, game_id)
        summaries[game_id] = (
            stored["favorite_tricode"],
            stored["spread"],
            stored["total"],
            [(side["tricode"], side["implied_total"]) for side in stored["teams"]],
        )
    # ESPN's abbreviations differ for NY and UTAH; BOS, MIA, ORL and OKC are away favourites.
    assert summaries == {
        "0022600150": ("NYK", -15.5, 222.5, [("MIL", 103.5), ("NYK", 119.0)]),
        "0022600151": ("BOS", -1.5, 217.5, [("BOS", 109.5), ("PHI", 108.0)]),
        "0022600152": ("PHX", -3.5, 224.5, [("LAC", 110.5), ("PHX", 114.0)]),
        "0022600153": ("MIA", -9.5, 227.5, [("MIA", 118.5), ("UTA", 109.0)]),
        "0022600154": ("ORL", -5.5, 222.5, [("ORL", 114.0), ("SAC", 108.5)]),
        "0022600155": ("OKC", -1.5, 226.5, [("OKC", 114.0), ("POR", 112.5)]),
    }


def test_a_game_with_no_stored_line_says_so(tmp_path):
    engine = world(tmp_path)

    assert line(engine, NYK_GAME) == {
        "status": "unavailable",
        "source": "espn_scoreboard",
        "provider": None,
        "read_at": None,
        "favorite_team_id": None,
        "favorite_tricode": None,
        "spread": None,
        "total": None,
        "teams": [],
    }


def test_a_rerun_rereads_the_line_and_changes_no_value(tmp_path):
    engine = world(tmp_path)
    run(engine, scoreboard(), BEFORE_TIP)
    first = line(engine, NYK_GAME)

    summary = run(engine, scoreboard(), LATER)

    assert (summary["inserted"], summary["updated"], summary["unchanged"]) == (0, 0, 6)
    assert line(engine, NYK_GAME) == {**first, "read_at": "2026-11-10T23:55:00+00:00"}


def test_a_moved_line_replaces_the_stored_one(tmp_path):
    engine = world(tmp_path)
    run(engine, scoreboard(), BEFORE_TIP)
    payload = scoreboard()
    odds = espn_event(payload, "NY")["competitions"][0]["odds"][0]
    odds.update(details="NY -14", spread=-14.0, overUnder=224.0)

    summary = run(engine, payload, LATER)

    assert (summary["updated"], summary["unchanged"]) == (1, 5)
    stored = line(engine, NYK_GAME)
    assert (stored["spread"], stored["total"], stored["read_at"]) == (
        -14.0,
        224.0,
        "2026-11-10T23:55:00+00:00",
    )
    assert [side["implied_total"] for side in stored["teams"]] == [105.0, 119.0]


@pytest.mark.parametrize("state", ["in", "post"])
def test_after_tip_the_last_line_read_before_it_is_kept(tmp_path, state):
    engine = world(tmp_path)
    run(engine, scoreboard(), BEFORE_TIP)
    before = line(engine, NYK_GAME)
    payload = scoreboard()
    event = espn_event(payload, "NY")
    event["status"]["type"]["state"] = state
    # A whole, consistent line that differs from the stored one: it is still not written.
    event["competitions"][0]["odds"][0].update(details="NY -30.5", spread=-30.5, overUnder=240.5)

    summary = run(engine, payload, AFTER_TIP)

    assert (summary["lines"], summary["unchanged"]) == (5, 5)
    assert line(engine, NYK_GAME) == before


def test_a_line_that_disappears_before_tip_keeps_the_last_one_read(tmp_path):
    engine = world(tmp_path)
    run(engine, scoreboard(), BEFORE_TIP)
    before = line(engine, NYK_GAME)
    payload = scoreboard()
    espn_event(payload, "NY")["competitions"][0].pop("odds")

    run(engine, payload, LATER)

    assert line(engine, NYK_GAME) == before


def test_a_pickem_has_no_favourite_and_splits_the_total(tmp_path):
    engine = world(tmp_path)
    payload = scoreboard()
    odds = espn_event(payload, "NY")["competitions"][0]["odds"][0]
    odds.update(details="EVEN", spread=0.0, overUnder=221.0)
    odds["homeTeamOdds"]["favorite"] = False

    run(engine, payload, BEFORE_TIP)

    stored = line(engine, NYK_GAME)
    assert (stored["favorite_team_id"], stored["favorite_tricode"], stored["spread"]) == (None, None, 0.0)
    assert [(side["spread"], side["implied_total"]) for side in stored["teams"]] == [
        (0.0, 110.5),
        (0.0, 110.5),
    ]


def test_espn_abbreviations_that_differ_from_nba_tricodes_match_their_games(tmp_path):
    games = {
        "0022600160": ("GSW", "SAS", "2026-11-11T00:00:00+00:00"),
        "0022600161": ("NOP", "WAS", "2026-11-11T00:00:00+00:00"),
    }
    engine = world(tmp_path, games)
    payload = scoreboard()
    template = espn_event(payload, "NY")
    events = []
    for event_id, (away, home) in (("1", ("GS", "SA")), ("2", ("NO", "WSH"))):
        event = copy.deepcopy(template)
        event["id"] = event_id
        event["date"] = "2026-11-11T00:00Z"
        sides = event["competitions"][0]["competitors"]
        next(side for side in sides if side["homeAway"] == "away")["team"]["abbreviation"] = away
        next(side for side in sides if side["homeAway"] == "home")["team"]["abbreviation"] = home
        event["competitions"][0]["odds"][0]["details"] = f"{home} -15.5"
        events.append(event)

    summary = run(engine, {"events": events}, BEFORE_TIP)

    assert (summary["inserted"], summary["unmatched"]) == (2, [])
    assert [side["tricode"] for side in line(engine, "0022600160")["teams"]] == ["GSW", "SAS"]
    assert line(engine, "0022600161")["favorite_tricode"] == "WAS"


def test_espn_listing_the_teams_the_other_way_round_still_states_each_teams_own_spread(tmp_path):
    # A neutral-site game can be listed with the other team at home.
    engine = world(tmp_path, {NYK_GAME: ("NYK", "MIL", "2026-11-11T00:30:00+00:00")})

    run(engine, scoreboard(), BEFORE_TIP)

    stored = line(engine, NYK_GAME)
    assert (stored["favorite_tricode"], stored["spread"]) == ("NYK", -15.5)
    assert [
        (side["tricode"], side["is_home"], side["spread"], side["implied_total"]) for side in stored["teams"]
    ] == [
        ("NYK", False, -15.5, 119.0),
        ("MIL", True, 15.5, 103.5),
    ]


def test_a_game_on_another_eastern_date_or_twice_in_the_catalog_is_not_matched(tmp_path):
    games = {
        # The same teams the night before: not the fixture's game.
        "0022600140": ("MIL", "NYK", "2026-11-10T00:30:00+00:00"),
        # Two catalog rows for one ESPN game: ambiguous, so neither is written.
        "0022600151": ("BOS", "PHI", "2026-11-11T01:00:00+00:00"),
        "0022600159": ("BOS", "PHI", "2026-11-10T23:00:00+00:00"),
    }
    engine = world(tmp_path, games)

    summary = run(engine, scoreboard(), BEFORE_TIP)

    assert summary["inserted"] == 0
    assert summary["unmatched"] == [
        "401909960 MIL@NYK",
        "401909961 BOS@PHI",
        "401909962 LAC@PHX",
        "401909963 MIA@UTA",
        "401909964 ORL@SAC",
        "401909965 OKC@POR",
    ]
    assert [line(engine, game_id)["status"] for game_id in games] == ["unavailable"] * 3


def test_a_postponed_catalog_game_is_not_matched(tmp_path):
    engine = world(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE event_catalog SET status_text = 'Postponed', postponed_status = 'PPD' WHERE nba_game_id = :id"
            ),
            {"id": NYK_GAME},
        )

    summary = run(engine, scoreboard(), BEFORE_TIP)

    assert summary["unmatched"] == ["401909960 MIL@NYK"]
    assert line(engine, NYK_GAME)["status"] == "unavailable"


def test_a_spread_that_contradicts_the_favourite_espn_names_is_not_stored(tmp_path):
    engine = world(tmp_path)
    payload = scoreboard()
    # The numeric spread says the home team is favoured; the display says the away team is.
    espn_event(payload, "NY")["competitions"][0]["odds"][0]["details"] = "MIL -15.5"

    summary = run(engine, payload, BEFORE_TIP)

    assert summary["lines"] == 5
    assert line(engine, NYK_GAME)["status"] == "unavailable"


@pytest.mark.parametrize(
    "odds",
    [
        {"spread": -15.5, "overUnder": None},
        {"spread": None, "overUnder": 222.5},
        {"spread": "-15.5", "overUnder": 222.5},
        {"spread": float("nan"), "overUnder": 222.5},
        {"spread": -15.5, "overUnder": True},
        {"spread": -15.5, "overUnder": 0},
        {"spread": -15.5, "overUnder": float("inf")},
    ],
)
def test_an_incomplete_or_malformed_line_is_not_stored(tmp_path, odds):
    engine = world(tmp_path)
    payload = scoreboard()
    espn_event(payload, "NY")["competitions"][0]["odds"][0].update(odds)

    assert run(engine, payload, BEFORE_TIP)["lines"] == 5
    assert line(engine, NYK_GAME)["status"] == "unavailable"


def test_the_stored_game_context_carries_the_line_with_or_without_team_rows(tmp_path):
    engine = world(tmp_path)
    run(engine, scoreboard(), BEFORE_TIP)

    context = GameContextRepository(engine).read(NYK_GAME)

    assert (context["status"], context["teams"]) == ("unavailable", [])
    assert context["game_line"] == line(engine, NYK_GAME)
    assert context["game_line"]["favorite_tricode"] == "NYK"


@pytest.mark.parametrize("broken", [{"date": "TBD"}, {"date": None}, {"id": None}, {"competitions": []}])
def test_a_malformed_event_is_skipped_and_the_others_are_stored(tmp_path, broken):
    engine = world(tmp_path)
    payload = scoreboard()
    espn_event(payload, "NY").update(broken)

    summary = run(engine, payload, BEFORE_TIP)

    assert (summary["lines"], summary["inserted"]) == (5, 5)
    assert line(engine, NYK_GAME)["status"] == "unavailable"


def test_a_finished_game_with_no_odds_keeps_its_last_line(tmp_path):
    engine = world(tmp_path)
    run(engine, scoreboard(), BEFORE_TIP)
    before = line(engine, NYK_GAME)
    payload = scoreboard()
    event = espn_event(payload, "NY")
    event["status"]["type"]["state"] = "post"
    event["competitions"][0]["odds"][0].update(details=None, spread=None, overUnder=None)

    run(engine, payload, AFTER_TIP)

    assert line(engine, NYK_GAME) == before


@pytest.mark.parametrize("details", ["EVEN", "PK"])
def test_a_pickem_display_with_a_nonzero_spread_is_not_stored(tmp_path, details):
    engine = world(tmp_path)
    payload = scoreboard()
    espn_event(payload, "NY")["competitions"][0]["odds"][0]["details"] = details

    assert run(engine, payload, BEFORE_TIP)["lines"] == 5
    assert line(engine, NYK_GAME)["status"] == "unavailable"


def test_an_overlapping_run_that_read_earlier_never_replaces_a_later_line(tmp_path):
    engine = world(tmp_path)
    payload = scoreboard()
    espn_event(payload, "NY")["competitions"][0]["odds"][0].update(
        details="NY -14", spread=-14.0, overUnder=224.0
    )
    run(engine, payload, LATER)

    # A run that read at 22:00 finishes after the 23:55 run.
    summary = run(engine, scoreboard(), BEFORE_TIP)

    assert (summary["inserted"], summary["updated"], summary["unchanged"], summary["stale"]) == (0, 0, 0, 6)
    stored = line(engine, NYK_GAME)
    assert (stored["spread"], stored["total"], stored["read_at"]) == (
        -14.0,
        224.0,
        "2026-11-10T23:55:00+00:00",
    )


def test_an_event_dated_another_eastern_day_does_not_take_this_days_game(tmp_path):
    engine = world(tmp_path)
    payload = scoreboard()
    # The same two teams again the next Eastern day, inside the catalog window this run reads.
    rematch = copy.deepcopy(espn_event(payload, "NY"))
    rematch["id"] = "401909999"
    rematch["date"] = "2026-11-12T00:30Z"
    rematch["competitions"][0]["odds"][0].update(details="NY -2.5", spread=-2.5, overUnder=230.5)
    payload["events"].append(rematch)

    summary = run(engine, payload, BEFORE_TIP)

    assert summary["unmatched"] == ["401909999 MIL@NYK"]
    stored = line(engine, NYK_GAME)
    assert (stored["spread"], stored["total"]) == (-15.5, 222.5)
