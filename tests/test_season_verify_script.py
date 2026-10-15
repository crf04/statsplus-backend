"""Offline contract for the read-only held-candidate verification script."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.canonical_game_ledger import CanonicalGameLedgerGame
from app.models.collection_control import PublicationVersion
from scripts import season_verify
from scripts.season_verify import (
    NbaReferenceError,
    NbaStatsReference,
    main,
    open_read_only_engine,
)
from tests.support.season_cutover import NEW, NEW_GAMES, log_rows, payload_for

CUTOFF = datetime(2026, 10, 24, 4, tzinfo=timezone.utc)
GAME_DATE = date(2026, 10, 24)
SEASON_STREAM = "traditional_opponent_season"
L15_STREAM = "traditional_opponent_l15"

# Ledger field -> NBA column, written out so a renamed mapping fails here.
LOG_COLUMNS = {
    "points": "PTS", "rebounds": "REB", "assists": "AST",
    "field_goals_made": "FGM", "field_goals_attempted": "FGA",
    "three_pointers_made": "FG3M", "three_pointers_attempted": "FG3A",
    "free_throws_made": "FTM", "free_throws_attempted": "FTA",
    "offensive_rebounds": "OREB", "defensive_rebounds": "DREB",
    "turnovers": "TOV", "steals": "STL", "blocks": "BLK",
    "personal_fouls": "PF", "minutes": "MIN",
}
TEAM_COLUMNS = {
    "points": "OPP_PTS", "rebounds": "OPP_REB", "assists": "OPP_AST",
    "field_goals_made": "OPP_FGM", "field_goals_attempted": "OPP_FGA",
    "three_pointers_made": "OPP_FG3M", "three_pointers_attempted": "OPP_FG3A",
    "free_throws_made": "OPP_FTM", "free_throws_attempted": "OPP_FTA",
    "turnovers": "OPP_TOV", "steals": "OPP_STL", "blocks": "OPP_BLK",
    "personal_fouls": "OPP_PF", "offensive_rebounds": "OPP_OREB",
    "defensive_rebounds": "OPP_DREB",
}
PER36_COLUMNS = {
    "points": "PTS", "rebounds": "REB", "assists": "AST",
    "field_goals_made": "FGM", "field_goals_attempted": "FGA",
    "three_pointers_made": "FG3M", "three_pointers_attempted": "FG3A",
    "free_throws_made": "FTM", "free_throws_attempted": "FTA",
    "turnovers": "TOV", "steals": "STL", "blocks": "BLK",
    "personal_fouls": "PF",
}


def _per36_rows():
    return [
        {
            "season": NEW, "player_id": player_id, "minutes": 500.0,
            "game_count": 10, "team_ids_at_game": [1610612737],
            **{f"{field}_per36": 20.04 + player_id for field in PER36_COLUMNS},
        }
        for player_id in (1, 2)
    ]


def _nba_logs(rows):
    return [
        {"PLAYER_ID": row["player_id"], "GAME_ID": row["game_id"],
         **{column: row[field] for field, column in LOG_COLUMNS.items()}}
        for row in rows
    ]


def _nba_teams(rows):
    return [
        {"TEAM_ID": row["team_id"], "GP": row["game_count"],
         **{column: row["counts"][field] for field, column in TEAM_COLUMNS.items()}}
        for row in rows
    ]


def _nba_per36(rows, *, rounded=False):
    return [
        {"PLAYER_ID": row["player_id"], "GP": row["game_count"],
         **{column: (round(row[f"{field}_per36"], 1) if rounded
                     else row[f"{field}_per36"])
            for field, column in PER36_COLUMNS.items()}}
        for row in rows
    ]


class FakeNba:
    def __init__(self, logs=(), per36=(), teams=()):
        self.logs, self.per36, self.teams = list(logs), list(per36), list(teams)
        self.calls = []

    def player_game_logs(self, season, date_to):
        self.calls.append(("logs", season, date_to))
        return self.logs

    def player_per36(self, season, date_to):
        self.calls.append(("per36", season, date_to))
        return self.per36

    def team_opponent_totals(self, season, date_to, last_n_games):
        self.calls.append(("teams", season, date_to, last_n_games))
        return self.teams


def _add(session, stream, payload, *, version=1, cutoff=CUTOFF, status="candidate"):
    session.add(PublicationVersion(
        publication_id=f"{stream}-{version}-{status}", stream_key=stream,
        season=NEW, cutoff=cutoff, version=version, status=status,
        checksum="x", payload=json.dumps(payload), created_at=cutoff, fence=0,
    ))


@pytest.fixture
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'verify.sqlite3'}")
    run_migrations(engine)
    logs = log_rows(NEW, NEW_GAMES)
    teams = payload_for(SEASON_STREAM, NEW)
    per36 = _per36_rows()
    with Session(engine) as session, session.begin():
        for game_id, home, away in NEW_GAMES:
            session.add(CanonicalGameLedgerGame(
                game_id=game_id, season=NEW, season_type="Regular Season",
                game_date=GAME_DATE, home_team_id=int(home),
                home_team_tricode="H", away_team_id=int(away),
                away_team_tricode="A", status="final",
                source_observation_id="o", checksum="c",
                retrieved_at=CUTOFF, updated_at=CUTOFF,
            ))
        _add(session, "player_game_logs", {"rows": logs})
        _add(session, "player_per36", {"rows": per36})
        _add(session, SEASON_STREAM, teams)
        _add(session, L15_STREAM, teams)
    return engine, logs, per36, teams


def _run(engine, nba, capsys, *extra):
    code = main(["--season", NEW, "--database-url", str(engine.url), *extra], nba=nba)
    return code, json.loads(capsys.readouterr().out)


def _stream(report, key):
    return next(item for item in report["streams"] if item["stream_key"] == key)


def test_identical_candidates_and_reference_report_full_agreement(world, capsys):
    engine, logs, per36, teams = world
    nba = FakeNba(_nba_logs(logs), _nba_per36(per36), _nba_teams(teams))

    code, report = _run(engine, nba, capsys)

    assert code == 0 and report["passed"] is True
    stream = _stream(report, "player_game_logs")
    assert (stream["rows_compared"], stream["exact"], stream["match_rate"]) == (30, 30, 1.0)
    assert (stream["ledger_only"], stream["nba_only"], stream["top_mismatches"]) == ([], [], [])
    assert _stream(report, "player_per36")["exact"] == 2
    assert _stream(report, SEASON_STREAM)["rows_compared"] == 30
    assert nba.calls == [
        ("logs", NEW, GAME_DATE), ("per36", NEW, GAME_DATE),
        ("teams", NEW, GAME_DATE, 0), ("teams", NEW, GAME_DATE, 15),
    ]
    assert _stream(report, "player_game_logs")["date_to"] == "2026-10-24"


def test_a_perturbed_stat_is_a_mismatch_with_both_values_and_exits_one(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_logs(logs)
    reference[3]["PTS"] += 1
    teams_reference = _nba_teams(teams)
    teams_reference[0]["OPP_REB"] += 2

    code, report = _run(engine, FakeNba(reference, _nba_per36(per36), teams_reference), capsys)

    assert code == 1 and report["passed"] is False
    log_stream = _stream(report, "player_game_logs")
    assert (log_stream["exact"], log_stream["mismatch_count"], log_stream["passed"]) == (29, 1, False)
    assert log_stream["top_mismatches"] == [{
        "identity": f"{logs[3]['player_id']}:{logs[3]['game_id']}", "field": "points",
        "ledger": 10.0, "nba": 11.0, "difference": 1.0,
    }]
    team_stream = _stream(report, SEASON_STREAM)
    assert team_stream["top_mismatches"][0]["field"] == "rebounds"
    assert (team_stream["top_mismatches"][0]["ledger"], team_stream["top_mismatches"][0]["nba"]) == (44.0, 46.0)


def test_missing_player_games_are_ledger_only_or_nba_only(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_logs(logs)
    dropped = reference.pop(0)
    reference.append({**reference[0], "PLAYER_ID": 9999})

    code, report = _run(engine, FakeNba(reference, _nba_per36(per36), _nba_teams(teams)), capsys)

    stream = _stream(report, "player_game_logs")
    assert code == 1
    assert stream["ledger_only"] == [f"{dropped['PLAYER_ID']}:{dropped['GAME_ID']}"]
    assert stream["nba_only"] == [f"9999:{reference[0]['GAME_ID']}"]
    assert (stream["rows_compared"], stream["match_rate"]) == (29, 29 / 31)


def test_per36_accepts_nba_one_decimal_rounding_but_not_a_real_difference(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_per36(per36, rounded=True)

    code, report = _run(engine, FakeNba(_nba_logs(logs), reference, _nba_teams(teams)), capsys)
    stream = _stream(report, "player_per36")
    assert code == 0
    assert (stream["exact"], stream["within_tolerance"]) == (0, 2)

    reference[1]["AST"] += 0.5
    code, report = _run(engine, FakeNba(_nba_logs(logs), reference, _nba_teams(teams)), capsys)
    stream = _stream(report, "player_per36")
    assert code == 1
    assert stream["top_mismatches"][0]["field"] == "assists_per36"
    assert stream["top_mismatches"][0]["identity"] == "2"


def test_games_played_disagreement_is_reported(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_teams(teams)
    reference[5]["GP"] = 2

    code, report = _run(engine, FakeNba(_nba_logs(logs), _nba_per36(per36), reference), capsys)

    top = _stream(report, L15_STREAM)["top_mismatches"][0]
    assert code == 1 and (top["field"], top["ledger"], top["nba"]) == ("game_count", 1.0, 2.0)


def test_only_the_newest_held_candidate_is_verified(world, capsys):
    engine, logs, per36, teams = world
    newer = [{**row, "points": 12} for row in logs]
    later = datetime(2026, 10, 25, 4, tzinfo=timezone.utc)
    with Session(engine) as session, session.begin():
        _add(session, "player_game_logs", {"rows": newer}, version=2, cutoff=later)
        _add(session, "player_game_logs", {"rows": log_rows(NEW, NEW_GAMES, points=99)},
             version=3, cutoff=later, status="active")

    code, report = _run(
        engine, FakeNba(_nba_logs(newer), _nba_per36(per36), _nba_teams(teams)), capsys,
    )

    stream = _stream(report, "player_game_logs")
    assert code == 0 and stream["publication_id"] == "player_game_logs-2-candidate"
    assert stream["exact"] == 30


def test_a_stream_without_a_candidate_fails_and_assist_streams_have_no_reference(world, capsys):
    engine, logs, per36, teams = world
    with Session(engine) as session, session.begin():
        session.query(PublicationVersion).filter_by(stream_key="player_per36").delete()

    code, report = _run(engine, FakeNba(_nba_logs(logs), [], _nba_teams(teams)), capsys)

    assert code == 1
    assert _stream(report, "player_per36")["status"] == "no_candidate"
    assert [_stream(report, key)["status"] for key in (
        "assist_locations_season", "assist_locations_l15", "player_assist_locations",
    )] == ["no_candidate"] * 3
    assert _stream(report, "assist_locations_l15")["passed"] is True
    assert _stream(report, "player_per36")["passed"] is False


def test_assist_candidates_are_counted_and_not_compared(world, capsys):
    engine, logs, per36, teams = world
    from app.services.ledger_derivations import ASSIST_DERIVED_METRICS

    def assist(row):
        block = {metric: 3.0 for metric in ASSIST_DERIVED_METRICS}
        return {**row, "counts": dict(block), "per48": dict(block),
                "league_average": dict(block),
                "population_sigma": {metric: 0.0 for metric in block},
                "competition_rank": {metric: 1 for metric in block}}

    with Session(engine) as session, session.begin():
        _add(session, "assist_locations_season", [assist(row) for row in teams])

    _code, report = _run(
        engine, FakeNba(_nba_logs(logs), _nba_per36(per36), _nba_teams(teams)), capsys,
    )

    stream = _stream(report, "assist_locations_season")
    assert (stream["status"], stream["rows"], stream["passed"]) == ("no_reference", 30, True)


def test_the_connection_is_read_only_and_the_script_writes_nothing(world, capsys, tmp_path):
    engine, logs, per36, teams = world
    guarded = open_read_only_engine(str(engine.url))
    with guarded.connect() as connection, pytest.raises(OperationalError):
        connection.execute(text("DELETE FROM publication_versions"))
    guarded.dispose()

    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(PublicationVersion))
    output = tmp_path / "report.json"
    code, report = _run(
        engine, FakeNba(_nba_logs(logs), _nba_per36(per36), _nba_teams(teams)), capsys,
        "--output", str(output),
    )
    with Session(engine) as session:
        after = session.scalar(select(func.count()).select_from(PublicationVersion))

    assert (before, after, code) == (4, 4, 0)
    assert json.loads(output.read_text()) == report


def test_the_proxy_url_is_never_printed_or_carried_in_errors(world, capsys, monkeypatch):
    engine, *_ = world
    secret = "http://user:hunter2@proxy.example:8080"

    def boom(**kwargs):
        raise RuntimeError(f"cannot connect via {kwargs['proxy']}")

    monkeypatch.setattr(season_verify.endpoints, "PlayerGameLogs", boom)
    code = main(["--season", NEW, "--database-url", str(engine.url), "--proxy-url", secret])
    captured = capsys.readouterr()
    assert code == 2
    assert "hunter2" not in captured.out + captured.err
    assert json.loads(captured.err) == {"error": "NBA.com request failed (RuntimeError)"}

    with pytest.raises(NbaReferenceError) as raised:
        NbaStatsReference(proxy=secret).player_game_logs(NEW, GAME_DATE)
    assert "hunter2" not in str(raised.value) and raised.value.__cause__ is None

    code, report = _run(engine, FakeNba(), capsys, "--proxy-url", secret)
    assert report["proxy"] == "configured" and "hunter2" not in json.dumps(report)
