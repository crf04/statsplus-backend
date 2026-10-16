"""Opt-in live check for the PrizePicks scraping path.

Hits the real board at ``partner-api.prizepicks.com``.  Excluded from the default
gate; needs ``LIVE_CONTRACT_TESTS=true``.  Set ``DFS_PRIZEPICKS_PROXY_URL`` to
exercise the residential-proxy path, which is what a datacenter host (DataDome
403s) requires:

    LIVE_CONTRACT_TESTS=true pytest -m live tests/live/test_prizepicks_live.py

    LIVE_CONTRACT_TESTS=true \
      DFS_PRIZEPICKS_PROXY_URL='http://user:pass_country-us@geo.iproyal.com:12321' \
      pytest -m live tests/live/test_prizepicks_live.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.providers.dfs import NBAMarketQuery, RetrievalContext, SnapshotStatus
from app.providers.prizepicks import PrizePicksAdapter
from app.utils import telemetry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("LIVE_CONTRACT_TESTS", "").strip().lower() != "true",
        reason="needs LIVE_CONTRACT_TESTS=true",
    ),
]


@pytest.fixture(autouse=True)
def _clean_events():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def test_live_prizepicks_board_yields_nba_lines():
    adapter = PrizePicksAdapter(
        proxy_url=os.environ.get("DFS_PRIZEPICKS_PROXY_URL") or None
    )
    assert adapter.BASE_URL == "https://partner-api.prizepicks.com/projections"

    context = RetrievalContext(
        deadline=datetime.now(timezone.utc) + timedelta(seconds=60)
    )
    snapshot = adapter.get_snapshot(NBAMarketQuery(), context)

    if not snapshot.markets:
        pytest.skip("no NBA markets on the PrizePicks board right now")

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.pagination_complete is True
    for market in snapshot.markets:
        assert market.athlete is not None
        assert market.athlete.name
        assert market.threshold is not None

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == telemetry.PROVIDER_PRIZEPICKS
    assert event["operation"] == "get_snapshot"
    assert event["outcome"] == telemetry.OUTCOME_SUCCESS
    assert event["status_code"] == 200
