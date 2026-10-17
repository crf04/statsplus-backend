"""The 2025-26 injury backfill stores each game's last report before its tip.

Reports are the recorded PDFs under ``tests/fixtures/injury_report`` served by
a fake CDN; storage is a real migrated SQLite database.  Expected values are
read off the recorded files.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import create_engine, func, select

from app.config.settings import ProviderSettings, RuntimeSettings
from app.migrations import run_migrations
from app.models.injury_snapshot import InjurySourceSnapshot
from app.providers.nba_injury_report import (
    NBAInjuryReportProvider,
    ParsedInjuryReport,
    ReportFileCache,
    UnsubmittedTeam,
    report_time_from_name,
)
from app.services.injury_snapshot_repository import (
    InjurySnapshotRepository,
    InjurySnapshotScope,
)
from app.services.matchup_injuries import MatchupInjuryService
from scripts import injury_backfill
from scripts.injury_backfill import games_to_backfill, run_backfill

FIXTURES = Path(__file__).parent / "fixtures" / "injury_report"
BASE = "https://ak-static.cms.nba.com/referee/injury/"
HOURLY = "Injury-Report_2025-10-21_05PM.pdf"  # header 5:30 PM ET
EARLY = "Injury-Report_2025-12-25_01_30PM.pdf"  # header 1:30 PM ET
LATE = "Injury-Report_2025-12-25_05_00PM.pdf"  # header 5:00 PM ET
SEASON = "2025-26"
RUN_AT = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)

SAS, OKC, HOU, LAL, GSW, DAL = 1610612759, 1610612760, 1610612745, 1610612747, 1610612744, 1610612742


def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def _pdf(name):
    return (FIXTURES / name).read_bytes()


class Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


class Site:
    """A fake CDN: named files exist, everything else is 403."""

    def __init__(self, files=None, *, fail=None):
        self.files = files or {}
        self.fail = fail  # callable(name) -> exception to raise, or None
        self.headers = {}
        self.calls = []

    def get(self, url, *, timeout, stream=False):
        name = url.removeprefix(BASE)
        self.calls.append(name)
        if self.fail and (error := self.fail(name)):
            raise error
        if name in self.files:
            return Response(200, self.files[name])
        return Response(403)


class Catalog:
    def get_catalog(self, season, *, active_only=False):
        assert (season, active_only) == (SEASON, True)
        return [
            {"player_id": 9001, "display_name": "Harrison Ingram", "team_abbreviation": "SAS"},
            {"player_id": 9002, "display_name": "Jonathan Kuminga", "team_abbreviation": "GSW"},
        ]


def _event(game_id, scheduled_at, away, home, **extra):
    ids = {"SAS": SAS, "OKC": OKC, "HOU": HOU, "LAL": LAL, "GSW": GSW, "DAL": DAL}
    return {
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "away_team_id": ids[away],
        "away_team_tricode": away,
        "home_team_id": ids[home],
        "home_team_tricode": home,
        **extra,
    }


# Christmas 2025 as the recorded 1:30 PM and 5:00 PM reports print it.
SAS_OKC = _event("0022500400", "2025-12-25T19:30:00+00:00", "SAS", "OKC")  # 2:30 PM ET
DAL_GSW = _event("0022500401", "2025-12-25T22:00:00+00:00", "DAL", "GSW")  # 5:00 PM ET
HOU_LAL = _event("0022500402", "2025-12-26T01:00:00+00:00", "HOU", "LAL")  # 8:00 PM ET
# 2025-10-21, hourly file names.
HOU_OKC = _event("0022500001", "2025-10-21T23:30:00+00:00", "HOU", "OKC")  # 7:30 PM ET
GSW_LAL = _event("0022500002", "2025-10-22T02:00:00+00:00", "GSW", "LAL")  # 10:00 PM ET


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.sqlite3'}")
    run_migrations(engine)
    yield engine
    engine.dispose()


def _stack(engine, site, **provider_settings):
    pauses = []
    provider = NBAInjuryReportProvider(
        session=site,
        settings=RuntimeSettings(
            environment="testing", providers=ProviderSettings(**provider_settings)
        ),
        sleep=pauses.append,
    )
    repository = InjurySnapshotRepository(engine)
    injuries = MatchupInjuryService(
        provider=provider,
        snapshot_repository=repository,
        athlete_catalog=Catalog(),
        enabled=True,
    )
    return SimpleNamespace(
        provider=provider, repository=repository, injuries=injuries, pauses=pauses
    )


def _run(stack, events, **kwargs):
    kwargs.setdefault("dry_run", False)
    return run_backfill(
        season=SEASON,
        events=events,
        provider=stack.provider,
        injuries=stack.injuries,
        now=RUN_AT,
        log=io.StringIO(),
        **kwargs,
    )


def _stored(stack, event):
    return stack.repository.get(InjurySnapshotScope(SEASON, event["nba_game_id"]))


def _source_count(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(InjurySourceSnapshot.__table__)
        ).scalar_one()


# -- choosing the report -----------------------------------------------------


def _fake_parse(monkeypatch, headers):
    """Serve files whose header time is set by name, whatever the bytes say."""

    import app.providers.nba_injury_report as module

    def parse(content):
        name = content.decode().removeprefix("%PDF ")
        return ParsedInjuryReport(
            title=name,
            rows=(),
            unsubmitted=(UnsubmittedTeam(date(2026, 1, 1), "SAS@OKC", "Spurs", "SAS"),),
            unparsed=(),
            report_time=headers[name],
        )

    monkeypatch.setattr(module, "parse_injury_report", parse)


def _fake_site(*names):
    return Site({name: f"%PDF {name}".encode() for name in names})


def _as_of(provider, moment, *, max_requests=60):
    return provider.report_as_of(
        moment, cache=ReportFileCache(), max_requests=max_requests, probe_delay_seconds=0.5
    )


def _provider(site):
    return NBAInjuryReportProvider(
        session=site,
        settings=RuntimeSettings(environment="testing", providers=ProviderSettings()),
        sleep=lambda seconds: None,
    )


def test_the_newest_header_at_or_before_the_tip_wins_across_both_name_formats(monkeypatch):
    # 7:00 PM tip.  Hourly name 06PM (header 6:30), quarter-hour name 06_45PM
    # (header 6:45) and 07_00PM (header exactly 7:00) all exist, and so does an
    # older hourly 05PM.  The 7:00 PM header is at the tip, so it wins.
    def et(hour, minute):
        return report_time_from_name(f"Injury-Report_2025-12-02_{hour:02d}_{minute:02d}PM.pdf")

    headers = {
        "Injury-Report_2025-12-02_05PM.pdf": et(5, 30),
        "Injury-Report_2025-12-02_06PM.pdf": et(6, 30),
        "Injury-Report_2025-12-02_06_45PM.pdf": et(6, 45),
        "Injury-Report_2025-12-02_07_00PM.pdf": et(7, 0),
    }
    _fake_parse(monkeypatch, headers)
    provider = _provider(_fake_site(*headers))
    tip = _utc(2025, 12, 3, 0, 0)  # 7:00 PM EST

    assert _as_of(provider, tip).url == BASE + "Injury-Report_2025-12-02_07_00PM.pdf"
    assert _as_of(provider, _utc(2025, 12, 2, 23, 59)).url == BASE + "Injury-Report_2025-12-02_06_45PM.pdf"
    assert _as_of(provider, _utc(2025, 12, 2, 23, 40)).url == BASE + "Injury-Report_2025-12-02_06PM.pdf"
    assert _as_of(provider, _utc(2025, 12, 2, 22, 40)).url == BASE + "Injury-Report_2025-12-02_05PM.pdf"


def test_a_report_stamped_after_the_tip_is_skipped_for_an_older_one(monkeypatch):
    # The 6 PM file is named before the 6:20 PM tip but its header says 6:30.
    headers = {
        "Injury-Report_2025-12-02_05PM.pdf": report_time_from_name("Injury-Report_2025-12-02_05_30PM.pdf"),
        "Injury-Report_2025-12-02_06PM.pdf": report_time_from_name("Injury-Report_2025-12-02_06_30PM.pdf"),
    }
    _fake_parse(monkeypatch, headers)
    site = _fake_site(*headers)

    found = _as_of(_provider(site), _utc(2025, 12, 2, 23, 20))  # 6:20 PM EST

    assert found.url == BASE + "Injury-Report_2025-12-02_05PM.pdf"
    assert "Injury-Report_2025-12-02_06PM.pdf" in site.calls  # fetched, found too late


def test_the_walk_crosses_the_spring_forward_hour_without_a_gap_or_repeat(monkeypatch):
    # 2026-03-08: 2:00 AM EST does not exist.  The only report is Saturday's
    # 11:30 PM one; a 1:00 PM EDT game on the 8th walks back 14 hours to it.
    name = "Injury-Report_2026-03-07_11_30PM.pdf"
    _fake_parse(monkeypatch, {name: report_time_from_name(name)})
    site = _fake_site(name)

    found = _as_of(_provider(site), _utc(2026, 3, 8, 17, 0), max_requests=100)

    assert found.url == BASE + name
    assert len(site.calls) == len(set(site.calls))
    assert "Injury-Report_2026-03-08_03_00AM.pdf" in site.calls
    assert "Injury-Report_2026-03-08_01_45AM.pdf" in site.calls  # the slot before 3:00 EDT
    assert not any(n.startswith("Injury-Report_2026-03-08_02") for n in site.calls)


def test_the_walk_crosses_the_fall_back_hour_probing_each_name_once(monkeypatch):
    # 2025-11-02: 1:00-1:59 AM happens twice.  Each wall-clock name is requested once.
    name = "Injury-Report_2025-11-01_11_30PM.pdf"
    _fake_parse(monkeypatch, {name: report_time_from_name(name)})
    site = _fake_site(name)

    found = _as_of(_provider(site), _utc(2025, 11, 2, 18, 0), max_requests=100)  # 1:00 PM EST

    assert found.url == BASE + name
    assert len(site.calls) == len(set(site.calls))
    assert site.calls.count("Injury-Report_2025-11-02_01_30AM.pdf") == 1


def test_no_report_inside_the_walk_is_none(monkeypatch):
    found = _as_of(_provider(Site()), _utc(2025, 12, 3, 0, 0), max_requests=7)

    assert found is None


# -- the run -------------------------------------------------------------------


def test_each_game_gets_the_last_report_before_its_tip_stored_like_a_live_refresh(engine):
    site = Site({EARLY: _pdf(EARLY), LATE: _pdf(LATE)})
    stack = _stack(engine, site)

    summary = _run(stack, [SAS_OKC, DAL_GSW, HOU_LAL])

    assert summary.to_document() == {
        "dry_run": False,
        "games_in_scope": 3,
        "games_stored": 3,
        "games_skipped_already_stored": 0,
        "games_no_report_found": 0,
        "games_parse_failed": 0,
        "games_network_failed": 0,
        "games_not_attempted": 0,
        "report_files_downloaded": 2,
        "requests_made": summary.requests_made,
        "parse_failures": [],
        "stopped_after_network_failures": False,
        "no_report_found_share": 0.0,
        "parse_failed_share": 0.0,
    }
    spurs = _stored(stack, SAS_OKC)  # 2:30 PM tip: the 1:30 PM report, not the 5:00 PM one
    assert len(spurs.normalized_entries) == 8
    first = spurs.normalized_entries[0]
    assert (first["source_player_name"], first["canonical_player_id"], first["canonical_status"]) == (
        "Harrison Ingram", 9001, "Out",
    )
    assert first["report_time"] == "2025-12-25T13:30:00-05:00"
    assert first["source_url"] == BASE + EARLY
    assert first["game_id"] == "0022500400"
    assert spurs.retrieved_at == RUN_AT
    # 5:00 PM tip: a header exactly at tip counts as before it.
    mavericks = _stored(stack, DAL_GSW)
    assert len(mavericks.normalized_entries) == 13
    assert {entry["report_time"] for entry in mavericks.normalized_entries} == {
        "2025-12-25T17:00:00-05:00"
    }
    assert len(_stored(stack, HOU_LAL).normalized_entries) == 8
    assert _source_count(engine) == 2  # one source per distinct report, not per game


def test_a_report_stamped_after_the_tip_leaves_the_game_without_one(engine):
    # The 5 PM file's header says 5:30 PM, so a 5:10 PM tip has no report.
    site = Site({HOURLY: _pdf(HOURLY)})
    early_tip = _event("0022500003", "2025-10-21T21:10:00+00:00", "HOU", "OKC")
    stack = _stack(engine, site)

    summary = _run(stack, [early_tip])

    assert (summary.games_stored, summary.games_no_report_found) == (0, 1)
    assert _stored(stack, early_tip) is None
    assert _source_count(engine) == 0


def test_hourly_era_names_are_found_from_a_late_tip(engine):
    site = Site({HOURLY: _pdf(HOURLY)})
    stack = _stack(engine, site)

    summary = _run(stack, [HOU_OKC, GSW_LAL])

    assert summary.games_stored == 2
    stored = _stored(stack, GSW_LAL)
    assert len(stored.normalized_entries) == 8
    assert {entry["source_url"] for entry in stored.normalized_entries} == {BASE + HOURLY}
    assert len(_stored(stack, HOU_OKC).normalized_entries) == 9


def test_a_game_that_already_has_a_snapshot_is_skipped_and_keeps_it(engine):
    site = Site({EARLY: _pdf(EARLY), LATE: _pdf(LATE)})
    stack = _stack(engine, site)
    live_observed_at = _utc(2025, 12, 25, 21, 55)
    stack.repository.publish(
        InjurySnapshotScope(SEASON, SAS_OKC["nba_game_id"]),
        source="nba_official",
        raw_payload=[],
        source_entries=[],
        normalized_entries=[{"entry_id": "live"}],
        retrieved_at=live_observed_at,
        unresolved_team_entry_count=0,
    )

    summary = _run(stack, [SAS_OKC, DAL_GSW])

    assert (summary.games_skipped_already_stored, summary.games_stored) == (1, 1)
    kept = _stored(stack, SAS_OKC)
    assert kept.normalized_entries == ({"entry_id": "live"},)
    assert kept.retrieved_at == live_observed_at
    assert EARLY not in site.calls  # no report was fetched for the skipped game


def test_a_second_run_stores_nothing_and_fetches_nothing(engine):
    site = Site({EARLY: _pdf(EARLY), LATE: _pdf(LATE)})
    stack = _stack(engine, site)
    events = [SAS_OKC, DAL_GSW, HOU_LAL]
    _run(stack, events)
    sources = _source_count(engine)
    calls = len(site.calls)
    before = [_stored(stack, event) for event in events]

    again = _run(stack, events)

    assert (again.games_stored, again.games_skipped_already_stored) == (0, 3)
    assert again.to_document()["report_files_downloaded"] == 0
    assert len(site.calls) == calls
    assert _source_count(engine) == sources
    assert [_stored(stack, event) for event in events] == before


def test_each_report_file_is_requested_once_per_run_even_across_games_and_slots(engine):
    site = Site({HOURLY: _pdf(HOURLY)})
    same_tip = _event("0022500004", "2025-10-22T02:00:00+00:00", "DAL", "GSW")  # with GSW_LAL
    stack = _stack(engine, site)

    summary = _run(stack, [HOU_OKC, GSW_LAL, same_tip])

    assert summary.games_stored == 3
    assert len(site.calls) == len(set(site.calls))
    assert site.calls.count(HOURLY) == 1
    assert summary.report_files_downloaded == 1
    assert summary.requests_made == len(site.calls)


def test_requests_are_paced_at_least_half_a_second_apart(engine):
    stack = _stack(engine, Site({LATE: _pdf(LATE)}), injury_report_probe_delay_seconds=0.1)

    summary = _run(stack, [HOU_LAL])

    assert summary.requests_made > 1
    assert stack.pauses == [0.5] * (summary.requests_made - 1)


def test_an_unreadable_layout_is_counted_with_its_url_and_the_run_goes_on(engine):
    bad = {EARLY: b"%PDF-1.4 a layout nobody has seen", LATE: _pdf(LATE)}
    stack = _stack(engine, Site(bad))

    summary = _run(stack, [SAS_OKC, HOU_LAL, DAL_GSW])

    assert summary.games_parse_failed == 1
    assert summary.parse_failures == [BASE + EARLY]
    assert summary.games_stored == 2
    assert _stored(stack, SAS_OKC) is None  # never stored, and no older report stands in
    assert len(_stored(stack, HOU_LAL).normalized_entries) == 8
    assert _source_count(engine) == 1


def test_a_file_the_parser_rejects_is_not_requested_again_for_the_next_game(engine):
    site = Site({EARLY: b"%PDF-1.4 a layout nobody has seen"})
    other = _event("0022500405", "2025-12-25T19:45:00+00:00", "HOU", "LAL")  # a later tip
    stack = _stack(engine, site)

    summary = _run(stack, [SAS_OKC, other])

    assert summary.games_parse_failed == 2
    assert summary.parse_failures == [BASE + EARLY]  # one file, listed once
    assert site.calls.count(EARLY) == 1


def test_dry_run_computes_everything_and_writes_nothing(engine):
    site = Site({EARLY: _pdf(EARLY), LATE: _pdf(LATE)})
    stack = _stack(engine, site)

    summary = _run(stack, [SAS_OKC, DAL_GSW, HOU_LAL], dry_run=True)

    assert (summary.dry_run, summary.games_stored, summary.report_files_downloaded) == (True, 3, 2)
    assert _source_count(engine) == 0
    assert all(_stored(stack, event) is None for event in (SAS_OKC, DAL_GSW, HOU_LAL))


def test_three_network_failures_in_a_row_stop_the_run_but_keep_what_was_found(engine):
    # The 2:30 PM game's walk takes 6 requests and finds the 1:30 PM report;
    # the CDN then goes away for every later request.
    site = Site({EARLY: _pdf(EARLY)})
    site.fail = lambda name: requests.ConnectionError("down") if len(site.calls) > 6 else None
    stack = _stack(engine, site)
    later = [
        _event(f"002250050{i}", f"2025-12-25T{20 + i}:00:00+00:00", "HOU", "LAL") for i in range(4)
    ]

    summary = _run(stack, [SAS_OKC, *later])

    assert summary.stopped_after_network_failures is True
    assert summary.games_stored == 1
    assert summary.games_network_failed == 3
    assert summary.games_not_attempted == 1
    assert len(site.calls) == 6 + 3
    assert len(_stored(stack, SAS_OKC).normalized_entries) == 8


def test_network_failures_separated_by_an_answer_do_not_stop_the_run(engine):
    # Requests 1, 2 and 4 die; request 3 is answered (403: nothing published).
    # That is three failures but never three in a row.
    site = Site({LATE: _pdf(LATE)})
    site.fail = lambda name: requests.Timeout("slow") if len(site.calls) in (1, 2, 4) else None
    stack = _stack(engine, site)
    games = [
        DAL_GSW,
        _event("0022500403", "2025-12-25T22:30:00+00:00", "DAL", "GSW"),
        HOU_LAL,
        _event("0022500404", "2025-12-26T01:30:00+00:00", "HOU", "LAL"),
    ]

    summary = _run(stack, games)

    assert summary.stopped_after_network_failures is False
    assert summary.games_network_failed == 3  # left for a rerun
    assert summary.games_stored == 1
    assert _stored(stack, games[3]) is not None


# -- which games ---------------------------------------------------------------


def test_games_in_scope_have_tipped_were_played_and_sit_in_the_eastern_date_range():
    def event(game_id, at, **extra):
        return _event(game_id, at, "SAS", "OKC", **extra)

    events = [
        event("0022500010", "2025-12-25T19:30:00+00:00"),
        event("0022500011", "2025-12-25T19:30:00+00:00", status_text="Postponed"),
        event("0022500012", "2026-10-18T00:00:00+00:00"),  # tips after the run
        event("0032500001", "2026-02-15T01:00:00+00:00"),  # All-Star
        event("0012500001", "2025-10-05T23:00:00+00:00"),  # preseason
        event("0042500101", "2026-04-26T00:30:00+00:00"),  # playoffs, 8:30 PM ET on the 25th
        event("0052500001", "2026-04-15T23:30:00+00:00"),  # play-in
        event("0022500013", "2025-12-26T04:30:00+00:00"),  # 11:30 PM ET on the 25th
    ]

    assert [e["nba_game_id"] for e in games_to_backfill(events, now=RUN_AT)] == [
        "0022500010", "0022500013", "0052500001", "0042500101",
    ]
    christmas = games_to_backfill(
        events, now=RUN_AT, date_from=date(2025, 12, 25), date_to=date(2025, 12, 25)
    )
    assert [e["nba_game_id"] for e in christmas] == ["0022500010", "0022500013"]
    playoffs = games_to_backfill(events, now=RUN_AT, date_from=date(2026, 4, 25))
    assert [e["nba_game_id"] for e in playoffs] == ["0042500101"]


# -- the command ---------------------------------------------------------------


def test_the_command_prints_one_json_summary_and_a_dry_run_writes_nothing(
    engine, monkeypatch, capsys
):
    site = Site({EARLY: _pdf(EARLY), LATE: _pdf(LATE)})
    stack = _stack(engine, site)
    import app.dependencies as dependencies

    monkeypatch.setattr(
        dependencies,
        "build_dependencies",
        lambda settings: SimpleNamespace(
            injury_refresh_service=SimpleNamespace(injuries=stack.injuries),
            event_catalog_service=SimpleNamespace(
                get_events=lambda season: [HOU_OKC, SAS_OKC, DAL_GSW, HOU_LAL]
            ),
            engine=SimpleNamespace(dispose=lambda: None),
        ),
    )
    code = injury_backfill.main(
        ["--season", SEASON, "--dry-run", "--database-url", "postgresql://u:p@db.example/x",
         "--from", "2025-12-25", "--to", "2025-12-25"],
        clock=lambda: RUN_AT,
    )

    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert (printed["dry_run"], printed["games_in_scope"], printed["games_stored"]) == (True, 3, 3)  # the October game is outside --from/--to
    assert _source_count(engine) == 0


def test_a_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as raised:
        injury_backfill.main(["--season", SEASON])

    assert raised.value.code == 2
