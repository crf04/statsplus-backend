"""Client contract and secret-handling tests for The Odds API (#377).

Bodies are the recorded fixtures in ``tests/fixtures/odds_api``; the transport
is a fake ``requests`` session, so nothing touches the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging

import pytest
import requests

from app.errors import _sanitize_diagnostic_detail
from app.providers.odds_api import (
    BOOK_ALLOWLIST,
    OddsApiClient,
    OddsApiError,
    install_log_redaction,
    parse_event_odds,
    redact_url,
)
from app.utils.sentry_setup import scrub_event
from tests.support.odds_world import FakeResponse, load, quota_headers

SECRET = "odds-fake-key-9f8e7d6c5b4a39281706f5e4d3c2b1a0"
WINDOW = (
    datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
    datetime(2026, 10, 22, 12, tzinfo=timezone.utc),
)


class Session:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if self.error is not None:
            raise self.error
        return self.response


def client_for(session):
    return OddsApiClient(SECRET, session=session, timeout=7)


# -- recorded responses -----------------------------------------------------


def test_events_list_is_decoded_and_requested_as_a_free_window():
    session = Session(FakeResponse(200, load("events"), quota_headers(20000, 0, 0)))

    result = client_for(session).list_events(*WINDOW)

    assert [(e.event_id, e.home_team, e.away_team) for e in result.events] == [
        ("9e6c2f0a1b7d4c3e8a5f6b7c8d9e0f12", "Denver Nuggets", "Los Angeles Lakers"),
        ("1f2e3d4c5b6a79880a1b2c3d4e5f6071", "Golden State Warriors", "LA Clippers"),
        ("00000000000000000000000000000abc", "Seattle SuperSonics", "Vancouver Grizzlies"),
    ]
    assert result.events[0].commence_time == datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
    (url, params, timeout), = session.calls
    assert url == "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
    assert params == {
        "commenceTimeFrom": "2026-10-21T12:00:00Z",
        "commenceTimeTo": "2026-10-22T12:00:00Z",
        "apiKey": SECRET,
    }
    assert timeout == 7


def test_event_odds_asks_for_the_us_region_five_markets_in_american_odds():
    session = Session(FakeResponse(200, load("event_odds_five_markets"), quota_headers(19990, 10, 5)))

    result = client_for(session).event_odds("9e6c2f0a1b7d4c3e8a5f6b7c8d9e0f12")

    (url, params, _), = session.calls
    assert url.endswith("/v4/sports/basketball_nba/events/9e6c2f0a1b7d4c3e8a5f6b7c8d9e0f12/odds")
    assert params["regions"] == "us"
    assert params["markets"] == (
        "player_points,player_rebounds,player_assists,player_threes,"
        "player_points_rebounds_assists"
    )
    assert params["oddsFormat"] == "american"
    assert (result.cost, result.quota.remaining, result.quota.used, result.quota.last_cost) == (
        5, 19990, 10, 5,
    )


def test_five_market_response_normalizes_to_allowlisted_quotes():
    parsed = parse_event_odds(load("event_odds_five_markets"))

    assert len(parsed.quotes) == 13
    assert {quote.book for quote in parsed.quotes} == {
        "betmgm", "draftkings", "fanduel", "williamhill_us",
    }
    lebron = sorted(
        (q.category, q.book, q.line, q.over, q.under)
        for q in parsed.quotes
        if q.player_name == "LeBron James"
    )
    assert lebron == [
        ("3PM", "draftkings", 2.5, 110, -135),
        ("AST", "draftkings", 7.5, 120, -150),
        ("PRA", "draftkings", 42.5, -110, -110),
        ("PTS", "betmgm", 28.5, 100, -125),
        ("PTS", "draftkings", 27.5, -115, -105),
        ("PTS", "fanduel", 27.5, -110, -110),
        ("PTS", "williamhill_us", 27.5, -105, None),
        ("REB", "draftkings", 7.5, -105, -115),
    ]
    assert {q.market_key for q in parsed.quotes} == {
        "player_points", "player_rebounds", "player_assists", "player_threes",
        "player_points_rebounds_assists",
    }


def test_offshore_books_are_dropped_by_the_allowlist():
    parsed = parse_event_odds(load("event_odds_five_markets"))

    assert parsed.dropped_books == frozenset({"bovada", "betonline_ag"})
    assert BOOK_ALLOWLIST == frozenset(
        {"draftkings", "fanduel", "betmgm", "williamhill_us", "betrivers", "fanatics"}
    )
    assert not {"bovada", "betonline_ag", "betus", "lowvig", "mybookie"} & BOOK_ALLOWLIST
    assert all(q.book in BOOK_ALLOWLIST for q in parsed.quotes)


def test_a_book_posting_two_distinct_lines_is_omitted_for_that_market():
    parsed = parse_event_odds(load("event_odds_five_markets"))

    assert parsed.multi_line_omitted == 1  # BetRivers: Reaves 14.5 and 15.5
    assert [q for q in parsed.quotes if q.book == "betrivers"] == []
    # The same player's single-line DraftKings quote is untouched.
    assert [
        (q.book, q.line) for q in parsed.quotes if q.player_name == "Austin Reaves"
    ] == [("draftkings", 15.5)]


def test_a_one_sided_quote_keeps_the_missing_side_none():
    parsed = parse_event_odds(load("event_odds_five_markets"))

    (quote,) = [q for q in parsed.quotes if q.book == "williamhill_us"]
    assert (quote.over, quote.under) == (-105, None)


def test_empty_response_is_free_and_has_no_quotes():
    session = Session(FakeResponse(200, load("event_odds_empty"), quota_headers(20000, 0, 0)))

    result = client_for(session).event_odds("9e6c2f0a1b7d4c3e8a5f6b7c8d9e0f12")

    assert (result.cost, len(result.parsed.quotes), result.quota.remaining) == (0, 0, 20000)


@pytest.mark.parametrize(
    "status, reason, halts",
    [
        (401, "unauthorized", True),
        (422, "invalid_request", True),
        (429, "rate_limited", False),
        (404, "event_not_found", False),
        (500, "server", False),
        (503, "server", False),
    ],
)
def test_error_statuses_map_to_closed_reasons(status, reason, halts):
    session = Session(FakeResponse(status, {"message": "nope"}))

    with pytest.raises(OddsApiError) as caught:
        client_for(session).event_odds("abc")

    assert (caught.value.reason, caught.value.status, caught.value.halts) == (
        reason, status, halts,
    )


def test_prices_that_are_not_integer_american_are_dropped():
    body = load("event_odds_five_markets")
    outcome = body["bookmakers"][0]["markets"][0]["outcomes"][0]
    outcome["price"] = 1.87  # a decimal price slipped through

    parsed = parse_event_odds(body)

    (quote,) = [
        q for q in parsed.quotes
        if q.book == "draftkings" and q.category == "PTS" and q.player_name == "LeBron James"
    ]
    assert (quote.over, quote.under) == (None, -105)


# -- the key never leaks ----------------------------------------------------


def test_a_failing_transport_never_puts_the_key_in_the_error():
    url = (
        "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
        f"?commenceTimeFrom=x&apiKey={SECRET}"
    )
    session = Session(error=requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}"))

    with pytest.raises(OddsApiError) as caught:
        client_for(session).list_events(*WINDOW)

    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert str(caught.value) == "Odds API transport for /v4/sports/basketball_nba/events"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("status", [401, 422, 429, 500, 418])
def test_no_error_status_puts_the_key_in_the_error(status):
    session = Session(FakeResponse(status, {"message": f"bad apiKey={SECRET}"}))

    with pytest.raises(OddsApiError) as caught:
        client_for(session).list_events(*WINDOW)

    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value)


def test_the_client_repr_hides_the_key():
    assert SECRET not in repr(client_for(Session()))


def test_http_library_request_logging_is_redacted(caplog):
    install_log_redaction()
    pool = logging.getLogger("urllib3.connectionpool")
    pool.setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
        pool.debug('%s://%s:%s "%s %s %s" %s %s', "https", "api.the-odds-api.com", 443,
                   "GET", f"/v4/sports/basketball_nba/events?apiKey={SECRET}&regions=us",
                   "HTTP/1.1", 200, None)

    assert SECRET not in caplog.text
    assert "apiKey=[REDACTED]&regions=us" in caplog.text


def test_urls_and_diagnostics_redact_only_the_key_value():
    url = f"https://api.the-odds-api.com/v4/x?regions=us&apiKey={SECRET}&markets=a"

    assert redact_url(url) == "https://api.the-odds-api.com/v4/x?regions=us&apiKey=[REDACTED]&markets=a"
    # The shared sanitizer is deliberately greedy after a credential key, so
    # it may redact the rest of the query too; what must hold is the key.
    assert _sanitize_diagnostic_detail(f"GET {url} failed") == (
        "GET https://api.the-odds-api.com/v4/x?regions=us&apiKey=[REDACTED] failed"
    )


def test_sentry_events_are_scrubbed_of_the_key():
    event = {
        "message": f"boom https://api.the-odds-api.com/v4/events?apiKey={SECRET}",
        "exception": {"values": [{"value": f"ConnectionError: ...?apiKey={SECRET}&x=1"}]},
    }

    scrubbed = scrub_event(event)

    assert SECRET not in repr(scrubbed)


def test_settings_repr_does_not_carry_the_key():
    from app.config.settings import OddsApiSettings, RuntimeSettings

    settings = RuntimeSettings(
        environment="testing", odds_api=OddsApiSettings(enabled=True, api_key=SECRET)
    )

    assert SECRET not in repr(settings) and SECRET not in str(settings.odds_api)


def test_an_unexpected_failure_in_a_beat_logs_without_the_key(tmp_path, caplog):
    from app.config.settings import OddsApiSettings
    from app.services.odds_collection import run_once
    from tests.support.odds_world import FakeClient, build_world

    world = build_world(tmp_path)
    coordinator = world.coordinator(
        FakeClient(), odds_settings=OddsApiSettings(enabled=True, api_key=SECRET)
    )

    def explode():
        raise RuntimeError(f"upstream said apiKey={SECRET} and also {SECRET}")

    coordinator.run = explode
    with caplog.at_level(logging.ERROR):
        result = run_once(coordinator)

    assert (result.status, result.reason) == ("failed", "unexpected_error")
    assert SECRET not in caplog.text
    assert "Odds API collection attempt failed" in caplog.text


def test_bookmaker_count_separates_an_empty_answer_from_an_all_omitted_snapshot():
    omitted = load("event_odds_empty")
    omitted["bookmakers"] = [load("event_odds_five_markets")["bookmakers"][4]]  # BetRivers: two lines

    assert parse_event_odds(load("event_odds_empty")).bookmaker_count == 0
    snapshot = parse_event_odds(omitted)
    assert (len(snapshot.quotes), snapshot.multi_line_omitted, snapshot.bookmaker_count) == (0, 1, 1)
    assert parse_event_odds(load("event_odds_five_markets")).bookmaker_count == 7
