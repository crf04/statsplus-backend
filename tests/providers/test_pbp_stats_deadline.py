"""``get_wowy(deadline_seconds=...)`` against a real local socket server.

A fake session cannot show that the deadline covers a slow body, ends the
socket, or sends one attempt, so these speak actual HTTP over loopback.
"""

from __future__ import annotations

import socketserver
import threading
import time

import pytest
import requests

from app.config.settings import load_settings
from app.errors import ProviderUnavailableError
from app.providers.pbp_stats import PBPStatsAdapter
from app.services import pbp_stats_adapter

BUDGET = 0.4
SLACK = 0.6


class _Server:
    def __init__(self, behave):
        self.connections = 0
        self.disconnected_at = None
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                outer.connections += 1
                behave(outer, self.request)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/wowy"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve(monkeypatch):
    servers = []

    def start(behave):
        server = _Server(behave)
        servers.append(server)
        monkeypatch.setattr(pbp_stats_adapter, "PBP_WOWY_URL", server.url)
        return server

    yield start
    for server in servers:
        server.close()


def _adapter() -> PBPStatsAdapter:
    settings = load_settings(environ={"FLASK_ENV": "testing", "FIREBASE_ADMIN_DISABLED": "true"})
    return PBPStatsAdapter(settings, session=requests.Session())  # default timeouts 10 s / 30 s, 3 retries


def _call(adapter):
    started = time.monotonic()
    with pytest.raises(ProviderUnavailableError):
        adapter.get_wowy(team_id=1610612747, season="2026-27", off_player_ids=[2544], deadline_seconds=BUDGET)
    return time.monotonic() - started


def _trickle(server, sock):
    sock.recv(65536)
    sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
    try:
        while True:  # a byte every 50 ms: no single wait ever times out
            sock.sendall(b"x")
            time.sleep(0.05)
    except OSError:
        pass


def _hang_until_closed(server, sock):
    sock.recv(65536)
    started = time.monotonic()
    sock.settimeout(3)
    try:
        if sock.recv(1) == b"":
            server.disconnected_at = time.monotonic() - started
    except OSError:
        pass


def _drop(server, sock):
    sock.recv(65536)  # then close with no response


def test_a_body_that_keeps_trickling_is_cut_off_at_the_deadline(serve):
    serve(_trickle)

    assert _call(_adapter()) < BUDGET + SLACK


def test_the_socket_of_a_silent_server_ends_with_the_deadline_not_the_default_timeout(serve):
    server = serve(_hang_until_closed)

    _call(_adapter())
    for _ in range(40):
        if server.disconnected_at is not None:
            break
        time.sleep(0.05)

    assert server.disconnected_at is not None and server.disconnected_at < BUDGET + SLACK


def test_a_failed_attempt_is_not_retried(serve):
    server = serve(_drop)

    _call(_adapter())
    time.sleep(0.3)

    assert server.connections == 1
