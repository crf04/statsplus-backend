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

from app.config.settings import ProviderSettings, RuntimeSettings
from app.services.nba_stats_adapter import NBAStatsAdapter


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
                lambda: pd.DataFrame({"GAME_ID": ["1"], "PTS": [1]})
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


def test_adapter_passes_configured_timeout_to_provider(monkeypatch):
    captured = {}
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1, timeout=7.5))

    def fake_endpoint(**kwargs):
        captured.update(kwargs)
        return FakeEndpoint()

    class FakeEndpoint:
        def get_data_frames(self):
            return [pd.DataFrame({"GAME_ID": ["1"], "PTS": [1]})]

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