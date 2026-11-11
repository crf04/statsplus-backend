"""Stored daily lineups: the collection step and the team read (crf04/statsplus#492)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import requests
from sqlalchemy import select

from app.models.team_lineup import TeamLineup
from app.services.team_lineups import TeamLineupRepository, collect_team_lineups
from tests.support.game_context_world import migrated_engine, schedule

RECORDED = json.loads(Path("tests/fixtures/nba_daily_lineups/00_daily_lineups_20261110.json").read_text())
#: 22:30 US Eastern on 2026-11-10, already 2026-11-11 in UTC.
LATE_EVENING = datetime(2026, 11, 11, 3, 30, tzinfo=UTC)
OKC = (1610612760, "OKC")
POR = (1610612757, "POR")
NYK = (1610612752, "NYK")
MIL = (1610612749, "MIL")

OKC_STARTERS = [
    {"player_id": 1642349, "name": "Ajay Mitchell", "position": "SG", "lineup_status": "Expected"},
    {"player_id": 1629026, "name": "Kenrich Williams", "position": "SF", "lineup_status": "Expected"},
    {"player_id": 1628983, "name": "Shai Gilgeous-Alexander", "position": "PG", "lineup_status": "Expected"},
    {"player_id": 1643530, "name": "Aday Mara", "position": "PF", "lineup_status": "Expected"},
    {"player_id": 1628392, "name": "Isaiah Hartenstein", "position": "C", "lineup_status": "Expected"},
]
OKC_INACTIVE = [
    {"player_id": 1631096, "name": "Chet Holmgren"},
    {"player_id": 1631114, "name": "Jalen Williams"},
    {"player_id": 1631119, "name": "Jaylin Williams"},
    {"player_id": 1641717, "name": "Cason Wallace"},
    {"player_id": 1642260, "name": "Nikola Topić"},
    {"player_id": 1642850, "name": "Thomas Sorber"},
]


def _fetched(payload, days=None):
    def fetch(day):
        if days is not None:
            days.append(day)
        return payload

    return fetch


def _stored(engine):
    with engine.connect() as connection:
        rows = connection.execute(select(TeamLineup.__table__)).mappings().all()
    return sorted((dict(row) for row in rows), key=lambda row: (row["game_id"], row["team_id"]))


def _world(tmp_path):
    engine = migrated_engine(tmp_path)
    # OKC @ POR tips 23:00 Eastern on 2026-11-10, which is 04:00 UTC on 2026-11-11.
    schedule(engine, "0022600216", "2026-11-11T04:00:00+00:00", OKC, POR, season="2026-27")
    schedule(engine, "0022600211", "2026-11-11T00:30:00+00:00", MIL, NYK, season="2026-27")
    return engine


def _repository(engine, now=LATE_EVENING):
    return TeamLineupRepository(engine, clock=lambda: now)


def test_the_step_stores_the_eastern_day_and_the_team_reads_tonights_lineup(tmp_path):
    engine = _world(tmp_path)
    days = []

    result = collect_team_lineups(engine, fetch=_fetched(RECORDED, days), clock=lambda: LATE_EVENING)

    assert days == [date(2026, 11, 10)]
    assert result == {"state": "stored", "date": "2026-11-10", "teams": 12}
    assert _repository(engine).read("OKC") == {
        "status": "posted",
        "game_id": "0022600216",
        "game_date": "2026-11-10",
        "matchup": "OKC @ POR",
        "feed_timestamp": "2026-11-10T22:29:43-05:00",
        "collected_at": "2026-11-11T03:30:00+00:00",
        "starters": OKC_STARTERS,
        "inactive": OKC_INACTIVE,
    }
    assert _repository(engine).read("NYK")["matchup"] == "NYK vs. MIL"


def test_running_the_step_twice_leaves_the_same_rows(tmp_path):
    engine = _world(tmp_path)

    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)
    first = _stored(engine)
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)

    assert len(first) == 12
    assert _stored(engine) == first


def test_a_failed_or_unpublished_fetch_changes_nothing_and_does_not_raise(tmp_path):
    engine = _world(tmp_path)
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)
    before = _stored(engine)

    def timeout(_day):
        raise requests.Timeout("read timed out")

    assert collect_team_lineups(engine, fetch=timeout, clock=lambda: LATE_EVENING) == {
        "state": "failed",
        "date": "2026-11-10",
        "error": "Timeout",
    }
    assert collect_team_lineups(engine, fetch=_fetched(None), clock=lambda: LATE_EVENING) == {
        "state": "not_published",
        "date": "2026-11-10",
    }
    assert collect_team_lineups(engine, fetch=_fetched({"games": "x"}), clock=lambda: LATE_EVENING) == {
        "state": "failed",
        "date": "2026-11-10",
        "error": "ProviderResponseError",
    }
    assert _stored(engine) == before


def test_a_database_failure_is_reported_not_raised(tmp_path):
    engine = migrated_engine(tmp_path)
    with engine.begin() as connection:
        TeamLineup.__table__.drop(connection)

    result = collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)

    assert result["state"] == "failed" and result["date"] == "2026-11-10"


def test_a_lineup_changed_after_it_was_confirmed_replaces_the_stored_one(tmp_path):
    engine = _world(tmp_path)
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)
    changed = json.loads(json.dumps(RECORDED))
    (okc_game,) = [game for game in changed["games"] if game["gameId"] == "0022600216"]
    for player in okc_game["awayTeam"]["players"]:
        if player["playerName"] == "Aday Mara" and player["position"]:
            player.update(personId=1642272, playerName="Jared McCain", lineupStatus="Confirmed")
            player["timestamp"] = "2026-11-10T22:40:00"
    later = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)

    collect_team_lineups(engine, fetch=_fetched(changed), clock=lambda: later)

    lineup = _repository(engine, later).read("OKC")
    assert [starter["name"] for starter in lineup["starters"]] == [
        "Ajay Mitchell",
        "Kenrich Williams",
        "Shai Gilgeous-Alexander",
        "Jared McCain",
        "Isaiah Hartenstein",
    ]
    assert lineup["starters"][3]["lineup_status"] == "Confirmed"
    assert lineup["feed_timestamp"] == "2026-11-10T22:40:00-05:00"
    assert lineup["collected_at"] == "2026-11-11T03:45:00+00:00"


def test_without_a_date_the_read_is_the_next_game_when_none_is_today(tmp_path):
    engine = _world(tmp_path)
    schedule(engine, "0022600230", "2026-11-13T00:00:00+00:00", (1610612737, "ATL"), OKC, season="2026-27")
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)
    tomorrow = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)

    assert _repository(engine, tomorrow).read("OKC") == {
        "status": "not_posted",
        "game_id": "0022600230",
        "game_date": "2026-11-12",
        "matchup": "OKC vs. ATL",
        "feed_timestamp": None,
        "collected_at": None,
        "starters": [],
        "inactive": [],
    }


def test_a_date_reads_that_days_game_and_a_day_without_one_is_no_game(tmp_path):
    engine = _world(tmp_path)
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)
    next_morning = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)

    assert _repository(engine, next_morning).read("OKC", day=date(2026, 11, 10))["starters"] == OKC_STARTERS
    assert _repository(engine, next_morning).read("OKC", day=date(2026, 11, 9)) == {
        "status": "no_game",
        "game_id": None,
        "game_date": None,
        "matchup": None,
        "feed_timestamp": None,
        "collected_at": None,
        "starters": [],
        "inactive": [],
    }


def test_a_postponed_game_is_no_game(tmp_path):
    engine = migrated_engine(tmp_path)
    schedule(engine, "0022600216", "2026-11-11T04:00:00+00:00", OKC, POR, season="2026-27", postponed=True)
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)

    assert _repository(engine).read("OKC")["status"] == "no_game"


def test_a_lineup_stored_for_a_game_since_moved_to_another_date_is_not_served(tmp_path):
    engine = migrated_engine(tmp_path)
    schedule(engine, "0022600216", "2026-11-13T03:00:00+00:00", OKC, POR, season="2026-27")
    collect_team_lineups(engine, fetch=_fetched(RECORDED), clock=lambda: LATE_EVENING)

    lineup = _repository(engine).read("OKC")
    assert (lineup["status"], lineup["game_date"], lineup["starters"]) == ("not_posted", "2026-11-12", [])


def test_a_team_with_inactive_players_but_no_starters_is_not_posted(tmp_path):
    engine = _world(tmp_path)
    benched = json.loads(json.dumps(RECORDED))
    (okc_game,) = [game for game in benched["games"] if game["gameId"] == "0022600216"]
    okc_game["awayTeam"]["players"] = [p for p in okc_game["awayTeam"]["players"] if not p["position"]]
    collect_team_lineups(engine, fetch=_fetched(benched), clock=lambda: LATE_EVENING)

    lineup = _repository(engine).read("OKC")
    assert (lineup["status"], lineup["starters"], lineup["inactive"]) == ("not_posted", [], OKC_INACTIVE)
    assert lineup["feed_timestamp"] == "2026-11-10T22:02:00-05:00"


def test_after_midnight_eastern_the_step_reads_the_new_day(tmp_path):
    engine = migrated_engine(tmp_path)
    days = []

    collect_team_lineups(
        engine, fetch=_fetched(None, days), clock=lambda: datetime(2026, 11, 11, 5, 5, tzinfo=UTC)
    )

    assert days == [date(2026, 11, 11)]
