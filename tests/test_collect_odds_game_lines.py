"""The odds cron stores the day's ESPN game lines without risking its own beat (crf04/statsplus#493)."""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import OddsApiSettings
from app.services import game_lines
from scripts import collect_odds
from tests.services.test_game_lines import GAMES, NYK_GAME, line, world

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "espn" / "scoreboard_2026-11-10.json"


@pytest.fixture
def espn(monkeypatch):
    """A local scoreboard that answers with ``body`` after ``delay`` seconds."""

    state = {"body": FIXTURE.read_bytes(), "delay": 0.0, "drip": None, "requests": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            time.sleep(state["delay"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if state["drip"] is None:
                self.wfile.write(state["body"])
                return
            # One byte per interval: each read finishes inside the per-read timeout.
            for byte in state["body"][:40]:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(state["drip"])

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(game_lines, "SCOREBOARD_URL", f"http://127.0.0.1:{server.server_port}/scoreboard")
    monkeypatch.setattr(game_lines, "TIMEOUT_SECONDS", (0.5, 0.5))
    monkeypatch.setattr(game_lines, "DEADLINE_SECONDS", 1.0)
    yield state
    server.shutdown()
    server.server_close()


def run_script(monkeypatch, engine, odds_api):
    settings = SimpleNamespace(odds_api=odds_api, log_level="INFO")
    monkeypatch.setattr(collect_odds, "load_settings", lambda **_kw: settings)
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: SimpleNamespace(engine=engine))
    return collect_odds.main(["--database-url", "postgresql://odds.example/statsplus"])


def test_each_run_reads_the_scoreboard_once_and_stores_the_lines(tmp_path, monkeypatch, capsys, espn):
    engine = world(tmp_path)

    code = run_script(monkeypatch, engine, OddsApiSettings())

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0] == "no_work: ODDS_API_ENABLED is false"
    assert out[1].startswith("game_lines: ") and '"inserted": 6' in out[1]
    assert len(espn["requests"]) == 1 and espn["requests"][0].startswith("/scoreboard?dates=")
    assert line(engine, NYK_GAME)["total"] == 222.5
    assert {line(engine, game_id)["status"] for game_id in GAMES} == {"available"}


@pytest.mark.parametrize(
    ("odds_api", "code", "first_line"),
    [
        (OddsApiSettings(), 0, "no_work: ODDS_API_ENABLED is false"),
        (OddsApiSettings(enabled=True), 1, "failed: ODDS_API_ENABLED is true but ODDS_API_KEY is not set"),
    ],
)
def test_a_scoreboard_that_times_out_leaves_the_odds_beat_and_exit_code_alone(
    tmp_path, monkeypatch, capsys, espn, odds_api, code, first_line
):
    engine = world(tmp_path)
    espn["delay"] = 2.0
    started = time.monotonic()

    assert run_script(monkeypatch, engine, odds_api) == code

    assert time.monotonic() - started < 1.5
    out = capsys.readouterr().out.splitlines()
    assert out[0] == first_line
    assert out[1].startswith("game_lines failed: ") and "timed out" in out[1]
    assert line(engine, NYK_GAME)["status"] == "unavailable"


def test_a_scoreboard_that_is_not_json_is_reported_and_stores_nothing(tmp_path, monkeypatch, capsys, espn):
    engine = world(tmp_path)
    espn["body"] = b"<html>maintenance</html>"

    assert run_script(monkeypatch, engine, OddsApiSettings()) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith("game_lines failed: ")
    assert line(engine, NYK_GAME)["status"] == "unavailable"


def test_a_scoreboard_that_trickles_in_is_cut_off_at_the_deadline(tmp_path, monkeypatch, capsys, espn):
    engine = world(tmp_path)
    espn["drip"] = 0.1
    started = time.monotonic()

    assert run_script(monkeypatch, engine, OddsApiSettings()) == 0

    assert time.monotonic() - started < 2.5
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "no_work: ODDS_API_ENABLED is false",
        "game_lines failed: TimeoutError: ESPN scoreboard read exceeded 1.0 s",
    ]
    assert line(engine, NYK_GAME)["status"] == "unavailable"
