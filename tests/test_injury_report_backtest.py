"""The injury-report backtest scores recorded PDFs against a fixture season.

Fixture season: the games named by the recorded 2025-12-25 05:00 PM report,
the Athlete Catalog built from its names (two players deliberately left out),
and box-score rows chosen so each reported status has a known outcome.  No
network and no real database: downloads come from a fake CDN session and the
publication read is injected.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.event_catalog import EventCatalogEntry
from app.providers.nba_injury_report import entries_from_report, parse_injury_report
from app.services.player_game_log_repository import PlayerGameLogRecord
from scripts import injury_report_backtest as backtest


FIXTURES = Path(__file__).parent / "fixtures" / "injury_report"
LATE = (FIXTURES / "Injury-Report_2025-12-25_05_00PM.pdf").read_bytes()
SEASON = "2025-26"


@pytest.fixture
def require_pdftotext():
    """The independent row count needs poppler: CI must have it, never skip."""

    if shutil.which("pdftotext") is None:
        message = "pdftotext (poppler-utils) is not installed"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


needs_pdftotext = pytest.mark.usefixtures("require_pdftotext")

#: The CDN files the fixture season "published": 12-25's last report before
#: 5:20 PM ET, and the same bytes standing in for 12-26's last report.
SITE = {
    "Injury-Report_2025-12-25_05_00PM.pdf": LATE,
    "Injury-Report_2025-12-26_06_45PM.pdf": LATE,
}
NOT_IN_CATALOG = {"Harrison Ingram", "Taelon Peter"}
NOW = datetime(2026, 5, 1, tzinfo=timezone.utc)


class FakeCdn:
    def __init__(self, files):
        self.files = files
        self.headers = {}
        self.requested = []

    def get(self, url, timeout):
        name = url.rsplit("/", 1)[1]
        self.requested.append(name)
        if name in self.files:
            return SimpleNamespace(
                status_code=200,
                content=self.files[name],
                raise_for_status=lambda: None,
            )
        return SimpleNamespace(status_code=403, content=b"", raise_for_status=lambda: None)


def _game_id(index):
    return f"00225{index:05d}"


def _season_database(
    tmp_path,
    *,
    played_out=("Dante Exum", "Jaylin Williams"),
    omit=tuple(NOT_IN_CATALOG),
):
    """Returns (engine, SeasonInputs-ready pieces)."""

    report = parse_injury_report(LATE)
    matchups = sorted({(row.game_date, row.matchup) for row in report.rows})
    engine = create_engine(f"sqlite:///{tmp_path / 'season.sqlite3'}")
    run_migrations(engine)
    events, games = [], {}
    from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID

    for number, (day, matchup) in enumerate(matchups, start=1):
        away, _, home = matchup.partition("@")
        # 12-25 games tip at 5:20 PM ET (22:20Z); 12-26 games at 7:00 PM ET.
        tip = (
            datetime(2025, 12, 25, 22, 20, tzinfo=timezone.utc)
            if day.day == 25
            else datetime(2025, 12, 27, 0, 0, tzinfo=timezone.utc)
        )
        game_id = _game_id(number)
        games[(day, matchup)] = game_id
        events.append(
            {
                "nba_game_id": game_id,
                "season": SEASON,
                "home_team_id": NBA_TEAM_TRICODE_TO_ID[home],
                "home_team_name": home,
                "home_team_tricode": home,
                "away_team_id": NBA_TEAM_TRICODE_TO_ID[away],
                "away_team_name": away,
                "away_team_tricode": away,
                "scheduled_at": tip,
                "status_text": "Final",
                "status_code": 3,
                "classification": "Regular Season",
                "first_seen_at": NOW,
                "last_seen_at": NOW,
            }
        )
    # A preseason game and a playoff-id game must not become game dates.
    events.append(
        {
            **events[0],
            "nba_game_id": "0012500001",
            "classification": "Preseason",
            "scheduled_at": datetime(2025, 12, 20, 23, 0, tzinfo=timezone.utc),
        }
    )
    athletes = []
    for entry in entries_from_report(report, source_url="u"):
        if entry.source_player_name in omit:
            continue
        athletes.append(
            {
                "season": SEASON,
                "player_id": 1000 + len(athletes),
                "display_name": entry.source_player_name,
                "roster_status": "Active",
                "is_active": True,
                "is_active_for_season": True,
                "team_id": NBA_TEAM_TRICODE_TO_ID[entry.source_team_tricode],
                "team_name": entry.source_team_tricode,
                "team_abbreviation": entry.source_team_tricode,
                "published_at": NOW,
            }
        )
    with engine.begin() as connection:
        connection.execute(insert(EventCatalogEntry.__table__), events)
        connection.execute(insert(AthleteCatalog.__table__), athletes)
    player_id = {row["display_name"]: row["player_id"] for row in athletes}

    played = {"Luka Doncic", "Max Christie", "Evan Mobley", *played_out}
    logs = []
    # A box-score row with 0 minutes (dressed, did not play) is not "played".
    logs.append(
        _log(
            games[(date(2025, 12, 25), "DAL@GSW")],
            player_id["Dereck Lively II"],
            "Dereck Lively II",
            "DAL",
            "GSW",
            0.0,
        )
    )
    for (day, matchup), game_id in games.items():
        away, _, home = matchup.partition("@")
        # Every game has at least one box-score row (a bench player nobody reported).
        logs.append(_log(game_id, 7, "Bench Player", home, away, 9.0))
    for row in report.rows:
        if row.display_name in played and row.display_name in player_id:
            away, _, home = row.matchup.partition("@")
            logs.append(
                _log(
                    games[(row.game_date, row.matchup)],
                    player_id[row.display_name],
                    row.display_name,
                    row.team_tricode,
                    away if row.team_tricode == home else home,
                    31.5,
                )
            )
    return engine, tuple(logs), player_id


def _log(game_id, player_id, name, team, opponent, minutes):
    return PlayerGameLogRecord(
        season=SEASON,
        season_type="Regular Season",
        player_id=player_id,
        game_id=game_id,
        player_name=name,
        game_date=date(2025, 12, 25),
        team_id=1,
        team_tricode=team,
        opponent_team_id=2,
        opponent_team_tricode=opponent,
        is_home=True,
        minutes=minutes,
        points=10,
        rebounds=1,
        assists=1,
        field_goals_made=4,
        field_goals_attempted=9,
        three_pointers_made=1,
        three_pointers_attempted=3,
    )


class Publications:
    def __init__(self, logs, *, available=True):
        self.read_args = None
        self.logs = logs
        self.available = available

    def read(self, stream_key, *, season=None):
        self.read_args = (stream_key, season)
        return SimpleNamespace(
            available=self.available,
            legacy_fallback_allowed=False,
            status="active" if self.available else "missing",
            decoded=self.logs,
            payload=None,
        )


def _run(tmp_path, **kwargs):
    engine, logs, player_id = _season_database(tmp_path, **kwargs)
    publications = Publications(logs)
    inputs = backtest.load_inputs(engine, SEASON, publication_reader=publications)
    fetcher = backtest.ReportFetcher(
        tmp_path / "cache", delay_seconds=0, session=FakeCdn(SITE), sleep=lambda s: None
    )
    return backtest.evaluate(inputs, fetcher), publications, fetcher


def test_inputs_come_from_the_published_season_stream_not_the_legacy_table(tmp_path):
    engine, logs, _ = _season_database(tmp_path)
    publications = Publications(logs)

    inputs = backtest.load_inputs(engine, SEASON, publication_reader=publications)

    assert publications.read_args == ("player_game_logs", SEASON)
    assert len(inputs.events) == 13
    assert len(inputs.athletes) == 113
    assert len(inputs.logs) == len(logs)


def test_an_inactive_publication_stops_the_backtest(tmp_path):
    engine, logs, _ = _season_database(tmp_path)

    with pytest.raises(backtest.BacktestInputError, match="no active published"):
        backtest.load_inputs(engine, SEASON, publication_reader=Publications(logs, available=False))


@needs_pdftotext
def test_backtest_scores_each_check_overall_and_per_month(tmp_path):
    document, _, fetcher = _run(tmp_path)

    assert document["dates"] == 2
    assert list(document["by_month"]) == ["2025-12"]
    assert document["thresholds"]["coverage"] == {"value": 1.0, "required": 1.0, "passed": True}
    assert document["thresholds"]["parse_completeness"]["value"] == 1.0
    assert document["coverage_failures"] == []
    assert document["completeness_failures"] == []
    # 12-25 report found by stepping 05_15PM -> 05_00PM; 12-26 at 06_45PM.
    assert fetcher.session.requested[:2] == [
        "Injury-Report_2025-12-25_05_15PM.pdf",
        "Injury-Report_2025-12-25_05_00PM.pdf",
    ]
    assert "Injury-Report_2025-12-26_06_45PM.pdf" in fetcher.session.requested

    # Identity: 115 rows in the file, 2 names absent from the catalog.
    overall = document["overall"]
    assert (overall["identity_rows"], overall["identity_matched"]) == (115, 113)
    assert document["unmatched"] == [
        {"name": "Harrison Ingram", "team": "SAS", "rows": 1},
        {"name": "Taelon Peter", "team": "IND", "rows": 1},
    ]
    assert document["unmatched_rows"] == 2
    assert overall["identity_rate"] == round(113 / 115, 4)
    assert document["thresholds"]["identity"]["passed"] is False  # 98.3% < 99%


@needs_pdftotext
def test_out_players_who_played_are_listed_and_set_precision(tmp_path):
    document, _, _ = _run(tmp_path)

    out = document["overall"]["statuses"]["Out"]
    assert out["graded"] == 73  # 74 Out rows, Harrison Ingram unmatched
    assert out["played"] == 2
    assert out["precision"] == round(71 / 73, 4)
    assert document["thresholds"]["out_precision"] == {
        "value": round(71 / 73, 4),
        "required": 0.98,
        "passed": False,  # 97.3% < 98%
    }
    assert sorted(
        (item["name"], item["team"], item["minutes"], item["reason"])
        for item in document["out_players_who_played"]
    ) == [
        ("Dante Exum", "DAL", 31.5, "Injury/Illness - Right Knee; Surgery"),
        ("Jaylin Williams", "OKC", 31.5, "Injury/Illness - Right Heel; Bursitis"),
    ]


@needs_pdftotext
def test_play_rates_for_the_other_statuses(tmp_path):
    document, _, _ = _run(tmp_path)

    statuses = document["overall"]["statuses"]
    assert statuses["Available"] == {"graded": 13, "played": 2, "play_rate": round(2 / 13, 4)}
    assert statuses["Probable"]["graded"] == 8  # Taelon Peter unmatched
    assert statuses["Probable"]["played"] == 1  # Luka Doncic
    assert statuses["Doubtful"] == {"graded": 1, "played": 0, "play_rate": 0.0}
    assert statuses["Questionable"]["graded"] == 18


@needs_pdftotext
def test_a_clean_season_exits_zero_and_writes_the_json(tmp_path, capsys):
    engine, logs, _ = _season_database(tmp_path, played_out=(), omit=())
    output = tmp_path / "report.json"

    status = backtest.main(
        [
            "--season",
            SEASON,
            "--database-url",
            f"sqlite:///{tmp_path / 'season.sqlite3'}",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output",
            str(output),
            "--delay",
            "0",
        ],
        session=FakeCdn(SITE),
        publication_reader=Publications(logs),
    )

    assert status == 0
    written = json.loads(output.read_text())
    assert written["passed"] is True
    assert written["overall"]["identity_matched"] == 115
    assert written["overall"]["statuses"]["Out"]["precision"] == 1.0
    printed = capsys.readouterr().out
    assert "PASS identity: 1.0" in printed
    assert "2025-12" in printed


@needs_pdftotext
def test_a_failed_threshold_exits_one(tmp_path):
    engine, logs, _ = _season_database(tmp_path)

    status = backtest.main(
        [
            "--season",
            SEASON,
            "--database-url",
            f"sqlite:///{tmp_path / 'season.sqlite3'}",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--delay",
            "0",
        ],
        session=FakeCdn(SITE),
        publication_reader=Publications(logs),
    )

    assert status == 1


def test_an_unreadable_input_exits_two(tmp_path, capsys):
    engine, logs, _ = _season_database(tmp_path)

    status = backtest.main(
        [
            "--season",
            SEASON,
            "--database-url",
            f"sqlite:///{tmp_path / 'season.sqlite3'}",
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        session=FakeCdn(SITE),
        publication_reader=Publications(logs, available=False),
    )

    assert status == 2
    assert "no active published player_game_logs" in capsys.readouterr().err


def test_the_database_connection_rejects_writes(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    _season_database(tmp_path)
    engine = backtest.open_read_only_engine(f"sqlite:///{tmp_path / 'season.sqlite3'}")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM event_catalog")).scalar() == 13
        with pytest.raises(OperationalError):
            connection.execute(text("DELETE FROM event_catalog"))


def test_a_missing_report_is_a_coverage_failure_with_bounded_probes(tmp_path):
    engine, logs, _ = _season_database(tmp_path)
    inputs = backtest.load_inputs(engine, SEASON, publication_reader=Publications(logs))
    cdn = FakeCdn({})
    fetcher = backtest.ReportFetcher(tmp_path / "cache", delay_seconds=0, session=cdn, sleep=lambda s: None)

    document = backtest.evaluate(inputs, fetcher, max_probes=6, count_status_cells=lambda b: 0)

    assert document["passed"] is False
    assert document["thresholds"]["coverage"]["value"] == 0.0
    assert [item["date"] for item in document["coverage_failures"]] == [
        "2025-12-25",
        "2025-12-26",
    ]
    assert len(cdn.requested) == 12  # 6 per date, no more
    # Misses are cached: a rerun makes no network call.
    backtest.evaluate(inputs, fetcher, max_probes=6, count_status_cells=lambda b: 0)
    assert len(cdn.requested) == 12


@needs_pdftotext
def test_row_count_is_checked_against_an_independent_pdftotext_count(tmp_path):
    engine, logs, _ = _season_database(tmp_path)
    inputs = backtest.load_inputs(engine, SEASON, publication_reader=Publications(logs))
    fetcher = backtest.ReportFetcher(
        tmp_path / "cache", delay_seconds=0, session=FakeCdn(SITE), sleep=lambda s: None
    )

    document = backtest.evaluate(inputs, fetcher, count_status_cells=lambda content: 116)

    assert document["passed"] is False
    assert document["thresholds"]["parse_completeness"]["value"] == 0.0
    assert "parsed 115 rows, pdftotext counts 116" in document["completeness_failures"][0]["problems"]


@needs_pdftotext
def test_pdftotext_counts_the_status_cells_of_a_recorded_report():
    assert backtest.pdftotext_status_cells(LATE) == 115
    assert (
        backtest.pdftotext_status_cells((FIXTURES / "Injury-Report_2025-10-21_05PM.pdf").read_bytes()) == 54
    )


def _evaluate_after(tmp_path, statement, **options):
    from sqlalchemy import text

    engine, logs, _ = _season_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text(statement))
    inputs = backtest.load_inputs(engine, SEASON, publication_reader=Publications(logs))
    fetcher = backtest.ReportFetcher(
        tmp_path / "cache", delay_seconds=0, session=FakeCdn(SITE), sleep=lambda s: None
    )
    return backtest.evaluate(inputs, fetcher, **options)


@needs_pdftotext
def test_athletes_inactive_in_the_end_of_season_catalog_still_reconcile(tmp_path):
    document = _evaluate_after(tmp_path, "UPDATE athlete_catalog SET is_active_for_season = 0")

    assert document["overall"]["identity_matched"] == 113
    assert document["unmatched_rows"] == 2


def _delete_game(matchup):
    return (
        f"DELETE FROM event_catalog WHERE home_team_tricode = '{matchup[-3:]}' "
        f"AND away_team_tricode = '{matchup[:3]}'"
    )


@needs_pdftotext
@pytest.mark.parametrize(
    ("matchup", "day", "rows"),
    [("DAL@GSW", date(2025, 12, 25), 13), ("PHX@NOP", date(2025, 12, 26), 5)],
)
def test_allowed_games_missing_from_the_event_catalog_are_listed_not_graded(tmp_path, matchup, day, rows):
    # Stands in for the Cup final or a postponed game the catalog never held.
    document = _evaluate_after(tmp_path, _delete_game(matchup), allowed_games={(day, matchup)})

    assert document["thresholds"]["parse_completeness"]["value"] == 1.0
    assert document["completeness_failures"] == []
    assert document["games_not_in_catalog"] == [{"date": day.isoformat(), "matchup": matchup, "rows": rows}]
    assert document["overall"]["identity_rows"] == 115  # still scored for identity
    graded = sum(item["graded"] for item in document["overall"]["statuses"].values())
    assert graded == 113 - rows  # but not graded (two names are not in the catalog)


@needs_pdftotext
def test_a_game_missing_from_the_catalog_and_not_allowed_fails_completeness(tmp_path):
    document = _evaluate_after(tmp_path, _delete_game("DAL@GSW"), allowed_games=())

    assert document["thresholds"]["parse_completeness"]["value"] == 0.0
    problems = document["completeness_failures"][0]["problems"]
    assert len(problems) == 13
    assert problems[0] == (
        "2025-12-25 DAL@GSW Christie, Max: game is not in the Event Catalog " "or the allowed exceptions"
    )


def test_the_known_2025_26_exceptions_are_the_cup_final_and_three_postponements():
    assert sorted(backtest.KNOWN_GAMES_NOT_IN_CATALOG) == [
        (date(2025, 12, 16), "SAS@NYK"),
        (date(2026, 1, 8), "MIA@CHI"),
        (date(2026, 1, 24), "GSW@MIN"),
        (date(2026, 1, 25), "DAL@MIL"),
    ]


@needs_pdftotext
def test_allow_game_flag_admits_a_missing_game_and_a_malformed_one_is_a_usage_error(
    tmp_path,
):
    engine, logs, _ = _season_database(tmp_path, played_out=(), omit=())
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text(_delete_game("DAL@GSW")))
    argv = [
        "--season",
        SEASON,
        "--database-url",
        f"sqlite:///{tmp_path / 'season.sqlite3'}",
        "--cache-dir",
        str(tmp_path / "cache"),
        "--delay",
        "0",
    ]

    def run(*extra):
        return backtest.main(
            [*argv, *extra],
            session=FakeCdn(SITE),
            publication_reader=Publications(logs),
        )

    assert run() == 1
    assert run("--allow-game", "2025-12-25:DAL@GSW") == 0
    with pytest.raises(SystemExit) as raised:
        run("--allow-game", "DAL@GSW")
    assert raised.value.code == 2


HOURLY = (FIXTURES / "Injury-Report_2025-10-21_05PM.pdf").read_bytes()


def test_a_report_stamped_after_the_cutoff_is_not_used(tmp_path):
    # The 05PM file's header says 05:30 PM; a 5:15 PM cutoff must skip it.
    def search(tip):
        fetcher = backtest.ReportFetcher(
            tmp_path / "cache",
            delay_seconds=0,
            session=FakeCdn({"Injury-Report_2025-10-21_05PM.pdf": HOURLY}),
            sleep=lambda s: None,
        )
        return backtest.find_last_report(fetcher, tip, max_probes=6)

    assert search(datetime(2025, 10, 21, 21, 15, tzinfo=timezone.utc)) is None
    name, _, report = search(datetime(2025, 10, 21, 21, 30, tzinfo=timezone.utc))
    assert name == "Injury-Report_2025-10-21_05PM.pdf"
    assert report.report_time.hour == 17 and report.report_time.minute == 30


@needs_pdftotext
@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"game_date": date(2025, 12, 24)}, "2025-12-24 DAL@GSW"),  # wrong date, in season
        ({"matchup": "DAL@LAL"}, "2025-12-25 DAL@LAL"),  # invented matchup, real date
    ],
)
def test_an_invalid_row_fails_completeness_and_still_counts_toward_identity(
    tmp_path, monkeypatch, changes, label
):
    import dataclasses

    real_parse = backtest.parse_injury_report

    def corrupt(content):
        report = real_parse(content)
        rows = tuple(
            dataclasses.replace(row, player_name="Nobody, Unknown", **changes)
            if row.player_name == "Christie, Max"
            else row
            for row in report.rows
        )
        return dataclasses.replace(report, rows=rows)

    monkeypatch.setattr(backtest, "parse_injury_report", corrupt)
    engine, logs, _ = _season_database(tmp_path, omit=())
    inputs = backtest.load_inputs(engine, SEASON, publication_reader=Publications(logs))
    fetcher = backtest.ReportFetcher(
        tmp_path / "cache", delay_seconds=0, session=FakeCdn(SITE), sleep=lambda s: None
    )

    document = backtest.evaluate(inputs, fetcher)

    assert document["thresholds"]["parse_completeness"]["value"] == 0.0
    assert document["passed"] is False
    for item in document["completeness_failures"]:
        assert (
            f"{label} Nobody, Unknown: game is not in the Event Catalog " "or the allowed exceptions"
        ) in item["problems"]
    # The row is scored for identity once (both stand-in reports repeat it),
    # is unmatched, and is never graded.
    assert document["unmatched"] == [{"name": "Unknown Nobody", "team": "DAL", "rows": 1}]
    assert document["overall"]["identity_rows"] == 115
    assert document["overall"]["identity_matched"] == 114
    assert document["overall"]["statuses"]["Available"]["graded"] == 12
