"""NBA.com's daily lineups feed, parsed from a recorded document (crf04/statsplus#492)."""

import json
import subprocess
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from app.providers.nba_daily_lineups import fetch_daily_lineups, parse_daily_lineups
from app.utils.telemetry import ProviderResponseError

RECORDED = Path("tests/fixtures/nba_daily_lineups/00_daily_lineups_20261110.json")


def _recorded():
    return json.loads(RECORDED.read_text())


def _team(lineups, tricode):
    (lineup,) = [lineup for lineup in lineups if lineup.tricode == tricode]
    return lineup


def test_an_expected_lineup_lists_its_starters_once_and_its_inactive_players():
    okc = _team(parse_daily_lineups(_recorded()), "OKC")

    assert (okc.game_id, okc.team_id) == ("0022600216", 1610612760)
    assert okc.feed_timestamp.isoformat() == "2026-11-10T22:29:43-05:00"
    assert [starter.as_json() for starter in okc.starters] == [
        {"player_id": 1642349, "name": "Ajay Mitchell", "position": "SG", "lineup_status": "Expected"},
        {"player_id": 1629026, "name": "Kenrich Williams", "position": "SF", "lineup_status": "Expected"},
        {
            "player_id": 1628983,
            "name": "Shai Gilgeous-Alexander",
            "position": "PG",
            "lineup_status": "Expected",
        },
        {"player_id": 1643530, "name": "Aday Mara", "position": "PF", "lineup_status": "Expected"},
        {"player_id": 1628392, "name": "Isaiah Hartenstein", "position": "C", "lineup_status": "Expected"},
    ]
    assert [player.as_json() for player in okc.inactive] == [
        {"player_id": 1631096, "name": "Chet Holmgren"},
        {"player_id": 1631114, "name": "Jalen Williams"},
        {"player_id": 1631119, "name": "Jaylin Williams"},
        {"player_id": 1641717, "name": "Cason Wallace"},
        {"player_id": 1642260, "name": "Nikola Topić"},
        {"player_id": 1642850, "name": "Thomas Sorber"},
    ]


def test_a_confirmed_lineup_keeps_the_feed_status_word():
    nyk = _team(parse_daily_lineups(_recorded()), "NYK")

    assert [(s.name, s.position, s.lineup_status) for s in nyk.starters] == [
        ("Jalen Brunson", "SG", "Confirmed"),
        ("OG Anunoby", "SF", "Confirmed"),
        ("Landry Shamet", "PG", "Confirmed"),
        ("Mikal Bridges", "PF", "Confirmed"),
        ("Karl-Anthony Towns", "C", "Confirmed"),
    ]
    assert [player.name for player in nyk.inactive] == ["Josh Hart", "Jaden Akins", "Tyler Nickel"]
    assert nyk.feed_timestamp.isoformat() == "2026-11-10T22:14:00-05:00"


def test_every_team_of_the_recorded_day_is_parsed():
    lineups = parse_daily_lineups(_recorded())

    assert sorted(lineup.tricode for lineup in lineups) == [
        "BOS",
        "LAC",
        "MIA",
        "MIL",
        "NYK",
        "OKC",
        "ORL",
        "PHI",
        "PHX",
        "POR",
        "SAC",
        "UTA",
    ]
    assert {len(lineup.starters) for lineup in lineups} == {5}


def _player(
    person_id, name, *, position="", status="Confirmed", roster="Active", stamp="2026-11-10T18:00:00"
):
    return {
        "personId": person_id,
        "teamId": 1,
        "playerName": name,
        "lineupStatus": status,
        "position": position,
        "rosterStatus": roster,
        "timestamp": stamp,
    }


def _document(players):
    return {
        "games": [
            {
                "gameId": "0022600300",
                "awayTeam": {"teamId": 1, "teamAbbreviation": "AAA", "players": players},
                "homeTeam": {"teamId": 2, "teamAbbreviation": "BBB", "players": []},
            }
        ]
    }


def test_a_team_with_no_starter_rows_has_no_starters_and_keeps_its_inactive_list():
    (aaa, bbb) = parse_daily_lineups(
        _document([_player(10, "Bench Guy"), _player(11, "Hurt Guy", roster="Inactive")])
    )

    assert aaa.starters == ()
    assert [player.name for player in aaa.inactive] == ["Hurt Guy"]
    assert bbb.starters == () and bbb.inactive == () and bbb.feed_timestamp is None


def test_four_or_six_starters_are_reported_as_the_feed_lists_them():
    four = [_player(n, f"P{n}", position="G") for n in range(4)]
    six = [_player(n, f"P{n}", position="F") for n in range(6)]

    assert len(parse_daily_lineups(_document(four))[0].starters) == 4
    assert len(parse_daily_lineups(_document(six))[0].starters) == 6


def test_a_starter_listed_twice_as_a_starter_appears_once():
    (aaa, _) = parse_daily_lineups(
        _document(
            [
                _player(10, "Twice", position="PG", status="Expected"),
                _player(10, "Twice", position="PG", status="Confirmed"),
                _player(20, "Hurt", roster="Inactive"),
                _player(20, "Hurt", roster="Inactive"),
            ]
        )
    )

    assert [(s.name, s.lineup_status) for s in aaa.starters] == [("Twice", "Expected")]
    assert [player.name for player in aaa.inactive] == ["Hurt"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"games": None},
        {"games": [{"gameId": "0022600300", "awayTeam": {"teamAbbreviation": "AAA", "players": []}}]},
        _document([{"playerName": "No Id", "position": "PG"}]),
        _document([_player(1, "Bad Stamp", stamp="tonight")]),
    ],
)
def test_a_malformed_document_is_refused_whole(payload):
    with pytest.raises(ProviderResponseError):
        parse_daily_lineups(payload)


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.is_redirect = 300 <= status_code < 400

    def json(self):
        assert not self.is_redirect and self.status_code < 400, "the body of a refusal was read"
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_fetch_reads_the_eastern_day_document_with_the_stats_headers():
    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return _Response(200, {"games": []})

    assert fetch_daily_lineups(date(2026, 11, 10), get=get) == {"games": []}
    assert seen["url"] == "https://stats.nba.com/js/data/leaders/00_daily_lineups_20261110.json"
    assert seen["headers"]["Referer"] == "https://www.nba.com/"
    assert seen["allow_redirects"] is False
    assert seen["timeout"] == 5


@pytest.mark.parametrize("status", [302, 404])
def test_a_day_not_published_yet_reads_as_none_without_reading_the_body(status):
    assert fetch_daily_lineups(date(2026, 11, 11), get=lambda url, **_: _Response(status)) is None


def test_a_server_error_raises():
    with pytest.raises(requests.HTTPError):
        fetch_daily_lineups(date(2026, 11, 11), get=lambda url, **_: _Response(500))


class _Trickle(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "64")
        self.end_headers()
        for _ in range(64):
            self.wfile.write(b" ")
            self.wfile.flush()
            time.sleep(0.1)

    def log_message(self, *_args):
        pass


def test_a_real_response_that_trickles_is_abandoned_at_the_deadline():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    local = f"http://127.0.0.1:{server.server_address[1]}/lineups.json"
    started = time.monotonic()
    try:
        with pytest.raises(requests.Timeout):
            fetch_daily_lineups(
                date(2026, 11, 10),
                timeout=0.5,
                get=lambda _url, headers, **kwargs: requests.get(local, **kwargs),
            )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert elapsed < 1.5


def test_a_download_still_hanging_at_the_deadline_does_not_keep_the_host_process_alive():
    host = """
import threading
from datetime import date

import requests

from app.providers.nba_daily_lineups import fetch_daily_lineups

try:
    fetch_daily_lineups(date(2026, 11, 10), timeout=0.2, get=lambda *_a, **_k: threading.Event().wait())
except requests.Timeout:
    print("timed out", flush=True)
"""
    finished = subprocess.run([sys.executable, "-c", host], capture_output=True, text=True, timeout=20)

    assert finished.stdout == "timed out\n"
