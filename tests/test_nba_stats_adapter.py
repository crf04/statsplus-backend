"""Concurrency and timeout tests for the bounded NBA Stats adapter (#10).

The game-log flow now runs as a synchronous Flask/threaded workload with all
``stats.nba.com`` calls funnelled through :class:`NBAStatsAdapter`.  These tests
prove that the provider callers explicitly enforce the configured
``NBA_STATS_MAX_CONCURRENCY`` bound and reuse the configured timeout.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from nba_api.stats import endpoints

from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    DatabaseSettings,
    ProviderSettings,
    RuntimeSettings,
)
from app.services.nba_stats_adapter import GAME_LOG_REQUIRED_COLUMNS, NBAStatsAdapter


class _ConcurrencyProbe:
    """Tracks how many fake provider calls are in flight at once."""

    def __init__(self, hold: float = 0.1):
        self._lock = threading.Lock()
        self._hold = hold
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def run(self, fn):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self._hold)
            return fn()
        finally:
            with self._lock:
                self.active -= 1


def _probe_endpoint(probe):
    class _ProbePlayerGameLogs:
        def __init__(self, *args, **kwargs):
            pass

        def get_data_frames(self, *args, **kwargs):
            frame = probe.run(
                lambda: pd.DataFrame(
                    {column: [1] for column in GAME_LOG_REQUIRED_COLUMNS}
                )
            )
            return [frame]

    return _ProbePlayerGameLogs


def _settings(max_concurrency: int, timeout: float = 1.0) -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        providers=ProviderSettings(
            nba_stats_timeout_seconds=timeout,
            nba_stats_max_concurrency=max_concurrency,
        ),
    )


def test_adapter_exposes_configured_bound():
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=3))

    assert adapter.max_concurrency == 3
    assert adapter.timeout == 1.0


def test_adapter_enforces_concurrency_bound_with_concurrent_calls(monkeypatch):
    probe = _ConcurrencyProbe(hold=0.1)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=2))

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        _probe_endpoint(probe),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(adapter.fetch_player_game_logs, player_id, "2024-25")
            for player_id in range(8)
        ]
        frames = [future.result() for future in futures]

    assert probe.calls == 8
    assert probe.max_active <= 2
    assert len(frames) == 8
    assert all(isinstance(frame, pd.DataFrame) for frame in frames)


def test_http_game_log_requests_share_gate_across_app_service_instances(monkeypatch):
    """Prove the advertised worker bound at the Flask HTTP seam."""
    from app import create_app

    probe = _ConcurrencyProbe(hold=0.05)
    settings = RuntimeSettings(
        environment="testing",
        database=DatabaseSettings(),
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        providers=ProviderSettings(nba_stats_max_concurrency=2),
        nba={"current_season": "2024-25"},
    )

    class ProbePlayerGameLogs:
        def __init__(self, *args, **kwargs):
            pass

        def get_data_frames(self):
            def frame():
                values = {column: [1] for column in GAME_LOG_REQUIRED_COLUMNS}
                values.update(
                    {
                        "GAME_DATE": ["2024-01-01"],
                        "PLAYER_NAME": ["LeBron James"],
                        "MATCHUP": ["BOS vs. LAL"],
                        "MIN": [30],
                        "PTS": [25],
                        "REB": [8],
                        "AST": [7],
                        "FGM": [9],
                        "FGA": [16],
                        "FG3M": [3],
                        "FG3A": [7],
                        "FTM": [4],
                        "FTA": [5],
                        "NBA_FANTASY_PTS": [44.2],
                    }
                )
                return pd.DataFrame(values)

            return [probe.run(frame)]

    monkeypatch.setattr(
        endpoints.playergamelogs, "PlayerGameLogs", ProbePlayerGameLogs
    )

    # Separate apps create separate GameService/NBAStatsAdapter instances,
    # while the shared process setting gives both adapters one semaphore.
    apps = [
        create_app(
            {
                "RUNTIME_SETTINGS": settings,
                "TESTING": True,
                "SKIP_FIREBASE_INIT": True,
                "SKIP_TABLE_CREATE": True,
            }
        )
        for _ in range(2)
    ]

    def request(app):
        with app.test_client() as client:
            return client.get(
                "/api/games/game_logs?player_name=LeBron%20James"
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [
            future.result()
            for future in (
                pool.submit(request, apps[index % len(apps)])
                for index in range(8)
            )
        ]

    assert all(response.status_code == 200 for response in responses)
    assert probe.calls == 8
    assert probe.max_active <= settings.providers.nba_stats_max_concurrency
    service_adapters = [
        app.extensions["request_services"]["game"].nba_stats for app in apps
    ]
    assert service_adapters[0]._bound is service_adapters[1]._bound


def test_adapter_passes_configured_timeout_to_provider(monkeypatch):
    captured = {}
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1, timeout=7.5))

    def fake_endpoint(**kwargs):
        captured.update(kwargs)
        return FakeEndpoint()

    class FakeEndpoint:
        def get_data_frames(self):
            return [
                pd.DataFrame(
                    {column: [1] for column in GAME_LOG_REQUIRED_COLUMNS}
                )
            ]

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: fake_endpoint(**kwargs),
    )

    adapter.fetch_player_game_logs("123", "2024-25")

    assert captured["timeout"] == 7.5
    assert captured["player_id_nullable"] == "123"
    assert captured["season_nullable"] == "2024-25"


def test_adapter_propagates_provider_timeout(monkeypatch):
    import requests

    def timed_out_endpoint(**kwargs):
        raise requests.exceptions.ReadTimeout("stats.nba.com timed out")

    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))
    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: timed_out_endpoint(**kwargs),
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        adapter.fetch_player_game_logs("123", "2024-25")


class _SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StatusResponse:
    """Fake endpoint that exposes ``nba_response.status_code`` only."""

    def __init__(self, status_code):
        self.nba_response = _SimpleNamespace(status_code=status_code)

    def get_data_frames(self):
        if self.nba_response.status_code >= 400:
            raise AssertionError("run_endpoint must raise before parsing")
        return [
            pd.DataFrame({column: [1] for column in GAME_LOG_REQUIRED_COLUMNS})
        ]


def test_adapter_records_upstream_status_on_provider_event(monkeypatch):
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    def fake_endpoint(**kwargs):
        return _StatusResponse(200)

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: fake_endpoint(**kwargs),
    )

    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    adapter.fetch_player_game_logs("123", "2024-25")
    events = telemetry.get_recorded_provider_events()
    assert events
    assert events[-1]["operation"] == "player_game_logs"
    assert events[-1]["status_code"] == 200
    assert events[-1]["outcome"] == telemetry.OUTCOME_SUCCESS


def test_adapter_classifies_non_2xx_status_as_http_error(monkeypatch):
    import requests

    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    def error_endpoint(**kwargs):
        return _StatusResponse(503)

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: error_endpoint(**kwargs),
    )

    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    with pytest.raises(requests.exceptions.HTTPError):
        adapter.fetch_player_game_logs("123", "2024-25")

    events = telemetry.get_recorded_provider_events()
    assert events[-1]["status_code"] == 503
    assert events[-1]["outcome"] == telemetry.OUTCOME_HTTP_ERROR


@pytest.mark.parametrize(
    "frame, required_columns",
    [
        (pd.DataFrame(), ()),
        (pd.DataFrame({"GAME_ID": ["1"]}), ("GAME_ID", "PTS")),
    ],
)
def test_adapter_classifies_empty_or_invalid_schema_as_malformed(
    monkeypatch, frame, required_columns
):
    from app.utils import telemetry

    class Endpoint:
        def get_data_frames(self):
            return [frame]

    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))
    telemetry.clear_recorded_provider_events()

    with pytest.raises(telemetry.ProviderResponseError):
        adapter.run_endpoint(
            "schema_probe",
            lambda timeout: Endpoint(),
            required_columns=required_columns,
        )

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["outcome"] == telemetry.OUTCOME_MALFORMED
