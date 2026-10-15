"""The residential collector as a Railway cron: NBA proxy and container secret.

The proxy URL and the machine secret are credentials.  These tests drive the
real nba_api request path and the real ``run_once`` wiring, recording what
leaves the process, rather than asserting that a mock was called.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest
import requests

from app.collector import cli
from app.collector.client import HTTPResponse, RequestsTransport
from app.collector.config import CollectorConfig, CollectorConfigurationError, load_collector_config
from app.collector.provider import NBAStatsProviderAdapter

PROXY = "http://proxy-user:proxy-pass-7731@proxy.example:8000"
SECRET = "machine-secret-4417"
BASE_ENV = {
    "COLLECTOR_RAILWAY_URL": "https://railway.example",
    "COLLECTOR_IDENTITY_ID": "railway-cron",
}


class _Reply:
    status_code = 200
    text = '{"resultSets": []}'
    content = b"{}"
    headers: dict = {}
    url = "https://stats.nba.com/stats/x"


@pytest.fixture
def wire(monkeypatch):
    """Record every ``requests`` call (url, proxies) without touching a network."""

    calls: list[tuple[str, object]] = []

    def fake_request(self, method, url, **kwargs):
        calls.append((url, kwargs.get("proxies")))
        return _Reply()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def _swallow(function, *args, **kwargs):
    # The empty reply is not a valid frame; only the outgoing request matters.
    with contextlib.suppress(Exception):
        function(*args, **kwargs)


def test_every_nba_request_carries_the_proxy(wire):
    provider = NBAStatsProviderAdapter(proxy=PROXY)
    _swallow(provider.fetch_whole_season_schedule, season="2026-27")
    _swallow(provider.get_player_roster, season="2026-27")
    _swallow(provider.fetch_synergy_play_types, "Transition", player_or_team_abbreviation="P",
             type_grouping="Offensive", season="2026-27", season_type="Regular Season")
    _swallow(provider.fetch_player_shot_type, "Overall", season="2026-27", season_type="Regular Season")
    _swallow(provider.fetch_opponent_shooting_zone, None, season="2026-27", season_type="Regular Season",
             team_id=1610612737, last_n_games=0)
    _swallow(provider.fetch_player_shooting_zone, season="2026-27", season_type="Regular Season")
    _swallow(provider.fetch_player_season_totals, season="2026-27", season_type="Regular Season")
    _swallow(provider.fetch_opponent_shot_chart, "Overall", None, season="2026-27",
             season_type="Regular Season", team_id=1610612737, last_n_games=0)
    _swallow(provider.provider._team_window_evidence, season="2026-27", season_type="Regular Season",
             team_id=1610612737, last_n_games=0, date_from=None, date_to=None, measure_type="Opponent")
    assert len(wire) >= 9
    assert all("nba.com" in url for url, _ in wire)
    assert all(proxies == {"http": PROXY, "https": PROXY} for _, proxies in wire)


def test_nba_requests_have_no_proxy_when_unset(wire, monkeypatch):
    monkeypatch.setattr("nba_api.library.http.PROXY", None)
    _swallow(NBAStatsProviderAdapter().fetch_whole_season_schedule, season="2026-27")
    assert [proxies for _, proxies in wire] == [None]


def test_control_plane_requests_never_use_the_proxy(wire):
    RequestsTransport().request("POST", "https://railway.example/api/collector/token", json_body={})
    assert wire == [("https://railway.example/api/collector/token", None)]


class _Transport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, json_body=None, timeout=30):
        self.calls.append((method, url, headers, body, json_body))
        if url.endswith("/api/collector/token"):
            return HTTPResponse(201, {"token": "short-lived", "expires_in": 300})
        if "/api/collector/discovery" in url:
            return HTTPResponse(200, {"environment": "production", "bootstrap_requests": [], "manifests": []})
        return HTTPResponse(200, {})


def _config(tmp_path: Path, **overrides) -> CollectorConfig:
    values = dict(
        railway_url="https://railway.example", environment="production", identity_id="railway-cron",
        outbox_path=tmp_path / "outbox.sqlite3", log_path=tmp_path / "collector.log", release_version="t",
    )
    values.update(overrides)
    return CollectorConfig(**values)


def test_run_once_builds_the_nba_adapter_with_the_proxy_and_keeps_it_off_the_control_plane(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    built = []
    real = cli.NBAStatsProviderAdapter
    monkeypatch.setattr(cli, "NBAStatsProviderAdapter", lambda **kw: built.append(real(**kw)) or built[-1])
    transport = _Transport()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.run_once(_config(tmp_path, nba_proxy_url=PROXY), transport=transport)
    assert [adapter.provider.proxy for adapter in built] == [PROXY]
    assert PROXY not in json.dumps(transport.calls, default=str)


def test_run_once_exchanges_the_env_secret_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    transport = _Transport()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.run_once(_config(tmp_path), transport=transport)
    token_bodies = [call[4] for call in transport.calls if call[1].endswith("/api/collector/token")]
    assert [body["secret"] for body in token_bodies] == [SECRET]


def test_run_once_falls_back_to_keyring_when_the_env_secret_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLECTOR_SECRET", raising=False)
    asked = []

    class FakeKeyring:
        @staticmethod
        def get_password(service, user):
            asked.append((service, user))
            return "keyring-secret"

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)
    transport = _Transport()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.run_once(_config(tmp_path), transport=transport)
    assert asked == [("StatsPlus/Collector/railway-cron", "machine")]
    token_bodies = [call[4] for call in transport.calls if call[1].endswith("/api/collector/token")]
    assert [body["secret"] for body in token_bodies] == ["keyring-secret"]


def test_validate_config_and_logs_never_contain_the_proxy_or_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    for key, value in {**BASE_ENV, "COLLECTOR_NBA_PROXY_URL": PROXY,
                       "COLLECTOR_OUTBOX_PATH": str(tmp_path / "outbox.sqlite3")}.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["validate-config"]) == 0
    validated = capsys.readouterr().out
    assert json.loads(validated)["nba_proxy"] == "configured"
    config = load_collector_config()
    monkeypatch.setattr(cli, "RequestsTransport", _Transport)
    cli.main(["run"])
    output = capsys.readouterr()
    logged = Path(config.log_path).read_text() if Path(config.log_path).exists() else ""
    for text in (validated, output.out, output.err, logged, repr(config)):
        assert PROXY not in text and "proxy-pass-7731" not in text and SECRET not in text


@pytest.mark.parametrize("bad", ["not a url", "socks5://h:1", "http://:80", "http://h:notaport"])
def test_malformed_proxy_is_rejected_without_echoing_it(bad):
    with pytest.raises(CollectorConfigurationError) as raised:
        load_collector_config({**BASE_ENV, "COLLECTOR_NBA_PROXY_URL": bad})
    assert bad not in str(raised.value)
