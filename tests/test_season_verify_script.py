"""Offline contract for the read-only held-candidate verification script."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.canonical_game_ledger import CanonicalGameLedgerGame
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    PublicationVersion,
)
from scripts import season_verify
from scripts.season_verify import (
    NbaReferenceError,
    NbaStatsReference,
    main,
    newest_held_candidate,
    open_read_only_engine,
)
from tests.support.season_cutover import NEW, NEW_GAMES, log_rows, payload_for

CUTOFF = datetime(2026, 10, 24, 4, tzinfo=UTC)
# The default governed games are scheduled six hours before the cutoff:
# 2026-10-23 18:00 US Eastern.
GAME_DATE = date(2026, 10, 23)
SEASON_STREAM = "traditional_opponent_season"
L15_STREAM = "traditional_opponent_l15"
LAST_GAME = NEW_GAMES[-1][0]

# Ledger field -> NBA column, written out so a renamed mapping fails here.
LOG_COLUMNS = {
    "points": "PTS",
    "rebounds": "REB",
    "assists": "AST",
    "field_goals_made": "FGM",
    "field_goals_attempted": "FGA",
    "three_pointers_made": "FG3M",
    "three_pointers_attempted": "FG3A",
    "free_throws_made": "FTM",
    "free_throws_attempted": "FTA",
    "offensive_rebounds": "OREB",
    "defensive_rebounds": "DREB",
    "turnovers": "TOV",
    "steals": "STL",
    "blocks": "BLK",
    "personal_fouls": "PF",
    "minutes": "MIN",
}
TEAM_COLUMNS = {
    "points": "OPP_PTS",
    "rebounds": "OPP_REB",
    "assists": "OPP_AST",
    "field_goals_made": "OPP_FGM",
    "field_goals_attempted": "OPP_FGA",
    "three_pointers_made": "OPP_FG3M",
    "three_pointers_attempted": "OPP_FG3A",
    "free_throws_made": "OPP_FTM",
    "free_throws_attempted": "OPP_FTA",
    "turnovers": "OPP_TOV",
    "steals": "OPP_STL",
    "blocks": "OPP_BLK",
    "personal_fouls": "OPP_PF",
    "offensive_rebounds": "OPP_OREB",
    "defensive_rebounds": "OPP_DREB",
}
PER36_COLUMNS = {
    "points": "PTS",
    "rebounds": "REB",
    "assists": "AST",
    "field_goals_made": "FGM",
    "field_goals_attempted": "FGA",
    "three_pointers_made": "FG3M",
    "three_pointers_attempted": "FG3A",
    "free_throws_made": "FTM",
    "free_throws_attempted": "FTA",
    "turnovers": "TOV",
    "steals": "STL",
    "blocks": "BLK",
    "personal_fouls": "PF",
}
# Every field gets its own value so a swapped or ignored mapping shows.
TEAM_COUNTS = {
    "points": 111.0,
    "rebounds": 45.0,
    "assists": 26.0,
    "field_goals_made": 41.0,
    "field_goals_attempted": 89.0,
    "three_pointers_made": 13.0,
    "three_pointers_attempted": 34.0,
    "free_throws_made": 19.0,
    "free_throws_attempted": 23.0,
    "turnovers": 15.0,
    "steals": 8.0,
    "blocks": 6.0,
    "personal_fouls": 20.0,
    "offensive_rebounds": 12.0,
    "defensive_rebounds": 33.0,
}


def _logs(games):
    rows = log_rows(NEW, games)
    for row in rows:
        for index, field in enumerate(LOG_COLUMNS):
            row[field] = 31.5 if field == "minutes" else 3 + 2 * index
    return rows


def _teams():
    def distinct(rows):
        for row in rows:
            row["counts"] = dict(TEAM_COUNTS)
            row["per48"] = dict(TEAM_COUNTS)

    rows = payload_for(SEASON_STREAM, NEW)
    distinct(rows)
    return rows


def _per36_rows():
    return [
        {
            "season": NEW,
            "player_id": player_id,
            "minutes": 500.0,
            "game_count": 10,
            "team_ids_at_game": [1610612737],
            **{f"{field}_per36": 10.04 + index + player_id for index, field in enumerate(PER36_COLUMNS)},
        }
        for player_id in (1, 2)
    ]


def _nba_logs(rows):
    return [
        {
            "PLAYER_ID": row["player_id"],
            "GAME_ID": row["game_id"],
            **{column: row[field] for field, column in LOG_COLUMNS.items()},
        }
        for row in rows
    ]


def _nba_teams(rows):
    return [
        {
            "TEAM_ID": row["team_id"],
            "GP": row["game_count"],
            **{column: row["counts"][field] for field, column in TEAM_COLUMNS.items()},
        }
        for row in rows
    ]


def _nba_per36(rows, *, rounded=False):
    return [
        {
            "PLAYER_ID": row["player_id"],
            "GP": row["game_count"],
            **{
                column: (round(row[f"{field}_per36"], 1) if rounded else row[f"{field}_per36"])
                for field, column in PER36_COLUMNS.items()
            },
        }
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


def _seed_authority(
    engine,
    *,
    cutoff=CUTOFF,
    scheduled=None,
    manifest_id="manifest-new",
    games=NEW_GAMES,
    created_at=None,
):
    """An Event Catalog and manifest governing NEW_GAMES, all final."""

    scheduled = scheduled or {}
    events = [
        {
            "nba_game_id": game_id,
            "home_team_id": int(home),
            "away_team_id": int(away),
            "phase": "Regular Season",
            "status": "Final",
            "status_code": 3,
            "scheduled_at": scheduled.get(game_id, cutoff - timedelta(hours=6)).isoformat(),
        }
        for game_id, home, away in games
    ]
    catalog = json.dumps({"events": events}, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(catalog.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            CatalogPublication.__table__.insert().values(
                publication_id=f"catalog-{manifest_id}",
                season=NEW,
                catalog_type="event",
                cutoff=cutoff,
                version="v1",
                checksum=checksum,
                payload=catalog,
                complete=True,
                published_at=cutoff,
            )
        )
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id=manifest_id,
                season=NEW,
                cutoff=cutoff,
                collect_before=cutoff + timedelta(days=1),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum=manifest_id,
                event_catalog_publication_id=f"catalog-{manifest_id}",
                event_catalog_checksum=checksum,
                status="active",
                created_at=created_at or cutoff,
            )
        )
    return {
        "manifest_id": manifest_id,
        "event_catalog_publication_id": f"catalog-{manifest_id}",
        "event_catalog_checksum": checksum,
    }


def _add(session, stream, payload, binding, *, version=1, cutoff=CUTOFF, status="candidate"):
    session.add(
        PublicationVersion(
            publication_id=f"{stream}-{version}-{status}",
            stream_key=stream,
            season=NEW,
            cutoff=cutoff,
            version=version,
            status=status,
            checksum="x",
            payload=json.dumps(payload),
            created_at=cutoff,
            fence=0,
            **binding,
        )
    )


def _world(tmp_path, *, cutoff=CUTOFF, scheduled=None, log_games=NEW_GAMES):
    engine = create_engine(f"sqlite:///{tmp_path / 'verify.sqlite3'}")
    run_migrations(engine)
    binding = _seed_authority(engine, cutoff=cutoff, scheduled=scheduled)
    logs, teams, per36 = _logs(log_games), _teams(), _per36_rows()
    with Session(engine) as session, session.begin():
        _add(session, "player_game_logs", {"rows": logs}, binding, cutoff=cutoff)
        _add(session, "player_per36", {"rows": per36}, binding, cutoff=cutoff)
        _add(session, SEASON_STREAM, teams, binding, cutoff=cutoff)
        _add(session, L15_STREAM, teams, binding, cutoff=cutoff)
    return engine, logs, per36, teams


@pytest.fixture
def world(tmp_path):
    return _world(tmp_path)


def _run(engine, nba, capsys, *extra):
    code = main(["--season", NEW, "--database-url", str(engine.url), *extra], nba=nba)
    return code, json.loads(capsys.readouterr().out)


def _stream(report, key):
    return next(item for item in report["streams"] if item["stream_key"] == key)


def _all_exact_reference(logs, per36, teams):
    return FakeNba(_nba_logs(logs), _nba_per36(per36), _nba_teams(teams))


def test_identical_candidates_and_reference_report_full_agreement(world, capsys):
    engine, logs, per36, teams = world
    nba = _all_exact_reference(logs, per36, teams)

    code, report = _run(engine, nba, capsys)

    assert code == 0 and report["passed"] is True
    stream = _stream(report, "player_game_logs")
    assert (stream["rows_compared"], stream["exact"], stream["match_rate"]) == (30, 30, 1.0)
    assert (stream["ledger_only"], stream["nba_only"], stream["top_mismatches"]) == ([], [], [])
    assert (stream["missing_governed_games"], stream["ungoverned_games"]) == ([], [])
    assert _stream(report, "player_per36")["exact"] == 2
    assert _stream(report, SEASON_STREAM)["rows_compared"] == 30
    assert nba.calls == [
        ("logs", NEW, GAME_DATE),
        ("per36", NEW, GAME_DATE),
        ("teams", NEW, GAME_DATE, 0),
        ("teams", NEW, GAME_DATE, 15),
    ]
    assert stream["date_to"] == "2026-10-23"


@pytest.mark.parametrize("field", list(LOG_COLUMNS))
def test_each_game_log_field_is_compared(world, capsys, field):
    engine, logs, per36, teams = world
    reference = _nba_logs(logs)
    reference[3][LOG_COLUMNS[field]] += 1

    code, report = _run(engine, FakeNba(reference, _nba_per36(per36), _nba_teams(teams)), capsys)

    stream = _stream(report, "player_game_logs")
    top = stream["top_mismatches"]
    assert code == 1 and stream["mismatch_count"] == 1 and stream["exact"] == 29
    assert top[0]["identity"] == f"{logs[3]['player_id']}:{logs[3]['game_id']}"
    assert (top[0]["field"], top[0]["difference"]) == (field, 1.0)
    assert top[0]["nba"] - top[0]["ledger"] == 1.0


@pytest.mark.parametrize("field", list(PER36_COLUMNS))
def test_each_per36_field_is_compared(world, capsys, field):
    engine, logs, per36, teams = world
    reference = _nba_per36(per36)
    reference[1][PER36_COLUMNS[field]] += 1

    code, report = _run(engine, FakeNba(_nba_logs(logs), reference, _nba_teams(teams)), capsys)

    top = _stream(report, "player_per36")["top_mismatches"]
    assert code == 1 and len(top) == 1
    assert (top[0]["identity"], top[0]["field"]) == ("2", f"{field}_per36")
    assert top[0]["ledger"] == 10.04 + list(PER36_COLUMNS).index(field) + 2


@pytest.mark.parametrize("field", list(TEAM_COLUMNS))
@pytest.mark.parametrize("stream_key", [SEASON_STREAM, L15_STREAM])
def test_each_team_count_is_compared(world, capsys, field, stream_key):
    engine, logs, per36, teams = world
    reference = _nba_teams(teams)
    reference[0][TEAM_COLUMNS[field]] += 2

    code, report = _run(engine, FakeNba(_nba_logs(logs), _nba_per36(per36), reference), capsys)

    top = _stream(report, stream_key)["top_mismatches"]
    assert code == 1 and len(top) == 1
    assert (top[0]["field"], top[0]["ledger"], top[0]["nba"]) == (
        field,
        TEAM_COUNTS[field],
        TEAM_COUNTS[field] + 2,
    )


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
    assert code == 1
    assert _stream(report, "player_per36")["top_mismatches"][0]["field"] == "assists_per36"


def test_games_played_disagreement_is_reported(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_teams(teams)
    reference[5]["GP"] = 0

    code, report = _run(engine, FakeNba(_nba_logs(logs), _nba_per36(per36), reference), capsys)

    top = _stream(report, L15_STREAM)["top_mismatches"][0]
    assert code == 1 and (top["field"], top["ledger"], top["nba"]) == ("game_count", 1.0, 0.0)


def test_a_candidate_missing_the_latest_slate_fails_instead_of_shrinking_the_window(
    tmp_path,
    capsys,
):
    earlier = datetime(2026, 10, 22, 22, tzinfo=UTC)
    scheduled = {game_id: earlier for game_id, _h, _a in NEW_GAMES[:-1]}
    engine, logs, per36, teams = _world(
        tmp_path,
        scheduled=scheduled,
        log_games=NEW_GAMES[:-1],
    )
    nba = _all_exact_reference(logs, per36, teams)

    code, report = _run(engine, nba, capsys)

    stream = _stream(report, "player_game_logs")
    assert code == 1
    assert stream["date_to"] == "2026-10-23"
    assert nba.calls[0] == ("logs", NEW, date(2026, 10, 23))
    assert stream["missing_governed_games"] == [LAST_GAME]
    assert (stream["ungoverned_games"], stream["passed"]) == ([], False)


def test_team_game_sets_must_match_the_governed_games(world, capsys):
    engine, logs, per36, teams = world
    with Session(engine) as session, session.begin():
        row = session.query(PublicationVersion).filter_by(stream_key=SEASON_STREAM).one()
        payload = json.loads(row.payload)
        payload[0]["game_ids"] = [payload[0]["game_ids"][0], "bogus"]
        payload[0]["game_count"] = 2
        payload[1]["game_ids"] = [NEW_GAMES[1][0]]  # governed, but for other teams
        row.payload = json.dumps(payload)

    reference = _nba_teams(teams)
    reference[0]["GP"] = 2
    _code, report = _run(engine, FakeNba(_nba_logs(logs), _nba_per36(per36), reference), capsys)

    stream = _stream(report, SEASON_STREAM)
    team_ids = [row["team_id"] for row in payload[:2]]
    assert stream["ungoverned_games"] == sorted(
        [
            f"{team_ids[0]}:bogus",
            f"{team_ids[1]}:{NEW_GAMES[1][0]}",
        ]
    )
    assert stream["missing_governed_games"] == [f"{team_ids[1]}:{NEW_GAMES[0][0]}"]
    assert stream["passed"] is False


def test_the_window_ends_on_the_eastern_date_and_ignores_later_ledger_games(tmp_path, capsys):
    cutoff = datetime(2026, 10, 25, 4, tzinfo=UTC)
    late = datetime(2026, 10, 25, 2, 30, tzinfo=UTC)  # 22:30 on Oct 24 ET
    engine, logs, per36, teams = _world(
        tmp_path,
        cutoff=cutoff,
        scheduled={game_id: late for game_id, _h, _a in NEW_GAMES},
    )
    with engine.begin() as connection:  # a game that arrived after the cutoff
        connection.execute(
            CanonicalGameLedgerGame.__table__.insert().values(
                game_id="0022600099",
                season=NEW,
                season_type="Regular Season",
                game_date=date(2026, 10, 25),
                home_team_id=1610612737,
                home_team_tricode="H",
                away_team_id=1610612738,
                away_team_tricode="A",
                status="final",
                source_observation_id="o",
                checksum="c",
                retrieved_at=cutoff,
                updated_at=cutoff,
            )
        )
    nba = _all_exact_reference(logs, per36, teams)

    code, _report = _run(engine, nba, capsys)

    assert code == 0
    assert nba.calls == [
        ("logs", NEW, date(2026, 10, 24)),
        ("per36", NEW, date(2026, 10, 24)),
        ("teams", NEW, date(2026, 10, 24), 0),
        ("teams", NEW, date(2026, 10, 24), 15),
    ]


def test_an_unbound_candidate_is_ungoverned_and_fails(world, capsys):
    engine, logs, per36, teams = world
    with Session(engine) as session, session.begin():
        session.query(PublicationVersion).filter_by(stream_key="player_per36").one().manifest_id = None

    code, report = _run(engine, _all_exact_reference(logs, per36, teams), capsys)

    assert code == 1
    assert _stream(report, "player_per36")["status"] == "ungoverned"


def test_newest_held_candidate_orders_by_cutoff_then_version(world):
    engine, logs, _per36, _teams = world
    early = CUTOFF - timedelta(days=1)
    with Session(engine) as session, session.begin():
        stream = "player_game_logs"
        _add(session, stream, {}, {}, version=2, cutoff=CUTOFF)  # tie, higher version
        _add(session, stream, {}, {}, version=9, cutoff=early)  # older cutoff, highest version
        _add(session, stream, {}, {}, version=20, cutoff=CUTOFF, status="active")
        session.flush()
        chosen = newest_held_candidate(session, stream, NEW)
        assert (chosen.version, chosen.status) == (2, "candidate")
        assert newest_held_candidate(session, stream, "2027-28") is None


def test_a_stream_without_a_candidate_fails_and_assist_streams_have_no_reference(world, capsys):
    engine, logs, per36, teams = world
    with Session(engine) as session, session.begin():
        session.query(PublicationVersion).filter_by(stream_key="player_per36").delete()

    code, report = _run(engine, FakeNba(_nba_logs(logs), [], _nba_teams(teams)), capsys)

    assert code == 1
    assert _stream(report, "player_per36")["status"] == "no_candidate"
    assert [
        _stream(report, key)["status"]
        for key in (
            "assist_locations_season",
            "assist_locations_l15",
            "player_assist_locations",
        )
    ] == ["no_candidate"] * 3
    assert _stream(report, "assist_locations_l15")["passed"] is True
    assert _stream(report, "player_per36")["passed"] is False


def test_assist_candidates_are_counted_and_not_compared(world, capsys):
    engine, logs, per36, teams = world
    from app.services.ledger_derivations import ASSIST_DERIVED_METRICS

    def assist(row):
        block = {metric: 3.0 for metric in ASSIST_DERIVED_METRICS}
        return {
            **row,
            "counts": dict(block),
            "per48": dict(block),
            "league_average": dict(block),
            "population_sigma": {metric: 0.0 for metric in block},
            "competition_rank": {metric: 1 for metric in block},
        }

    with Session(engine) as session, session.begin():
        _add(session, "assist_locations_season", [assist(row) for row in teams], {})

    _code, report = _run(engine, _all_exact_reference(logs, per36, teams), capsys)

    stream = _stream(report, "assist_locations_season")
    assert (stream["status"], stream["rows"], stream["passed"]) == ("no_reference", 30, True)


def test_the_sqlite_connection_rejects_writes_and_the_script_writes_nothing(world, capsys, tmp_path):
    engine, logs, per36, teams = world
    guarded = open_read_only_engine(str(engine.url))
    with guarded.connect() as connection, pytest.raises(OperationalError):
        connection.execute(text("DELETE FROM publication_versions"))
    guarded.dispose()

    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(PublicationVersion))
    output = tmp_path / "report.json"
    code, report = _run(
        engine,
        _all_exact_reference(logs, per36, teams),
        capsys,
        "--output",
        str(output),
    )
    with Session(engine) as session:
        after = session.scalar(select(func.count()).select_from(PublicationVersion))

    assert (before, after, code) == (4, 4, 0)
    assert json.loads(output.read_text()) == report


def test_the_postgres_engine_is_configured_read_only(monkeypatch):
    seen = {}

    def fake_create_engine(url, **kwargs):
        seen["url"], seen["kwargs"] = url, kwargs
        return SimpleNamespace()

    monkeypatch.setattr(season_verify, "create_engine", fake_create_engine)

    open_read_only_engine("postgres://user:pw@db.example:5432/app")

    assert seen == {
        "url": "postgresql+psycopg2://user:pw@db.example:5432/app",
        "kwargs": {"connect_args": {"options": "-c default_transaction_read_only=on"}},
    }


def test_nba_requests_carry_the_bounds_and_proxy(world, capsys, monkeypatch):
    engine, logs, per36, teams = world
    secret = "http://user:hunter2@proxy.example:8080"
    calls = []

    def stub(name, records):
        def build(**kwargs):
            calls.append((name, kwargs))
            return SimpleNamespace(get_data_frames=lambda: [pd.DataFrame(records)])

        monkeypatch.setattr(season_verify.endpoints, name, build)

    stub("PlayerGameLogs", _nba_logs(logs))
    stub("LeagueDashPlayerStats", _nba_per36(per36))
    stub("LeagueDashTeamStats", _nba_teams(teams))

    code, report = _run(engine, None, capsys, "--proxy-url", secret)

    common = {"date_to_nullable": "10/23/2026", "proxy": secret, "timeout": 60.0}
    team = {
        "season": NEW,
        "season_type_all_star": "Regular Season",
        "measure_type_detailed_defense": "Opponent",
        "per_mode_detailed": "Totals",
        **common,
    }
    assert code == 0
    assert calls == [
        (
            "PlayerGameLogs",
            {
                "season_nullable": NEW,
                "season_type_nullable": "Regular Season",
                **common,
            },
        ),
        (
            "LeagueDashPlayerStats",
            {
                "season": NEW,
                "season_type_all_star": "Regular Season",
                "measure_type_detailed_defense": "Base",
                "per_mode_detailed": "Per36",
                **common,
            },
        ),
        ("LeagueDashTeamStats", {**team, "last_n_games": 0}),
        ("LeagueDashTeamStats", {**team, "last_n_games": 15}),
    ]
    assert report["proxy"] == "configured" and "hunter2" not in json.dumps(report)


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


def test_a_logs_game_outside_the_governed_set_fails(world, capsys):
    engine, logs, per36, teams = world
    stray = {**logs[0], "game_id": "0029999999", "player_id": 5000}
    with Session(engine) as session, session.begin():
        row = session.query(PublicationVersion).filter_by(stream_key="player_game_logs").one()
        row.payload = json.dumps({"rows": logs + [stray]})
    nba = _all_exact_reference(logs + [stray], per36, teams)

    code, report = _run(engine, nba, capsys)

    stream = _stream(report, "player_game_logs")
    assert code == 1 and stream["exact"] == 30
    assert stream["ledger_only"] == ["5000:0029999999"]
    assert (stream["ungoverned_games"], stream["passed"]) == (["0029999999"], False)


def test_l15_coverage_is_the_last_fifteen_governed_games_per_team(tmp_path, capsys):
    team_a, team_b = int(NEW_GAMES[0][1]), int(NEW_GAMES[0][2])
    games = tuple((f"00226{index:05d}", team_a, team_b) for index in range(16))
    scheduled = {
        game_id: CUTOFF - timedelta(hours=6 + 16 - index) for index, (game_id, _h, _a) in enumerate(games)
    }
    engine = create_engine(f"sqlite:///{tmp_path / 'l15.sqlite3'}")
    run_migrations(engine)
    binding = _seed_authority(engine, scheduled=scheduled, games=games)
    last_fifteen = [game_id for game_id, _h, _a in games[1:]]
    rows = payload_for(L15_STREAM, NEW)[:2]
    for row, team in zip(rows, (team_a, team_b), strict=False):
        row.update(team_id=team, game_ids=last_fifteen, game_count=15)
        row["counts"] = dict(TEAM_COUNTS)
        row["per48"] = dict(TEAM_COUNTS)
    with Session(engine) as session, session.begin():
        _add(session, L15_STREAM, rows, binding)

    _code, report = _run(engine, FakeNba(teams=_nba_teams(rows)), capsys)

    stream = _stream(report, L15_STREAM)
    assert (stream["missing_governed_games"], stream["ungoverned_games"]) == ([], [])
    assert stream["rows_compared"] == 2 and stream["passed"] is True


def test_per36_games_played_below_the_candidates_is_a_mismatch(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_per36(per36)
    reference[0]["GP"] = 9

    code, report = _run(engine, FakeNba(_nba_logs(logs), reference, _nba_teams(teams)), capsys)

    top = _stream(report, "player_per36")["top_mismatches"]
    assert code == 1 and len(top) == 1
    assert (top[0]["identity"], top[0]["field"], top[0]["ledger"], top[0]["nba"]) == (
        "1",
        "game_count",
        10.0,
        9.0,
    )


def test_minutes_within_the_tolerance_are_matches_not_mismatches(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_logs(logs)
    reference[0]["MIN"] += 0.05

    code, report = _run(engine, FakeNba(reference, _nba_per36(per36), _nba_teams(teams)), capsys)

    stream = _stream(report, "player_game_logs")
    assert code == 0
    assert (stream["exact"], stream["within_tolerance"], stream["mismatch_count"]) == (29, 1, 0)


def test_top_mismatches_are_the_ten_largest_differences_first(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_logs(logs)
    for index in range(12):
        reference[index]["PTS"] += index + 1

    _code, report = _run(engine, FakeNba(reference, _nba_per36(per36), _nba_teams(teams)), capsys)

    stream = _stream(report, "player_game_logs")
    assert stream["mismatch_count"] == 12
    assert [item["difference"] for item in stream["top_mismatches"]] == [float(n) for n in range(12, 2, -1)]
    assert [item["identity"] for item in stream["top_mismatches"]] == [
        f"{logs[i]['player_id']}:{logs[i]['game_id']}" for i in range(11, 1, -1)
    ]


def test_reference_games_outside_the_governed_set_are_dropped_not_compared(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_logs(logs) + [
        {**_nba_logs(logs)[0], "GAME_ID": "0022699999", "PTS": 99},
        {**_nba_logs(logs)[1], "GAME_ID": "0022699999"},
    ]

    code, report = _run(engine, FakeNba(reference, _nba_per36(per36), _nba_teams(teams)), capsys)

    stream = _stream(report, "player_game_logs")
    assert code == 0
    assert (stream["reference_outside_window"], stream["nba_only"], stream["exact"]) == (2, [], 30)


def test_a_reference_with_more_games_than_governed_is_an_incomparable_window(world, capsys):
    engine, logs, per36, teams = world
    reference = _nba_teams(teams)
    reference[0]["GP"] = 2
    reference[0]["OPP_PTS"] += 40
    per36_reference = _nba_per36(per36)
    per36_reference[1]["GP"] = 11
    per36_reference[1]["PTS"] += 5

    code, report = _run(
        engine,
        FakeNba(_nba_logs(logs), per36_reference, reference),
        capsys,
    )

    team_stream = _stream(report, SEASON_STREAM)
    assert code == 1 and team_stream["passed"] is False
    assert team_stream["status"] == "window_incomparable"
    assert team_stream["incomparable"] == [
        {"identity": str(teams[0]["team_id"]), "nba_games": 2, "governed_games": 1},
    ]
    assert (team_stream["mismatch_count"], team_stream["rows_compared"]) == (0, 29)
    player_stream = _stream(report, "player_per36")
    assert player_stream["status"] == "window_incomparable"
    assert player_stream["incomparable"] == [
        {"identity": "2", "nba_games": 11, "governed_games": 10},
    ]
    assert (player_stream["mismatch_count"], player_stream["rows_compared"]) == (0, 1)


def test_a_null_nba_statistic_is_rejected_not_matched(world, capsys, monkeypatch):
    engine, logs, per36, teams = world
    records = _nba_logs(logs)
    records[2]["PTS"] = None

    def build(**kwargs):
        return SimpleNamespace(get_data_frames=lambda: [pd.DataFrame(records)])

    monkeypatch.setattr(season_verify.endpoints, "PlayerGameLogs", build)

    code = main(["--season", NEW, "--database-url", str(engine.url)])

    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert json.loads(captured.err) == {
        "error": "NBA.com response has a non-numeric statistic",
    }


def test_the_candidates_own_manifest_governs_when_manifests_compete(tmp_path, capsys):
    engine = create_engine(f"sqlite:///{tmp_path / 'compete.sqlite3'}")
    run_migrations(engine)
    earlier = CUTOFF - timedelta(hours=1)
    mine = _seed_authority(engine, manifest_id="mine", created_at=earlier)
    # Newer, at the same cutoff, governing a day earlier: a lookup that
    # ignores the candidate's manifest would pick this one.
    _seed_authority(
        engine,
        manifest_id="rival",
        created_at=CUTOFF,
        scheduled={g: CUTOFF - timedelta(days=1, hours=6) for g, _h, _a in NEW_GAMES},
    )
    with Session(engine) as session, session.begin():
        _add(session, "player_game_logs", {"rows": _logs(NEW_GAMES)}, mine)
    nba = FakeNba(_nba_logs(_logs(NEW_GAMES)))

    _run(engine, nba, capsys)

    assert nba.calls[0] == ("logs", NEW, GAME_DATE)


@pytest.mark.parametrize(
    "corrupt",
    [
        {"manifest_id": "rival"},
        {"event_catalog_checksum": "0" * 64},
        {"event_catalog_publication_id": "catalog-rival"},
    ],
)
def test_a_candidate_whose_authority_binding_is_not_exact_is_ungoverned(
    tmp_path,
    capsys,
    corrupt,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding.sqlite3'}")
    run_migrations(engine)
    binding = _seed_authority(engine)
    _seed_authority(engine, manifest_id="rival", created_at=CUTOFF - timedelta(hours=1))
    with Session(engine) as session, session.begin():
        _add(session, "player_game_logs", {"rows": _logs(NEW_GAMES)}, {**binding, **corrupt})

    code, report = _run(engine, FakeNba(_nba_logs(_logs(NEW_GAMES))), capsys)

    stream = _stream(report, "player_game_logs")
    assert code == 1 and (stream["status"], stream["passed"]) == ("ungoverned", False)
