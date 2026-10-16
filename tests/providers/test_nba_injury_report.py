"""Offline contract tests for the NBA official injury-report adapter.

The PDFs under ``tests/fixtures/injury_report`` are real reports downloaded
from ``ak-static.cms.nba.com`` (hourly file-name era and quarter-hour era).
Expected values below are read off those files, not computed by the parser.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

from app.config.settings import ProviderSettings, RuntimeSettings
from app.errors import ProviderUnavailableError
from app.providers.nba_injury_report import (
    NBAInjuryReportProvider,
    candidate_report_names,
    entries_from_report,
    parse_injury_report,
    report_file_name,
    report_time_from_name,
)
from app.utils import telemetry


FIXTURES = Path(__file__).parents[1] / "fixtures" / "injury_report"
HOURLY = "Injury-Report_2025-10-21_05PM.pdf"
QUARTER_EARLY = "Injury-Report_2025-12-25_01_30PM.pdf"
QUARTER_LATE = "Injury-Report_2025-12-25_05_00PM.pdf"
STACKED = "Injury-Report_2026-01-12_07_00PM.pdf"
BASE = "https://ak-static.cms.nba.com/referee/injury/"


def _report(name):
    return parse_injury_report((FIXTURES / name).read_bytes())


def _row(report, player):
    matches = [row for row in report.rows if row.player_name == player]
    assert len(matches) == 1, player
    return matches[0]


# -- parsing -----------------------------------------------------------------


def test_hourly_era_report_row_counts_and_every_status():
    report = _report(HOURLY)

    assert report.title == "Injury Report: 10/21/25 05:30 PM"
    assert len(report.rows) == 54
    assert Counter(row.status for row in report.rows) == {
        "Out": 38,
        "Questionable": 7,
        "Probable": 4,
        "Doubtful": 3,
        "Available": 2,
    }
    assert report.unparsed == ()


def test_wrapped_reason_with_status_on_the_middle_line_is_one_row():
    sorber = _row(_report(HOURLY), "Sorber, Thomas")

    assert sorber.reason == "Injury/Illness - Right ACL; Surgical recovery"
    assert sorber.status == "Out"
    assert sorber.team_tricode == "OKC"


def test_wrapped_reason_with_status_on_the_first_line_is_one_row():
    kuminga = _row(_report(HOURLY), "Kuminga, Jonathan")

    assert kuminga.reason == "Injury/Illness - Right Ankle; Inflammation"
    assert kuminga.status == "Probable"


def test_team_game_and_date_carry_down_blank_rows_and_pages():
    report = _report(HOURLY)

    # Printed once on the first row of the Golden State block (page 1) ...
    toohey = _row(report, "Toohey, Alex")
    assert (toohey.game_date, toohey.game_time_et, toohey.matchup) == (
        date(2025, 10, 21),
        "10:00 (ET)",
        "GSW@LAL",
    )
    assert toohey.team_name == "Golden State Warriors"
    # ... the next team of that game prints its own name only (page 2 start).
    lebron = _row(report, "James, LeBron")
    assert lebron.team_name == "Los Angeles Lakers"
    assert lebron.matchup == "GSW@LAL"
    assert lebron.reason == "Injury/Illness - Right Sciatica; -"


def test_suffix_names_are_rewritten_to_display_order():
    report = _report(HOURLY)

    assert _row(report, "Butler III, Jimmy").display_name == "Jimmy Butler III"
    late = _report(QUARTER_LATE)
    assert _row(late, "Nance Jr., Larry").display_name == "Larry Nance Jr."
    assert _row(late, "Jones Garcia, David").display_name == "David Jones Garcia"
    assert _row(late, "Finney-Smith, Dorian").display_name == "Dorian Finney-Smith"
    assert _row(late, "Jaquez Jr., Jaime").display_name == "Jaime Jaquez Jr."


def test_late_season_report_counts_and_available_rows():
    report = _report(QUARTER_LATE)

    assert report.title == "Injury Report: 12/25/25 05:00 PM"
    assert len(report.rows) == 115
    assert Counter(row.status for row in report.rows) == {
        "Out": 74,
        "Questionable": 18,
        "Available": 13,
        "Probable": 9,
        "Doubtful": 1,
    }
    christie = _row(report, "Christie, Max")
    assert (christie.status, christie.reason, christie.team_tricode) == (
        "Available",
        "Injury/Illness - Illness; Illness",
        "DAL",
    )
    assert report.unparsed == ()


def test_two_injuries_stacked_in_one_row_stay_one_row():
    report = _report(STACKED)

    assert len(report.rows) == 123
    assert Counter(row.status for row in report.rows) == {
        "Out": 98,
        "Available": 15,
        "Questionable": 6,
        "Probable": 4,
    }
    embiid = _row(report, "Embiid, Joel")
    assert (embiid.status, embiid.team_tricode) == ("Available", "PHI")
    assert embiid.reason == (
        "Injury/Illness - Left Knee; Injury Management; Left Adductor Soreness"
    )
    assert _row(report, "Rozier, Terry").reason == "Not With Team"
    assert report.unparsed == ()


def test_reason_split_across_a_page_break_and_around_the_status_line_is_joined():
    report = _report(QUARTER_LATE)

    # Status line at the foot of page 1, last reason line on top of page 2.
    assert _row(report, "Washington, P.J.").reason == (
        "Injury/Illness - Right Midfoot; Soreness"
    )
    # Reason lines both above and below the line that carries player and status.
    assert _row(report, "Curry, Seth").reason == (
        "Injury/Illness - Left Glute; Injury Management"
    )
    assert _row(report, "Horford, Al").reason == (
        "Injury/Illness - Right Sciatic Nerve; Irritation"
    )


def test_not_yet_submitted_teams_are_listed_apart_from_player_rows():
    report = _report(QUARTER_LATE)

    assert [
        (team.game_date, team.matchup, team.team_tricode)
        for team in report.unsubmitted
    ] == [
        (date(2025, 12, 26), "MIL@MEM", "MEM"),
        (date(2025, 12, 26), "PHX@NOP", "PHX"),
        (date(2025, 12, 26), "DET@UTA", "DET"),
        (date(2025, 12, 26), "DET@UTA", "UTA"),
        (date(2025, 12, 26), "LAC@POR", "LAC"),
        (date(2025, 12, 26), "LAC@POR", "POR"),
    ]
    assert not any(row.reason == "NOT YET SUBMITTED" for row in report.rows)


def test_report_where_most_teams_have_not_submitted():
    report = _report(QUARTER_EARLY)

    assert len(report.rows) == 43
    assert len(report.unsubmitted) == 18
    assert {row.matchup for row in report.rows} == {
        "SAS@OKC",
        "DAL@GSW",
        "HOU@LAL",
        "MIN@DEN",
        "CLE@NYK",
    }


def test_garbage_bytes_are_a_response_error_not_an_empty_report():
    with pytest.raises(telemetry.ProviderResponseError):
        parse_injury_report(b"%PDF-1.4 truncated")


TITLE = [
    (62.0, 289.0, "Injury"), (62.0, 355.0, "Report:"), (62.0, 438.0, "12/25/25"),
    (62.0, 539.0, "05:00"), (62.0, 602.0, "PM"),
]


def test_reason_line_at_the_foot_of_a_page_prefixes_the_next_pages_row(monkeypatch):
    """Page 1 ends with a reason-only line; the player/status line opens page 2."""

    header = [
        (115.2, 23.0, "Game"), (115.2, 53.0, "Date"), (115.2, 119.0, "Game"),
        (115.2, 149.0, "Time"), (115.2, 200.0, "Matchup"), (115.2, 264.0, "Team"),
        (115.2, 425.0, "Player"), (115.2, 457.0, "Name"), (115.2, 585.0, "Current"),
        (115.2, 624.0, "Status"), (115.2, 666.0, "Reason"),
    ]
    page_one = [
        *TITLE, *header,
        (136.3, 24.0, "12/25/2025"), (136.3, 121.0, "02:30"), (136.3, 201.0, "SAS@OKC"),
        (136.3, 265.0, "San"), (136.3, 282.0, "Antonio"), (136.3, 317.0, "Spurs"),
        (136.3, 426.0, "Ingram,"), (136.3, 459.0, "Harrison"), (136.3, 587.0, "Out"),
        (136.3, 667.0, "Knee;"),
        (500.0, 667.0, "Injury"), (500.0, 700.0, "Management;"),  # foot of page 1
    ]
    page_two = [
        *TITLE,
        (87.9, 426.0, "Jones"), (87.9, 451.0, "Garcia,"), (87.9, 481.0, "David"),
        (87.9, 587.0, "Questionable"), (87.9, 667.0, "Left"), (87.9, 690.0, "Ankle"),
    ]
    pages = iter([page_one, page_two])

    class FakeReader:
        def __init__(self, stream):
            self.pages = ["page one", "page two"]

    monkeypatch.setattr("app.providers.nba_injury_report.PdfReader", FakeReader)
    monkeypatch.setattr(
        "app.providers.nba_injury_report._fragments", lambda page: next(pages)
    )

    report = parse_injury_report(b"unused")

    assert [(row.player_name, row.status, row.team_tricode, row.reason) for row in report.rows] == [
        ("Ingram, Harrison", "Out", "SAS", "Knee;"),
        ("Jones Garcia, David", "Questionable", "SAS", "Injury Management; Left Ankle"),
    ]
    assert report.unparsed == ()


# -- normalization -------------------------------------------------------------


def test_entries_map_statuses_and_keep_available_without_a_canonical_status():
    report = _report(QUARTER_LATE)
    url = BASE + QUARTER_LATE

    entries = {
        entry.source_player_name: entry
        for entry in entries_from_report(report, source_url=url)
    }

    assert len(entries) == 115
    doncic = entries["Luka Doncic"]
    assert doncic.entry_id == "nba_official:2025-12-25:HOU@LAL:lukadoncic"
    assert doncic.source_team_tricode == "LAL"
    assert (doncic.canonical_status, doncic.raw_status) == ("Probable", "Probable")
    assert doncic.source_url == url
    christie = entries["Max Christie"]
    assert (christie.canonical_status, christie.raw_status) == (None, "Available")
    assert entries["Jayson Tatum"].canonical_status == "Out"
    assert entries["Jalen Suggs"].canonical_status == "Doubtful"
    assert entries["Bones Hyland"].canonical_status == "Questionable"


def test_evidence_documents_round_trip():
    entry = entries_from_report(_report(HOURLY), source_url=BASE + HOURLY)[0]

    assert entry.to_document() == {
        "entry_id": "nba_official:2025-10-21:HOU@OKC:isaiahcrawford",
        "source_player_id": "HOU:isaiahcrawford",
        "source_player_name": "Isaiah Crawford",
        "source_team_tricode": "HOU",
        "canonical_status": "Questionable",
        "raw_status": "Questionable",
        "reason": "Injury/Illness - Left Ankle; Sprain",
        "source_url": BASE + HOURLY,
        "game_date": "2025-10-21",
        "matchup": "HOU@OKC",
        "report_time": "2025-10-21T17:30:00-04:00",
    }
    assert type(entry).from_document(entry.to_document()) == entry


# -- discovery -----------------------------------------------------------------


def _utc(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_file_names_cover_both_formats_and_round_trip():
    moment = _utc(2025, 12, 25, 22, 0)  # 5:00 PM ET

    assert report_file_name(moment, quarter_hour=True) == QUARTER_LATE
    assert report_file_name(moment, quarter_hour=False) == (
        "Injury-Report_2025-12-25_05PM.pdf"
    )
    assert report_time_from_name(QUARTER_LATE).hour == 17
    assert report_time_from_name(QUARTER_EARLY).minute == 30
    assert report_time_from_name("Injury-Report_2025-10-21_12AM.pdf").hour == 0
    assert report_time_from_name("not-a-report.pdf") is None


def test_candidates_walk_back_by_quarter_hour_and_try_the_hourly_name_on_the_hour():
    names = candidate_report_names(_utc(2025, 12, 25, 22, 20), max_requests=6)

    assert names == [
        "Injury-Report_2025-12-25_05_15PM.pdf",
        "Injury-Report_2025-12-25_05_00PM.pdf",
        "Injury-Report_2025-12-25_05PM.pdf",
        "Injury-Report_2025-12-25_04_45PM.pdf",
        "Injury-Report_2025-12-25_04_30PM.pdf",
        "Injury-Report_2025-12-25_04_15PM.pdf",
    ]


def test_candidates_cross_midnight_in_eastern_time():
    names = candidate_report_names(_utc(2025, 12, 26, 5, 5), max_requests=4)

    assert names == [
        "Injury-Report_2025-12-26_12_00AM.pdf",
        "Injury-Report_2025-12-26_12AM.pdf",
        "Injury-Report_2025-12-25_11_45PM.pdf",
        "Injury-Report_2025-12-25_11_30PM.pdf",
    ]


class Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self):
        pass


class Site:
    """A fake CDN: named files exist, everything else is 403."""

    def __init__(self, files=None, *, default=403):
        self.files = files or {}
        self.default = default
        self.headers = {}
        self.calls = []

    def get(self, url, *, timeout, stream=False):
        self.calls.append((url, timeout))
        name = url.removeprefix(BASE)
        if name in self.files:
            return Response(200, self.files[name])
        return Response(self.default)


def _provider(site, now, **providers):
    return NBAInjuryReportProvider(
        session=site,
        settings=RuntimeSettings(
            environment="testing", providers=ProviderSettings(**providers)
        ),
        clock=lambda: now,
    )


def test_quarter_hour_era_discovery_steps_over_403_gaps():
    site = Site({QUARTER_LATE: (FIXTURES / QUARTER_LATE).read_bytes()})
    now = _utc(2025, 12, 25, 22, 40)  # 5:40 PM ET; newest file is 5:00 PM

    snapshot = _provider(
        site,
        now,
        injury_report_connect_timeout_seconds=1.25,
        injury_report_read_timeout_seconds=4.5,
    ).get_snapshot()

    assert [url.removeprefix(BASE) for url, _ in site.calls] == [
        "Injury-Report_2025-12-25_05_30PM.pdf",
        "Injury-Report_2025-12-25_05_15PM.pdf",
        QUARTER_LATE,
    ]
    assert {timeout for _, timeout in site.calls} == {(1.25, 4.5)}
    assert snapshot.retrieved_at == now
    assert len(snapshot.entries) == 115
    assert snapshot.entries[0].source_url == BASE + QUARTER_LATE
    assert snapshot.raw_payload[0] == {
        "game_date": "2025-12-25",
        "game_time_et": "02:30 (ET)",
        "matchup": "SAS@OKC",
        "team": "San Antonio Spurs",
        "team_tricode": "SAS",
        "player": "Ingram, Harrison",
        "status": "Out",
        "reason": "G League - Two-Way",
    }
    # The six NOT YET SUBMITTED teams stay visible in the raw observation.
    assert sum(1 for row in snapshot.raw_payload if row["status"] == "NOT YET SUBMITTED") == 6
    event = telemetry.get_recorded_provider_events()[-1]
    assert (event["provider"], event["operation"], event["outcome"]) == (
        "nba_injury_report",
        "get_injuries",
        "success",
    )


def test_hourly_era_discovery_falls_back_to_the_hourly_file_name():
    site = Site({HOURLY: (FIXTURES / HOURLY).read_bytes()})
    now = _utc(2025, 10, 21, 22, 40)  # 6:40 PM EDT

    snapshot = _provider(site, now).get_snapshot()

    assert [url.removeprefix(BASE) for url, _ in site.calls] == [
        "Injury-Report_2025-10-21_06_30PM.pdf",
        "Injury-Report_2025-10-21_06_15PM.pdf",
        "Injury-Report_2025-10-21_06_00PM.pdf",
        "Injury-Report_2025-10-21_06PM.pdf",
        "Injury-Report_2025-10-21_05_45PM.pdf",
        "Injury-Report_2025-10-21_05_30PM.pdf",
        "Injury-Report_2025-10-21_05_15PM.pdf",
        "Injury-Report_2025-10-21_05_00PM.pdf",
        HOURLY,
    ]
    assert len(snapshot.entries) == 54


@pytest.mark.parametrize("missing", [403, 404])
def test_no_report_inside_the_bound_is_a_provider_failure_with_bounded_requests(
    missing,
):
    site = Site(default=missing)

    with pytest.raises(ProviderUnavailableError):
        _provider(site, _utc(2026, 9, 1, 16, 0)).get_snapshot()  # preseason

    assert len(site.calls) == NBAInjuryReportProvider.MAX_REQUESTS == 12
    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == "malformed"


def test_server_error_stops_the_walk_and_is_a_sanitized_failure():
    site = Site(default=500)

    with pytest.raises(ProviderUnavailableError) as raised:
        _provider(site, _utc(2025, 12, 25, 22, 40)).get_snapshot()

    assert len(site.calls) == 1
    assert "status 500" not in str(raised.value)
    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == "http_error"


def test_a_non_pdf_body_is_rejected():
    site = Site({"Injury-Report_2025-12-25_05_30PM.pdf": b"<html>maintenance</html>"})

    with pytest.raises(ProviderUnavailableError):
        _provider(site, _utc(2025, 12, 25, 22, 40)).get_snapshot()


def test_a_report_with_unreadable_rows_is_rejected_not_published_partially():
    # A PDF whose pages parse to nothing must not publish an empty "all clear".
    site = Site({"Injury-Report_2025-12-25_05_30PM.pdf": b"%PDF-1.4\n%%EOF"})

    with pytest.raises(ProviderUnavailableError):
        _provider(site, _utc(2025, 12, 25, 22, 40)).get_snapshot()


def test_transport_timeout_is_a_sanitized_provider_failure():
    class Slow(Site):
        def get(self, url, *, timeout, stream=False):
            raise requests.ReadTimeout("secret upstream detail")

    with pytest.raises(ProviderUnavailableError) as raised:
        _provider(Slow(), _utc(2025, 12, 25, 22, 40)).get_snapshot()

    assert "secret upstream detail" not in str(raised.value)
    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == "timeout"


class Clock:
    """A manual monotonic clock that a fake CDN advances."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowSite(Site):
    def __init__(self, clock, seconds, files=None):
        super().__init__(files)
        self.clock = clock
        self.seconds = seconds

    def get(self, url, *, timeout, stream=False):
        response = super().get(url, timeout=timeout, stream=stream)
        self.clock.now += self.seconds
        return response


def _slow_provider(site, clock, **providers):
    return NBAInjuryReportProvider(
        session=site,
        settings=RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(
                injury_report_probe_delay_seconds=0, **providers
            ),
        ),
        clock=lambda: _utc(2025, 12, 25, 22, 40),
        monotonic=clock,
        sleep=lambda seconds: None,
    )


def test_the_whole_walk_respects_the_deadline():
    clock = Clock()
    site = SlowSite(clock, 5.0)

    with pytest.raises(ProviderUnavailableError):
        _slow_provider(site, clock, injury_report_deadline_seconds=12.0).get_snapshot()

    # Requests at t=0, 5, 10; at t=15 the 12 s budget is spent.
    assert len(site.calls) == 3
    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == "timeout"


def test_each_request_is_capped_by_the_remaining_budget():
    clock = Clock()
    site = SlowSite(clock, 5.0)

    with pytest.raises(ProviderUnavailableError):
        _slow_provider(
            site,
            clock,
            injury_report_deadline_seconds=12.0,
            injury_report_connect_timeout_seconds=3.0,
            injury_report_read_timeout_seconds=8.0,
        ).get_snapshot()

    assert [timeout for _, timeout in site.calls] == [(3.0, 8.0), (3.0, 7.0), (2.0, 2.0)]


def test_a_report_that_arrives_after_the_deadline_is_not_served():
    clock = Clock()
    site = SlowSite(
        clock, 7.0, {"Injury-Report_2025-12-25_05_30PM.pdf": (FIXTURES / QUARTER_LATE).read_bytes()}
    )

    with pytest.raises(ProviderUnavailableError):
        _slow_provider(site, clock, injury_report_deadline_seconds=5.0).get_snapshot()

    assert len(site.calls) == 1


def test_probes_are_paced_and_a_failed_search_is_not_repeated_during_the_cooldown():
    clock = Clock()
    site = Site()
    pauses = []
    provider = NBAInjuryReportProvider(
        session=site,
        settings=RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(
                injury_report_probe_delay_seconds=0.1,
                injury_report_failure_cooldown_seconds=300.0,
            ),
        ),
        clock=lambda: _utc(2025, 12, 25, 22, 40),
        monotonic=clock,
        sleep=pauses.append,
    )

    for _ in range(3):
        with pytest.raises(ProviderUnavailableError):
            provider.get_snapshot()

    assert len(site.calls) == 12  # one probe sequence for three reads
    assert pauses == [0.1] * 11
    clock.now = 299.0
    with pytest.raises(ProviderUnavailableError):
        provider.get_snapshot()
    assert len(site.calls) == 12
    clock.now = 301.0
    with pytest.raises(ProviderUnavailableError):
        provider.get_snapshot()
    assert len(site.calls) == 24  # cooldown over: one more full sequence


def test_a_successful_read_after_a_failure_clears_nothing_but_serves_normally():
    clock = Clock()
    site = Site()
    provider = _slow_provider(site, clock, injury_report_failure_cooldown_seconds=0.0)
    with pytest.raises(ProviderUnavailableError):
        provider.get_snapshot()
    site.files[QUARTER_LATE] = (FIXTURES / QUARTER_LATE).read_bytes()
    site.files["Injury-Report_2025-12-25_05_30PM.pdf"] = site.files[QUARTER_LATE]

    assert len(provider.get_snapshot().entries) == 115


def test_a_report_stamped_after_now_is_not_current_yet():
    # The 05PM file's own header says 05:30 PM.  At 5:10 PM ET it is from the
    # future, so discovery moves on to the older file.
    hourly = (FIXTURES / HOURLY).read_bytes()
    site = Site({"Injury-Report_2025-10-21_05PM.pdf": hourly})

    with pytest.raises(ProviderUnavailableError):
        _provider(site, _utc(2025, 10, 21, 21, 10)).get_snapshot()  # 5:10 PM EDT
    requested = [url.removeprefix(BASE) for url, _ in site.calls]
    assert requested[:3] == [
        "Injury-Report_2025-10-21_05_00PM.pdf",
        "Injury-Report_2025-10-21_05PM.pdf",  # found, but stamped 05:30 PM
        "Injury-Report_2025-10-21_04_45PM.pdf",  # so the walk went on
    ]
    assert len(requested) == 12

    snapshot = _provider(site, _utc(2025, 10, 21, 21, 40)).get_snapshot()  # 5:40 PM
    assert len(snapshot.entries) == 54


def test_header_time_is_the_report_time():
    assert _report(HOURLY).report_time == datetime(
        2025, 10, 21, 17, 30, tzinfo=report_time_from_name(HOURLY).tzinfo
    )
    assert _report(QUARTER_LATE).report_time.hour == 17


def test_candidates_walk_the_utc_timeline_across_the_fall_back_hour():
    # 06:10Z on 2025-11-02 is 1:10 AM EST, the second 1 o'clock hour.
    names = candidate_report_names(_utc(2025, 11, 2, 6, 10), max_requests=8)

    assert names == [
        "Injury-Report_2025-11-02_01_00AM.pdf",  # 01:00 EST
        "Injury-Report_2025-11-02_01AM.pdf",
        "Injury-Report_2025-11-02_01_45AM.pdf",  # 01:45 EDT
        "Injury-Report_2025-11-02_01_30AM.pdf",
        "Injury-Report_2025-11-02_01_15AM.pdf",
        "Injury-Report_2025-11-02_12_45AM.pdf",  # 00:45 EDT; 01:00 EDT repeats
        "Injury-Report_2025-11-02_12_30AM.pdf",
        "Injury-Report_2025-11-02_12_15AM.pdf",
    ]


def test_a_report_with_lines_the_parser_cannot_place_is_rejected_whole(monkeypatch):
    import app.providers.nba_injury_report as module

    site = Site({"Injury-Report_2025-12-25_05_30PM.pdf": b"%PDF-1.4 stand-in"})
    good = _report(QUARTER_LATE)
    partial = module.ParsedInjuryReport(
        title=good.title,
        rows=good.rows,
        unsubmitted=good.unsubmitted,
        unparsed=("Out Out",),
        report_time=good.report_time,
    )
    monkeypatch.setattr(module, "parse_injury_report", lambda content: partial)

    with pytest.raises(ProviderUnavailableError):
        _provider(site, _utc(2025, 12, 25, 22, 40)).get_snapshot()


def test_two_rows_merged_into_one_line_are_unparsed_not_a_status(monkeypatch):
    header = [
        (115.2, 23.0, "Game"), (115.2, 53.0, "Date"), (115.2, 119.0, "Game"),
        (115.2, 149.0, "Time"), (115.2, 200.0, "Matchup"), (115.2, 264.0, "Team"),
        (115.2, 425.0, "Player"), (115.2, 457.0, "Name"), (115.2, 585.0, "Current"),
        (115.2, 624.0, "Status"), (115.2, 666.0, "Reason"),
    ]
    page = [
        *TITLE, *header,
        (136.3, 24.0, "12/25/2025"), (136.3, 121.0, "02:30"), (136.3, 201.0, "SAS@OKC"),
        (136.3, 265.0, "San"), (136.3, 282.0, "Antonio"), (136.3, 317.0, "Spurs"),
        (136.3, 426.0, "Ingram,"), (136.3, 459.0, "Harrison"),
        (136.3, 587.0, "Out"), (136.3, 610.0, "Out"), (136.3, 667.0, "Knee"),
    ]

    class FakeReader:
        def __init__(self, stream):
            self.pages = ["p"]

    monkeypatch.setattr("app.providers.nba_injury_report.PdfReader", FakeReader)
    monkeypatch.setattr("app.providers.nba_injury_report._fragments", lambda p: page)

    report = parse_injury_report(b"x")

    assert report.rows == ()
    assert report.unparsed == ("12/25/2025 02:30 SAS@OKC San Antonio Spurs Ingram, Harrison Out Out Knee",)


def test_the_adapter_never_names_another_publisher():
    site = Site({QUARTER_LATE: (FIXTURES / QUARTER_LATE).read_bytes()})
    _provider(site, _utc(2025, 12, 25, 22, 10)).get_snapshot()

    assert all(url.startswith("https://ak-static.cms.nba.com/") for url, _ in site.calls)


class DrippingResponse:
    """A 200 whose body arrives one chunk per ``seconds`` of fake time."""

    status_code = 200

    def __init__(self, clock, seconds, chunks):
        self.clock, self.seconds, self.chunks = clock, seconds, chunks
        self.delivered = 0
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            self.clock.now += self.seconds
            self.delivered += 1
            yield chunk

    def close(self):
        self.closed = True


def test_a_slowly_delivered_body_is_abandoned_at_the_deadline_not_after():
    clock = Clock()
    response = DrippingResponse(clock, 5.0, [b"%PDF-" + b"x" * 10] * 10)

    class Dripper(Site):
        def get(self, url, *, timeout, stream=False):
            self.calls.append((url, timeout))
            return response

    with pytest.raises(ProviderUnavailableError):
        _slow_provider(
            Dripper(), clock, injury_report_deadline_seconds=12.0
        ).get_snapshot()

    # Chunks land at t=5, 10 and 15; the third is past the 12 s deadline, so
    # the other seven are never read and the connection is released.
    assert response.delivered == 3
    assert clock.now == 15.0
    assert response.closed is True
    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == "timeout"


# -- supplemental round-2 checks --------------------------------------------------


def test_transport_failure_starts_the_cooldown_and_recovers_after_it():
    clock = Clock()
    site = Site(default=500)
    provider = _slow_provider(site, clock, injury_report_failure_cooldown_seconds=300.0)

    with pytest.raises(ProviderUnavailableError):
        provider.get_snapshot()
    site.files["Injury-Report_2025-12-25_05_30PM.pdf"] = (FIXTURES / QUARTER_LATE).read_bytes()
    clock.now = 299.0
    with pytest.raises(ProviderUnavailableError):
        provider.get_snapshot()
    assert len(site.calls) == 1  # the 500 stopped the first search; nothing since

    clock.now = 301.0
    assert len(provider.get_snapshot().entries) == 115
    assert len(site.calls) == 2


@pytest.mark.parametrize(
    ("clock_text", "meridiem", "utc_hour", "utc_minute"),
    [("12:00", "AM", 5, 0), ("12:00", "PM", 17, 0), ("01:30", "AM", 6, 30), ("01:30", "PM", 18, 30)],
)
def test_header_midnight_noon_and_meridiem(monkeypatch, clock_text, meridiem, utc_hour, utc_minute):
    import app.providers.nba_injury_report as module

    real = module._fragments

    def altered(page):
        return [
            (y, x, clock_text if y < 70 and text == "05:00" else meridiem if y < 70 and text == "PM" else text)
            for y, x, text in real(page)
        ]

    monkeypatch.setattr(module, "_fragments", altered)

    report = parse_injury_report((FIXTURES / QUARTER_LATE).read_bytes())

    assert report.report_time.astimezone(timezone.utc) == _utc(2025, 12, 25, utc_hour, utc_minute)


def test_a_report_still_parsing_at_the_deadline_is_not_served(monkeypatch):
    import app.providers.nba_injury_report as module

    clock = Clock()
    site = Site({"Injury-Report_2025-12-25_05_30PM.pdf": (FIXTURES / QUARTER_LATE).read_bytes()})
    real = module.parse_injury_report

    def slow_parse(content):
        report = real(content)
        clock.now += 20.0  # parsing outlasts the 12 s budget
        return report

    monkeypatch.setattr(module, "parse_injury_report", slow_parse)

    with pytest.raises(ProviderUnavailableError):
        _slow_provider(site, clock, injury_report_deadline_seconds=12.0).get_snapshot()
    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == "timeout"


def test_entries_carry_the_report_header_time_not_the_file_name_time():
    # The hourly fixture is named 05PM but its header reads 05:30 PM.
    entry = entries_from_report(_report(HOURLY), source_url=BASE + HOURLY)[0]

    assert entry.report_time == "2025-10-21T17:30:00-04:00"
    assert entry.to_document()["report_time"] == "2025-10-21T17:30:00-04:00"
