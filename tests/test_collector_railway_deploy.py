"""The residential collector as a Railway cron: NBA proxy and container secret.

The proxy URL and the machine secret are credentials.  These tests drive the
real nba_api request path and the real ``run_once`` wiring, recording what
leaves the process, rather than asserting that a mock was called.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from datetime import UTC, datetime, timedelta
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
    # Empty frames: enough for a second dependent request to be made.
    text = json.dumps(
        {
            "resultSets": [
                {"name": name, "headers": ["TEAM_ID"], "rowSet": []}
                for name in ("SynergyPlayType", "LeagueDashTeamStats", "x")
            ]
        }
    )
    content = b"{}"
    headers: dict = {}
    url = "https://stats.nba.com/stats/x"


@pytest.fixture(autouse=True)
def isolated_release_root(tmp_path, monkeypatch):
    """``run_once`` checksums COLLECTOR_RELEASE_ROOT; the default is the working
    tree, which races pytest-cov's transient ``.coverage.*`` files."""

    root = tmp_path / "release"
    root.mkdir()
    (root / "statsplus_collector.txt").write_text("release", encoding="utf-8")
    monkeypatch.setenv("COLLECTOR_RELEASE_ROOT", str(root))
    monkeypatch.delenv("COLLECTOR_SECRET", raising=False)
    monkeypatch.delenv("COLLECTOR_MACHINE_SECRET", raising=False)


@pytest.fixture
def wire(monkeypatch):
    """Record every ``requests`` call (url, proxies) without touching a network."""

    calls: list[tuple[str, object]] = []

    def fake_request(self, method, url, **kwargs):
        calls.append((url, kwargs.get("proxies") or dict(self.proxies) or None))
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
    _swallow(
        provider.fetch_synergy_play_types,
        "Transition",
        player_or_team_abbreviation="P",
        type_grouping="Offensive",
        season="2026-27",
        season_type="Regular Season",
    )
    _swallow(provider.fetch_player_shot_type, "Overall", season="2026-27", season_type="Regular Season")
    _swallow(
        provider.fetch_opponent_shooting_zone,
        None,
        season="2026-27",
        season_type="Regular Season",
        team_id=1610612737,
        last_n_games=0,
    )
    _swallow(provider.fetch_player_shooting_zone, season="2026-27", season_type="Regular Season")
    _swallow(provider.fetch_player_season_totals, season="2026-27", season_type="Regular Season")
    _swallow(
        provider.fetch_opponent_shot_chart,
        "Overall",
        None,
        season="2026-27",
        season_type="Regular Season",
        team_id=1610612737,
        last_n_games=0,
    )
    _swallow(
        provider.provider._team_window_evidence,
        season="2026-27",
        season_type="Regular Season",
        team_id=1610612737,
        last_n_games=0,
        date_from=None,
        date_to=None,
        measure_type="Opponent",
    )
    assert len(wire) >= 9
    assert all("nba.com" in url for url, _ in wire)
    assert all(proxies == {"http": PROXY, "https": PROXY} for _, proxies in wire)


def test_team_synergy_and_its_supporting_minutes_request_both_carry_the_proxy(wire):
    _swallow(
        NBAStatsProviderAdapter(proxy=PROXY).fetch_synergy_play_types,
        "Transition",
        player_or_team_abbreviation="T",
        type_grouping="Offensive",
        season="2026-27",
        season_type="Regular Season",
    )
    assert len(wire) == 2, wire
    assert all(proxies == {"http": PROXY, "https": PROXY} for _, proxies in wire)


def test_nba_requests_have_no_proxy_when_unset(wire, monkeypatch):
    monkeypatch.setattr("nba_api.library.http.PROXY", None)
    _swallow(NBAStatsProviderAdapter().fetch_whole_season_schedule, season="2026-27")
    assert [proxies for _, proxies in wire] == [None]


def test_control_plane_requests_never_use_the_proxy(wire):
    RequestsTransport().request("POST", "https://railway.example/api/collector/token", json_body={})
    assert wire == [("https://railway.example/api/collector/token", None)]


class _Transport:
    def __init__(self, discovery=None, environment="production"):
        self.calls = []
        self.discovery = discovery or {"environment": environment, "bootstrap_requests": [], "manifests": []}

    def request(self, method, url, *, headers=None, body=None, json_body=None, timeout=30):
        self.calls.append((method, url, headers, body, json_body))
        if url.endswith("/api/collector/token"):
            return HTTPResponse(201, {"token": "short-lived", "expires_in": 300})
        if "/api/collector/discovery" in url:
            return HTTPResponse(200, self.discovery)
        return HTTPResponse(200, {})


def _config(tmp_path: Path, **overrides) -> CollectorConfig:
    values = dict(
        railway_url="https://railway.example",
        environment="production",
        identity_id="railway-cron",
        outbox_path=tmp_path / "outbox.sqlite3",
        log_path=tmp_path / "collector.log",
        release_version="t",
    )
    values.update(overrides)
    return CollectorConfig(**values)


def test_run_once_builds_the_nba_adapter_with_the_proxy_and_keeps_it_off_the_control_plane(
    tmp_path,
    monkeypatch,
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
    for key, value in {
        **BASE_ENV,
        "COLLECTOR_NBA_PROXY_URL": PROXY,
        "COLLECTOR_OUTBOX_PATH": str(tmp_path / "outbox.sqlite3"),
    }.items():
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


def test_control_plane_requests_bypass_a_configured_proxy_through_run_once(tmp_path, monkeypatch, wire):
    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    monkeypatch.setenv("COLLECTOR_NBA_PROXY_URL", PROXY)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.run_once(_config(tmp_path, nba_proxy_url=PROXY))  # real RequestsTransport
    assert wire, "the control plane was never contacted"
    assert all(url.startswith("https://railway.example/") for url, _ in wire)
    assert all(proxies is None for _, proxies in wire)


def test_only_collector_secret_set_lets_run_succeed_in_a_non_production_environment(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    for key, value in {
        **BASE_ENV,
        "COLLECTOR_ENVIRONMENT": "testing",
        "COLLECTOR_OUTBOX_PATH": str(tmp_path / "outbox.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    transport = _Transport(environment="testing")
    monkeypatch.setattr(cli, "RequestsTransport", lambda: transport)
    assert cli.main(["credential-check"]) == 0
    assert cli.main(["run"]) == 0
    token_bodies = [call[4] for call in transport.calls if call[1].endswith("/api/collector/token")]
    assert [body["secret"] for body in token_bodies] == [SECRET]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] in {"no_work", "complete"}


def _run_with_nba_failure(tmp_path, monkeypatch, capsys, caplog, failure, expected_code):
    from app.services.collection_control import _collector_scope_descriptors

    def refuse(self, method, url, **kwargs):
        if "nba.com" in url:
            raise failure
        return _Reply()

    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    live = datetime.now(UTC)
    scopes = {"exact_shot_zones_opponent_season", "shot_zones_opponent"}
    discovery = {
        "environment": "production",
        "bootstrap_requests": [],
        "manifests": [
            {
                "manifest_id": "manifest-1",
                "season": "2026-27",
                "cutoff": live.isoformat(),
                "collect_before": (live + timedelta(hours=1)).isoformat(),
                "accepted_versions": [2],
                "scopes": sorted(scopes),
                "scope_descriptors": _collector_scope_descriptors(scopes, live),
            }
        ],
    }
    config = _config(tmp_path, nba_proxy_url=PROXY)
    shared = logging.getLogger("statsplus.residential")
    for handler in list(shared.handlers):
        shared.removeHandler(handler)
    transport = _Transport(discovery=discovery)
    try:
        with caplog.at_level(logging.DEBUG):
            code = cli.run_once(config, transport=transport)
    finally:
        for handler in list(shared.handlers):
            handler.close()
            shared.removeHandler(handler)
    assert code == expected_code
    output = capsys.readouterr()
    log = config.log_path.read_text() if config.log_path.exists() else ""
    reported = json.dumps(transport.calls, default=str)
    for text in (output.out, output.err, log, caplog.text, reported):
        assert "proxy-pass-7731" not in text and "proxy.example" not in text
    return output.out + log + reported


def test_a_proxy_failure_is_retryable_and_leaks_neither_stdout_nor_the_log(
    tmp_path, monkeypatch, capsys, caplog
):
    failure = requests.exceptions.ProxyError(f"Unable to connect to proxy {PROXY}")
    evidence = _run_with_nba_failure(tmp_path, monkeypatch, capsys, caplog, failure, 10)
    assert "provider_unavailable" in evidence  # retryable, not provider_schema_changed


def test_an_unexpected_nba_exception_carrying_the_proxy_url_leaks_nowhere(
    tmp_path, monkeypatch, capsys, caplog
):
    failure = ValueError(f"unexpected reply via {PROXY}")
    _run_with_nba_failure(tmp_path, monkeypatch, capsys, caplog, failure, 20)


def test_credential_check_and_railway_rehearsal_still_fall_back_to_keyring_outside_production(
    tmp_path,
    monkeypatch,
    capsys,
):
    asked = []

    class FakeKeyring:
        @staticmethod
        def get_password(service, user):
            asked.append((service, user))
            return "keyring-secret"

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)
    for key, value in {
        **BASE_ENV,
        "COLLECTOR_ENVIRONMENT": "testing",
        "COLLECTOR_OUTBOX_PATH": str(tmp_path / "outbox.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["credential-check"]) == 0
    assert asked == [("StatsPlus/Collector/railway-cron", "machine")]

    exchanged = []

    class StopAfterExchange(Exception):
        pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def exchange_token(self, secret):
            exchanged.append(secret)
            raise StopAfterExchange

    monkeypatch.setattr(cli, "RailwayClient", FakeClient)
    with pytest.raises(StopAfterExchange):
        cli.main(["railway-rehearsal", "--season", "2026-27", "--cutoff", "2026-10-20T00:00:00+00:00"])
    assert exchanged == ["keyring-secret"]


def _cron_runs_per_day(expression: str) -> int:
    minutes, hours, day, month, weekday = expression.split()
    assert (day, month, weekday) == ("*", "*", "*")

    def count(field: str, low: int, high: int) -> int:
        total = 0
        for part in field.split(","):
            if part == "*":
                total += high - low + 1
            elif part.startswith("*/"):
                total += len(range(low, high + 1, int(part[2:])))
            elif "-" in part:
                start, end = part.split("-")
                total += int(end) - int(start) + 1
            else:
                total += 1
        return total

    return count(minutes, 0, 59) * count(hours, 0, 23)


def test_cron_cadence_stays_within_the_control_plane_poll_budget(tmp_path, monkeypatch):
    from app.services.collection_control import USAGE_MAX_POLLS

    monkeypatch.setenv("COLLECTOR_SECRET", SECRET)
    transport = _Transport()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.run_once(_config(tmp_path), transport=transport)
    # A no-work run: every authenticated call except the token exchange is a poll.
    polls_per_run = len([c for c in transport.calls if not c[1].endswith("/api/collector/token")])
    assert polls_per_run >= 2

    schedule = json.loads((Path(__file__).resolve().parents[1] / "railway.collector.json").read_text())
    runs = _cron_runs_per_day(schedule["deploy"]["cronSchedule"])
    # Half the budget stays free for manual runs and manifest reads.
    assert runs * polls_per_run <= USAGE_MAX_POLLS // 2, (runs, polls_per_run)
