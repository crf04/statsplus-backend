"""Opt-in live contract checks for The Odds API (spikes of crf04/statsplus#185).

Needs ``LIVE_CONTRACT_TESTS=true`` and ``ODDS_API_KEY``; spends at most one
paid call (5 credits) per test and only when an event is close enough to carry
props.
Run: ``LIVE_CONTRACT_TESTS=true ODDS_API_KEY=... pytest -m live tests/live/test_odds_api_live.py``.
The three-spike runner with a credit cap is ``scripts/odds_api_spike.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

from app.providers.odds_api import (
    BOOK_ALLOWLIST,
    DFS_BOOK_ALLOWLIST,
    MARKET_CATEGORIES,
    OddsApiClient,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("LIVE_CONTRACT_TESTS", "").strip().lower() != "true"
        or not os.environ.get("ODDS_API_KEY"),
        reason="needs LIVE_CONTRACT_TESTS=true and ODDS_API_KEY",
    ),
]


def test_live_events_are_free_and_bulk_odds_do_not_serve_props():
    client = OddsApiClient(os.environ["ODDS_API_KEY"], regions="us")
    now = datetime.now(timezone.utc)
    listed = client.list_events(now, now + timedelta(hours=36))
    assert all(event.home_team and event.away_team for event in listed.events)
    status, served_props, _quota = client.bulk_props_probe()
    assert served_props is False, f"bulk /odds returned props (http {status})"


def test_live_empty_event_costs_nothing_and_books_are_allowlisted():
    client = OddsApiClient(os.environ["ODDS_API_KEY"], regions="us")
    now = datetime.now(timezone.utc)
    events = sorted(
        client.list_events(now, now + timedelta(hours=36)).events,
        key=lambda event: event.commence_time,
    )
    if not events:
        pytest.skip("no events in the next 36 hours")
    result = client.event_odds(events[-1].event_id)
    if not result.parsed.quotes:
        assert result.cost == 0
    assert {quote.book for quote in result.parsed.quotes} <= BOOK_ALLOWLIST


def test_live_us_dfs_answers_with_the_documented_bookmakers_and_standard_markets():
    """The facts crf04/statsplus#215 builds on, unverified until this runs."""

    client = OddsApiClient(os.environ["ODDS_API_KEY"], regions="us_dfs")
    now = datetime.now(timezone.utc)
    events = sorted(
        client.list_events(now, now + timedelta(hours=36)).events,
        key=lambda event: event.commence_time,
    )
    if not events:
        pytest.skip("no events in the next 36 hours")
    result = client.event_odds(events[0].event_id)
    parsed = result.parsed
    if not parsed.bookmaker_count:
        assert result.cost == 0
        pytest.skip("no DFS props posted for the nearest event yet")
    # A response that carries only other apps proves nothing about PrizePicks:
    # skip rather than pass, so "unverified" stays visible.
    if not parsed.quotes:
        pytest.skip(
            "DFS props are posted but none is from bookmaker key 'prizepicks' "
            f"(saw {sorted(parsed.dropped_books)}); the key or market names may differ"
        )
    assert {quote.book for quote in parsed.quotes} == DFS_BOOK_ALLOWLIST
    assert parsed.dropped_books <= {"underdog", "pick6", "dabble_us_dfs"}
    assert {quote.market_key for quote in parsed.quotes} <= set(MARKET_CATEGORIES)
    assert {quote.category for quote in parsed.quotes} & {"PTS", "REB", "AST"}
    assert 0 < result.cost <= len(MARKET_CATEGORIES)
