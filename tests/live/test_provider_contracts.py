"""Live provider-contract tests for #12.

These hit the real upstream providers and are excluded from the default
``pytest`` gate.  Opt in explicitly with ``LIVE_CONTRACT_TESTS=true``, which is
also required by the registered ``live`` marker (``-m live``).

- ``stats.nba.com`` calls go through ``nba_api`` via
  :class:`app.services.nba_stats_adapter.NBAStatsAdapter`.
- ``api.pbpstats.com`` calls use the shared retrying requests session via
  :class:`app.services.pbp_stats_adapter.PBPTotalsAdapter`.

Both paths must be reachable from a normal login as these providers gate
behind realistic HTTP clients, and each successful call must produce a
structured provider event with the documented fields.
"""

from __future__ import annotations

import os

import pytest

from app.config.settings import RuntimeSettings
from app.services.nba_stats_adapter import NBAStatsAdapter
from app.services.pbp_stats_adapter import PBPTotalsAdapter
from app.utils import telemetry

pytestmark = pytest.mark.live


def _live_settings():
    return RuntimeSettings(
        pbp_connect_timeout_seconds=5.0,
    )


@pytest.fixture(autouse=True)
def _clean_events():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def _live_enabled() -> bool:
    return os.environ.get("LIVE_CONTRACT_TESTS", "").strip().lower() == "true"


@pytest.mark.skipif(
    not _live_enabled(),
    reason="live provider contract tests need LIVE_CONTRACT_TESTS=true",
)
def test_live_pbp_totals_record_a_provider_event():
    settings = _live_settings()
    adapter = PBPTotalsAdapter(settings=settings)
    frame = adapter.fetch_totals_frame("player")
    assert frame.shape[0] >= 1

    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == telemetry.PROVIDER_PBP_STATS
    assert event["operation"] == "get_totals_player"
    assert event["outcome"] == telemetry.OUTCOME_SUCCESS
    assert event["cache_status"] == telemetry.CACHE_DISABLED
    assert event["status_code"] == 200
    assert isinstance(event["duration_ms"], float)
    assert isinstance(event["retry_count"], int)


@pytest.mark.skipif(
    not _live_enabled(),
    reason="LIVE_CONTRACT_TESTS=true required",
)
def test_live_nba_game_logs_record_a_provider_event():
    settings = _live_settings()
    adapter = NBAStatsAdapter(settings=settings)

    frame = adapter.fetch_player_game_logs(
        player_id=2544,
        season="2023-24",
        cache_status=telemetry.CACHE_MISS,
    )
    assert frame.shape[0] >= 1

    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == telemetry.PROVIDER_NBA_STATS
    assert event["operation"] == "player_game_logs"
    assert event["outcome"] == telemetry.OUTCOME_SUCCESS
    assert event["cache_status"] == telemetry.CACHE_MISS
    assert isinstance(event["duration_ms"], float)