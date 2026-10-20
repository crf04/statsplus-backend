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
        server.disconnected_at = time.monotonic()


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


def _fetch_threads():
    return {t for t in threading.enumerate() if t.name.endswith("(fetch)")}


def test_a_body_that_keeps_trickling_is_cut_off_at_the_deadline_and_its_worker_and_socket_end(serve):
    server = serve(_trickle)
    before = _fetch_threads()

    started = time.monotonic()
    elapsed = _call(_adapter())
    for _ in range(20):
        if server.disconnected_at is not None and not (_fetch_threads() - before):
            break
        time.sleep(0.05)

    assert elapsed < BUDGET + SLACK
    assert server.disconnected_at is not None  # the server saw the client hang up
    assert server.disconnected_at - started < BUDGET + 0.5
    assert _fetch_threads() - before == set()


def test_a_redirect_is_a_failure_and_is_not_followed(serve):
    target = serve(lambda server, sock: sock.recv(65536))
    origin = serve(
        lambda server, sock: (
            sock.recv(65536),
            sock.sendall(
                f"HTTP/1.1 302 Found\r\nLocation: {target.url}\r\nContent-Length: 0\r\n\r\n".encode()
            ),
        )
    )
    pbp_stats_adapter.PBP_WOWY_URL = origin.url  # the fixture pointed it at the last server started

    _call(_adapter())

    assert (origin.connections, target.connections) == (1, 0)


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


def test_a_response_that_arrives_after_the_caller_gave_up_is_closed_unread():
    from unittest.mock import Mock

    response = Mock(is_redirect=False)
    response.iter_content.return_value = iter([b"{}"])
    session = Mock()
    session.get.side_effect = lambda *args, **kwargs: (time.sleep(0.3), response)[1]
    adapter = _adapter()
    adapter._bounded_session = session

    with pytest.raises(requests.exceptions.Timeout):
        adapter._get_by_deadline("http://unused.invalid/wowy", {}, 0.1)
    time.sleep(0.4)

    response.close.assert_called_once_with()
    response.iter_content.assert_not_called()
