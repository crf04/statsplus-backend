"""Offline tests for the Kalshi DFS snapshot adapter.

The recorded fixtures in ``tests/fixtures/kalshi`` come from Kalshi's public
API, which needs no key.  No NBA player prop was open when they were recorded
(preseason), so the markets are the settled ladders of New York at San Antonio
on 2026-06-13, reopened:

* ``markets.<SERIES>.json``: ``GET /historical/markets?event_ticker=
  <SERIES>-26JUN13NYKSAS&limit=200`` for KXNBAPTS, KXNBAREB, KXNBAAST,
  KXNBA3PT, KXNBABLK and KXNBASTL, reduced to Victor Wembanyama and Jalen
  Brunson (and Karl-Anthony Towns for blocks).  ``status`` is ``active`` and
  ``result`` is empty; the settlement fields are dropped; every book price is
  replaced because a settled book is the empty 0.0000/1.0000.  Everything else
  is the recorded object.
* ``open_games.json``: ``GET /events/KXNBAGAME-26JUN13NYKSAS`` and
  ``GET /milestones?limit=200&related_event_ticker=KXNBAGAME-26JUN13NYKSAS``,
  assembled in the shape of ``GET /events?series_ticker=KXNBAGAME&status=open&
  with_milestones=true``; the milestone's ``details`` are reduced to the fields
  the adapter reads and its status is ``scheduled``.

Tests that need a ladder the recording does not have build it from one real
market object, changing only the fields named in the test.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    CoverageCode,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PriceKind,
    PriceScope,
    RetrievalContext,
    ScoringPeriod,
    SelectionDirection,
    SnapshotStatus,
)
from app.providers.kalshi import SERIES_STATISTIC_LABELS, KalshiAdapter
from app.providers.registry import DFSProviderRuntime, build_dfs_provider
from app.services.athlete_resolver import AthleteResolver, MappingResolutionState
from app.services.event_resolver import (
    DEFAULT_EVENT_MATCH_WINDOW,
    EventResolutionState,
    EventResolver,
)
from app.utils import telemetry

FIXTURES = Path(__file__).parents[1] / "fixtures" / "kalshi"
BASE = "https://api.elections.kalshi.com/trade-api/v2"
EVENT_TICKER = "KXNBAPTS-26JUN13NYKSAS"
WEMBANYAMA_ID = "b269496a-6cdf-466d-aa1a-c648b20591ae"
SAS_ID = "ad36c3e8-4194-4e63-920f-7c50f46191a6"
NYK_ID = "67468ecc-b868-43a4-b9dc-751a52894bb0"
TIP_OFF = datetime(2026, 6, 14, 0, 30, tzinfo=UTC)
ESTIMATED_START = datetime(2026, 6, 14, 3, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _empty_page() -> dict[str, Any]:
    return {"cursor": "", "markets": []}


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class FakeSession:
    """Answer each GET from the next queued payload, recording the call."""

    def __init__(self, *payloads: object) -> None:
        self.queue = list(payloads)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


def _recorded_session() -> FakeSession:
    return FakeSession(
        _fixture("open_games.json"),
        *(_fixture(f"markets.{series}.json") for series in SERIES_STATISTIC_LABELS),
    )


def _snapshot(session: FakeSession, query: NBAMarketQuery | None = None):
    return KalshiAdapter(session=session, now=lambda: datetime(2026, 6, 13, 18, tzinfo=UTC)).get_snapshot(
        query or NBAMarketQuery(),
        RetrievalContext(deadline="2030-01-01T00:00:00Z", request_id="kalshi-test"),
    )


def _base_market() -> dict[str, Any]:
    markets = _fixture("markets.KXNBAPTS.json")["markets"]
    return deepcopy(next(m for m in markets if m["ticker"].endswith("SASVWEMBANYAMA1-30")))


def _rung(
    line: float,
    *,
    bid: str,
    ask: str,
    no_ask: str | None = None,
    suffix: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One real market object moved to another line and book."""

    market = _base_market()
    market["floor_strike"] = line
    market["ticker"] = f"{EVENT_TICKER}-SASVWEMBANYAMA1-{suffix or int(line + 0.5)}"
    market["yes_bid_dollars"] = bid
    market["yes_ask_dollars"] = ask
    market["no_ask_dollars"] = no_ask if no_ask is not None else "0.5000"
    market["no_bid_dollars"] = "0.4000"
    market.update(overrides)
    return market


def _ladder_snapshot(*rungs: dict[str, Any]):
    """A snapshot of one game whose points ladder is exactly ``rungs``."""

    return _snapshot(
        FakeSession(
            _fixture("open_games.json"),
            {"cursor": "", "markets": list(rungs)},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
        )
    )


def _lines_by_variant(snapshot) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"standard": [], "alternate": []}
    for market in sorted(snapshot.markets, key=lambda m: m.threshold.value):
        result[market.variant.value].append(str(market.threshold.value))
    return result


# -- the recorded game --------------------------------------------------------


def test_recorded_game_requests_the_open_games_then_each_player_series():
    session = _recorded_session()

    snapshot = _snapshot(session)

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.fetched_count == 50
    assert len(snapshot.markets) == 50
    assert session.calls == [
        (
            f"{BASE}/events",
            {
                "series_ticker": "KXNBAGAME",
                "status": "open",
                "with_milestones": "true",
                "limit": 200,
            },
        ),
        *(
            (
                f"{BASE}/markets",
                {"series_ticker": series, "status": "open", "limit": 1000},
            )
            for series in (
                "KXNBAPTS",
                "KXNBAREB",
                "KXNBAAST",
                "KXNBA3PT",
                "KXNBABLK",
                "KXNBASTL",
            )
        ),
    ]
    operations = [e["operation"] for e in telemetry.get_recorded_provider_events()]
    assert operations == ["open_games"] + ["series_markets"] * 6
    assert {e["provider"] for e in telemetry.get_recorded_provider_events()} == {"kalshi"}


def test_recorded_market_states_athlete_team_event_line_and_period():
    snapshot = _snapshot(_recorded_session())
    market = next(m for m in snapshot.markets if m.market_id == "KXNBAPTS-26JUN13NYKSAS-SASVWEMBANYAMA1-30")

    assert market.provider == "kalshi"
    assert market.athlete.provider_id == WEMBANYAMA_ID
    assert market.athlete.name == "Victor Wembanyama"
    assert market.athlete.team.abbreviation == "SAS"
    assert market.athlete.team.provider_id == SAS_ID
    assert market.team.abbreviation == "SAS"
    assert market.opponent.abbreviation == "NYK"
    assert market.opponent.provider_id == NYK_ID
    assert market.statistic.label == "Points"
    assert market.threshold.value == Decimal("29.5")
    assert market.threshold.original_value == "29.5"
    assert market.threshold.unit == "count"
    assert market.status is MarketStatus.AVAILABLE
    assert market.status_label == "active"
    assert market.scoring_period is ScoringPeriod.FULL_GAME
    assert market.scoring_period_label is None
    assert market.starts_at == TIP_OFF
    assert market.updated_at == datetime(2026, 6, 14, 4, 6, 23, 585956, tzinfo=UTC)


def test_event_evidence_orders_away_then_home_as_the_ticker_does():
    """26JUN13NYKSAS is New York at San Antonio, so San Antonio is home.

    ESPN lists that game (tip-off 2026-06-14T00:30Z) with San Antonio at home,
    and Kalshi's own event title reads "New York at San Antonio".
    """

    event = _snapshot(_recorded_session()).markets[0].event

    assert event.provider_id == "KXNBAGAME-26JUN13NYKSAS"
    assert event.label == "New York at San Antonio"
    assert event.status_label == "scheduled"
    assert event.starts_at == TIP_OFF
    assert event.away_team.abbreviation == "NYK"
    assert event.away_team.provider_id == NYK_ID
    assert event.home_team.abbreviation == "SAS"
    assert event.home_team.provider_id == SAS_ID


def test_a_new_york_player_has_new_york_as_team_and_san_antonio_as_opponent():
    snapshot = _snapshot(_recorded_session())
    market = next(m for m in snapshot.markets if m.athlete.name == "Jalen Brunson")

    assert market.athlete.team.abbreviation == "NYK"
    assert market.opponent.abbreviation == "SAS"


def test_every_series_states_the_label_the_statistic_catalog_maps():
    snapshot = _snapshot(_recorded_session())

    assert sorted({m.statistic.label for m in snapshot.markets}) == [
        "Assists",
        "Blocks",
        "Points",
        "Rebounds",
        "Steals",
        "Three Pointers",
    ]


# -- standard line (D3) -------------------------------------------------------


def test_each_recorded_ladder_has_exactly_the_line_whose_over_midpoint_is_nearest_half():
    snapshot = _snapshot(_recorded_session())

    standard = {
        (m.statistic.label, m.athlete.name): str(m.threshold.value)
        for m in snapshot.markets
        if m.variant is MarketVariant.STANDARD
    }

    assert standard == {
        ("Points", "Victor Wembanyama"): "29.5",
        ("Points", "Jalen Brunson"): "34.5",
        ("Rebounds", "Victor Wembanyama"): "9.5",
        ("Rebounds", "Jalen Brunson"): "5.5",
        ("Assists", "Victor Wembanyama"): "3.5",
        ("Assists", "Jalen Brunson"): "5.5",
        ("Three Pointers", "Victor Wembanyama"): "1.5",
        ("Three Pointers", "Jalen Brunson"): "2.5",
        ("Blocks", "Victor Wembanyama"): "3.5",
        ("Blocks", "Karl-Anthony Towns"): "2.5",
        ("Steals", "Victor Wembanyama"): "1.5",
        ("Steals", "Jalen Brunson"): "2.5",
    }
    alternates = [m for m in snapshot.markets if m.variant is MarketVariant.ALTERNATE]
    assert len(alternates) == 38
    assert {m.variant_label for m in alternates} == {"alternate"}


def test_a_tie_between_two_lines_goes_to_the_lower_line():
    # Over midpoints 0.40 and 0.60 are both 0.10 from 0.5.
    snapshot = _ladder_snapshot(
        _rung(24.5, bid="0.5900", ask="0.6100"),
        _rung(29.5, bid="0.3900", ask="0.4100"),
    )

    assert _lines_by_variant(snapshot) == {
        "standard": ["24.5"],
        "alternate": ["29.5"],
    }


def test_a_tie_is_broken_by_line_not_by_the_order_the_venue_lists_them():
    snapshot = _ladder_snapshot(
        _rung(29.5, bid="0.3900", ask="0.4100"),
        _rung(24.5, bid="0.5900", ask="0.6100"),
    )

    assert _lines_by_variant(snapshot) == {
        "standard": ["24.5"],
        "alternate": ["29.5"],
    }


def test_a_line_without_both_bid_and_ask_cannot_be_the_standard_line():
    # 24.5 has an ask of 0.50 but no bid, so it has no midpoint; 29.5's
    # midpoint of 0.15 is the only one and is chosen.
    snapshot = _ladder_snapshot(
        _rung(24.5, bid="0.0000", ask="0.5000"),
        _rung(29.5, bid="0.1000", ask="0.2000"),
    )

    assert _lines_by_variant(snapshot) == {
        "standard": ["29.5"],
        "alternate": ["24.5"],
    }


def test_with_no_midpoint_a_single_line_is_standard():
    snapshot = _ladder_snapshot(
        _rung(29.5, bid="0.0000", ask="1.0000", no_ask="1.0000"),
    )

    assert _lines_by_variant(snapshot) == {"standard": ["29.5"], "alternate": []}


def test_with_no_midpoint_several_lines_are_all_alternate():
    empty = {"bid": "0.0000", "ask": "1.0000", "no_ask": "1.0000"}

    snapshot = _ladder_snapshot(
        _rung(24.5, **empty),
        _rung(29.5, **empty),
    )

    assert _lines_by_variant(snapshot) == {
        "standard": [],
        "alternate": ["24.5", "29.5"],
    }


def test_a_ladder_split_across_pages_still_has_one_standard_line():
    first = {
        "cursor": "page-2",
        "markets": [_rung(24.5, bid="0.7800", ask="0.8000")],
    }
    second = {
        "cursor": "",
        "markets": [_rung(29.5, bid="0.4800", ask="0.5000")],
    }
    session = FakeSession(
        _fixture("open_games.json"),
        first,
        second,
        *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
    )

    snapshot = _snapshot(session)

    assert _lines_by_variant(snapshot) == {
        "standard": ["29.5"],
        "alternate": ["24.5"],
    }
    assert [call[1].get("cursor") for call in session.calls[1:3]] == [None, "page-2"]


def test_ladders_are_chosen_per_game_statistic_and_player():
    other_player = _rung(24.5, bid="0.4800", ask="0.5000")
    other_player["ticker"] = f"{EVENT_TICKER}-SASDCASTLE5-25"
    other_player["title"] = "Stephon Castle: 25+ points"
    other_player["custom_strike"] = {
        "basketball_player": "player-castle",
        "basketball_team": SAS_ID,
    }

    snapshot = _ladder_snapshot(
        _rung(29.5, bid="0.4800", ask="0.5000"),
        other_player,
    )

    assert sorted((m.athlete.name, m.variant.value) for m in snapshot.markets) == [
        ("Stephon Castle", "standard"),
        ("Victor Wembanyama", "standard"),
    ]


def test_two_games_of_one_athlete_and_statistic_each_get_their_own_standard_line():
    june_13 = [
        _rung(24.5, bid="0.7800", ask="0.8000"),
        _rung(29.5, bid="0.4800", ask="0.5000"),
    ]
    june_16 = [
        _rung(
            line,
            bid=bid,
            ask=ask,
            ticker=f"KXNBAPTS-26JUN16SASNYK-SASVWEMBANYAMA1-{suffix}",
            event_ticker="KXNBAPTS-26JUN16SASNYK",
        )
        for line, bid, ask, suffix in (
            (24.5, "0.4800", "0.5000", 25),
            (29.5, "0.1800", "0.2000", 30),
        )
    ]

    snapshot = _ladder_snapshot(*june_13, *june_16)

    assert sorted(
        (m.event.provider_id, str(m.threshold.value), m.variant.value) for m in snapshot.markets
    ) == [
        ("KXNBAGAME-26JUN13NYKSAS", "24.5", "alternate"),
        ("KXNBAGAME-26JUN13NYKSAS", "29.5", "standard"),
        ("KXNBAGAME-26JUN16SASNYK", "24.5", "standard"),
        ("KXNBAGAME-26JUN16SASNYK", "29.5", "alternate"),
    ]


def _conflicting_duplicates(order):
    a = _rung(24.5, bid="0.4000", ask="0.8000")
    b = _rung(29.5, bid="0.4500", ask="0.6500")
    duplicate = _rung(24.5, bid="0.2000", ask="0.8000")
    rows = {"duplicate_last": [a, b, duplicate], "duplicate_first": [duplicate, b, a]}
    return _ladder_snapshot(*rows[order])


@pytest.mark.parametrize("order", ["duplicate_last", "duplicate_first"])
def test_a_repeated_ticker_with_a_different_bid_is_excluded_in_either_order(order):
    snapshot = _conflicting_duplicates(order)

    assert _lines_by_variant(snapshot) == {"standard": ["29.5"], "alternate": []}
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.skipped_reasons == (CoverageCode.CONFLICTING_SOURCE_IDENTITY,)


def test_an_identical_repeated_row_is_one_market():
    row = _rung(29.5, bid="0.4800", ask="0.5000")

    snapshot = _ladder_snapshot(row, deepcopy(row))

    assert [str(m.threshold.value) for m in snapshot.markets] == ["29.5"]
    assert snapshot.status is SnapshotStatus.COMPLETE


# -- prices (D1, D2) ----------------------------------------------------------


def test_over_is_yes_and_under_is_no_each_priced_at_one_over_its_ask():
    snapshot = _ladder_snapshot(
        _rung(29.5, bid="0.4800", ask="0.5000", no_ask="0.5200"),
    )
    over, under = snapshot.markets[0].selections

    assert over.selection_id == "KXNBAPTS-26JUN13NYKSAS-SASVWEMBANYAMA1-30:yes"
    assert over.direction is SelectionDirection.HIGHER
    assert over.label == "Over"
    assert over.price_kind is PriceKind.DECIMAL
    assert over.price_scope is PriceScope.SELECTION
    assert over.decimal_price == Decimal("2")
    assert over.price_value == Decimal("2")
    assert under.selection_id == "KXNBAPTS-26JUN13NYKSAS-SASVWEMBANYAMA1-30:no"
    assert under.direction is SelectionDirection.LOWER
    assert under.label == "Under"
    assert under.price_kind is PriceKind.DECIMAL
    assert under.price_scope is PriceScope.SELECTION
    assert under.decimal_price == Decimal("1.923076923076923076923076923")
    assert snapshot.markets[0].is_priced


def test_recorded_prices_are_one_over_each_side_ask():
    snapshot = _snapshot(_recorded_session())
    market = next(m for m in snapshot.markets if m.market_id == "KXNBAPTS-26JUN13NYKSAS-NYKJBRUNSON11-40")

    # Recorded book: yes ask 0.2500, no ask 0.7700.
    assert [s.decimal_price for s in market.selections] == [
        Decimal("4"),
        Decimal("1.298701298701298701298701299"),
    ]


def test_an_empty_ask_leaves_both_sides_unpriced():
    # The Under's ask is 1.0000, which Kalshi uses for "nobody is offering".
    snapshot = _ladder_snapshot(
        _rung(29.5, bid="0.4800", ask="0.5000", no_ask="1.0000"),
    )
    market = snapshot.markets[0]

    assert [s.price_kind for s in market.selections] == [
        PriceKind.UNPRICED,
        PriceKind.UNPRICED,
    ]
    assert [s.decimal_price for s in market.selections] == [None, None]
    assert not market.is_priced
    # The market is still evidence of the line, and still the ladder's standard.
    assert market.variant is MarketVariant.STANDARD


def test_a_missing_yes_ask_leaves_both_sides_unpriced():
    rung = _rung(29.5, bid="0.4800", ask="0.5000", no_ask="0.5200")
    del rung["yes_ask_dollars"]

    market = _ladder_snapshot(rung).markets[0]

    assert not market.is_priced


# -- scope (D4) ---------------------------------------------------------------


def _skipped(snapshot_or_error) -> tuple[Any, ...]:
    return snapshot_or_error.coverage.skipped_reasons


def _with_one_excluded_row(row: dict[str, Any]):
    return _ladder_snapshot(_rung(29.5, bid="0.4800", ask="0.5000"), row)


def test_a_market_of_another_series_is_excluded_as_a_non_projection_market():
    row = _rung(29.5, bid="0.4800", ask="0.5000", suffix="other")
    row["event_ticker"] = "KXNBA1HPTS-26JUN13NYKSAS"

    snapshot = _with_one_excluded_row(row)

    assert len(snapshot.markets) == 1
    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.skipped_count == 1
    assert _skipped(snapshot) == (CoverageCode.NON_PROJECTION_MARKET,)


@pytest.mark.parametrize(
    "event_ticker",
    ["KXNBAH2HPTS-26JUN13LKORNET7JALVARADO5", "KXNBAFINMVP-26"],
)
def test_head_to_head_and_season_long_tickers_are_excluded_not_malformed(event_ticker):
    row = _rung(29.5, bid="0.4800", ask="0.5000", suffix="x")
    row["event_ticker"] = event_ticker

    snapshot = _with_one_excluded_row(row)

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 1
    assert snapshot.coverage.skipped_count == 1
    assert _skipped(snapshot) == (CoverageCode.NON_PROJECTION_MARKET,)


@pytest.mark.parametrize(
    "overrides",
    [{"market_type": "scalar"}, {"strike_type": "greater"}],
)
def test_a_market_that_is_not_a_structured_binary_ladder_rung_is_excluded(overrides):
    snapshot = _with_one_excluded_row(_rung(29.5, bid="0.4800", ask="0.5000", suffix="x", **overrides))

    assert len(snapshot.markets) == 1
    assert _skipped(snapshot) == (CoverageCode.NON_PROJECTION_MARKET,)


def test_a_market_with_no_player_is_excluded_as_a_non_player_market():
    row = _rung(29.5, bid="0.4800", ask="0.5000", suffix="team")
    row["custom_strike"] = {"basketball_team": SAS_ID}

    snapshot = _with_one_excluded_row(row)

    assert len(snapshot.markets) == 1
    assert _skipped(snapshot) == (CoverageCode.NON_PLAYER_MARKET,)


@pytest.mark.parametrize(
    "status",
    ["initialized", "closed", "determined", "disputed", "amended", "finalized"],
)
def test_a_market_that_is_not_open_or_paused_is_excluded_by_status(status):
    snapshot = _with_one_excluded_row(_rung(29.5, bid="0.4800", ask="0.5000", suffix="x", status=status))

    assert len(snapshot.markets) == 1
    assert _skipped(snapshot) == (CoverageCode.INELIGIBLE_STATUS,)


def test_an_inactive_market_is_retained_as_suspended_and_filterable():
    rung = _rung(29.5, bid="0.4800", ask="0.5000", status="inactive")

    suspended = _ladder_snapshot(rung).markets[0]
    only_available = _snapshot(
        FakeSession(
            _fixture("open_games.json"),
            {"cursor": "", "markets": [rung]},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
        ),
        NBAMarketQuery(market_statuses=(MarketStatus.AVAILABLE,)),
    )

    assert suspended.status is MarketStatus.SUSPENDED
    assert suspended.status_label == "inactive"
    assert only_available.markets == ()
    assert only_available.coverage.skipped_reasons == (CoverageCode.STATUS_FILTER,)


def test_a_game_the_milestone_says_is_live_is_excluded_as_ineligible_event():
    games = _fixture("open_games.json")
    games["milestones"][0]["details"]["status"] = "live"

    snapshot = _snapshot(
        FakeSession(
            games,
            {"cursor": "", "markets": [_rung(29.5, bid="0.4800", ask="0.5000")]},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
        )
    )

    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_reasons == (CoverageCode.INELIGIBLE_EVENT_STATUS,)


# -- start time and the event resolver ----------------------------------------


def test_without_the_game_milestone_the_estimated_start_is_kept_within_the_window():
    games = _fixture("open_games.json")
    games["milestones"] = []

    snapshot = _snapshot(
        FakeSession(
            games,
            {"cursor": "", "markets": [_rung(29.5, bid="0.4800", ask="0.5000")]},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
        )
    )
    market = snapshot.markets[0]

    assert market.starts_at == ESTIMATED_START
    assert market.event.starts_at == ESTIMATED_START
    assert market.event.label == "NYK at SAS"
    assert market.event.home_team.abbreviation == "SAS"
    assert market.event.home_team.provider_id is None
    assert ESTIMATED_START - TIP_OFF == timedelta(hours=3)
    assert ESTIMATED_START - TIP_OFF <= DEFAULT_EVENT_MATCH_WINDOW


def test_a_failed_open_games_lookup_falls_back_to_estimates_and_is_partial():
    snapshot = _snapshot(
        FakeSession(
            requests.ConnectionError("down"),
            {"cursor": "", "markets": [_rung(29.5, bid="0.4800", ask="0.5000")]},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
        )
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.markets[0].starts_at == ESTIMATED_START
    assert snapshot.coverage.warning_codes == (CoverageCode.FIXTURE_LIST_FAILED,)
    assert not snapshot.coverage.is_complete


class _Catalog:
    """One scheduled game, in the two reads the event resolver needs."""

    def get_events(self, season: str):
        if season != "2025-26":
            return []
        return [
            {
                "nba_game_id": "0042500405",
                "season": "2025-26",
                "scheduled_at": TIP_OFF,
                "home_team_id": 1610612759,
                "home_team_name": "San Antonio Spurs",
                "home_team_tricode": "SAS",
                "away_team_id": 1610612752,
                "away_team_name": "New York Knicks",
                "away_team_tricode": "NYK",
                "status_text": "Scheduled",
                "status_code": 1,
                "postponed_status": None,
                "postponement_evidence": None,
                "is_postponed": False,
                "classification": "Playoffs",
                "first_seen_at": TIP_OFF,
                "last_seen_at": TIP_OFF,
            }
        ]

    def get_freshness(self, season: str):
        return {"season": season, "fresh": True, "last_success_at": TIP_OFF.isoformat()}


@pytest.mark.parametrize("estimated", [False, True])
def test_the_event_resolver_maps_the_game_with_either_start_time(estimated):
    games = _fixture("open_games.json")
    if estimated:
        games["milestones"] = []
    snapshot = _snapshot(
        FakeSession(
            games,
            {"cursor": "", "markets": [_rung(29.5, bid="0.4800", ask="0.5000")]},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 1)),
        )
    )

    resolution = EventResolver(_Catalog()).resolve_market(snapshot.markets[0], "2025-26")

    assert resolution.state is EventResolutionState.AUTO
    assert resolution.canonical_event.nba_game_id == "0042500405"


def _athlete_row(player_id, name, team_id, abbreviation):
    return {
        "season": "2025-26",
        "player_id": player_id,
        "display_name": name,
        "roster_status": "active",
        "is_active": True,
        "is_active_for_season": True,
        "team_id": team_id,
        "team_name": None,
        "team_abbreviation": abbreviation,
        "published_at": TIP_OFF,
    }


class _AthleteCatalog:
    def __init__(self, rows):
        self.rows = rows

    def get_catalog(self, season, *, active_only=False):
        return [r for r in self.rows if r["season"] == season]


def test_recorded_markets_resolve_to_canonical_athletes_and_the_game_through_the_resolvers():
    """The calls dfs_board makes: ``resolve_market(market, season, observed_at=)``."""

    snapshot = _snapshot(_recorded_session())
    athletes = AthleteResolver(
        _AthleteCatalog(
            [
                _athlete_row(1641705, "Victor Wembanyama", 1610612759, "SAS"),
                _athlete_row(1628973, "Jalen Brunson", 1610612752, "NYK"),
                _athlete_row(1626157, "Karl-Anthony Towns", 1610612752, "NYK"),
            ]
        )
    )
    events = EventResolver(_Catalog())

    resolved = {}
    for market in snapshot.markets:
        athlete = athletes.resolve_market(market, "2025-26", observed_at=snapshot.retrieved_at)
        event = events.resolve_market(market, "2025-26", observed_at=snapshot.retrieved_at)
        assert athlete.state is MappingResolutionState.AUTO
        assert event.state is EventResolutionState.AUTO
        resolved[market.athlete.name] = (
            athlete.canonical_athlete.player_id,
            event.canonical_event.nba_game_id,
        )

    assert resolved == {
        "Victor Wembanyama": (1641705, "0042500405"),
        "Jalen Brunson": (1628973, "0042500405"),
        "Karl-Anthony Towns": (1626157, "0042500405"),
    }


# -- coverage and failures ----------------------------------------------------


def test_an_empty_venue_is_a_complete_empty_snapshot():
    snapshot = _snapshot(
        FakeSession(
            {"cursor": "", "events": [], "milestones": []},
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS))),
        )
    )

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.fetched_count == 0


def test_one_failed_series_makes_the_snapshot_partial_with_the_others_kept():
    snapshot = _snapshot(
        FakeSession(
            _fixture("open_games.json"),
            _fixture("markets.KXNBAPTS.json"),
            requests.ConnectionError("down"),
            *(_empty_page() for _ in range(len(SERIES_STATISTIC_LABELS) - 2)),
        )
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 8
    assert CoverageCode.PAGE_FETCH_FAILED in snapshot.coverage.warning_codes
    assert not snapshot.coverage.pagination_complete


def test_every_series_failing_is_one_typed_unavailable_error():
    session = FakeSession(
        *(requests.ConnectionError("down") for _ in range(7)),
    )

    with pytest.raises(ProviderUnavailableError):
        _snapshot(session)
    assert len(session.calls) == 7


def test_a_malformed_row_is_skipped_with_typed_coverage():
    broken = _rung(29.5, bid="0.4800", ask="0.5000", suffix="broken")
    broken["floor_strike"] = "not-a-number"

    snapshot = _with_one_excluded_row(broken)

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 1
    assert snapshot.coverage.skipped_reasons == (CoverageCode.MALFORMED_RECORD,)
    assert snapshot.coverage.diagnostic_details == ("floor_strike must be numeric",)


@pytest.mark.parametrize("price", ["1.5000", "-0.1000", "free"])
def test_a_book_price_outside_zero_to_one_is_a_malformed_row(price):
    snapshot = _with_one_excluded_row(_rung(29.5, bid="0.4800", ask=price, suffix="bad"))

    assert len(snapshot.markets) == 1
    assert snapshot.coverage.skipped_reasons == (CoverageCode.MALFORMED_RECORD,)


def test_a_payload_that_is_not_a_market_page_is_an_invalid_response():
    session = FakeSession(
        _fixture("open_games.json"),
        *({"unexpected": True} for _ in range(len(SERIES_STATISTIC_LABELS))),
    )

    with pytest.raises(ProviderUnavailableError, match="Kalshi returned an invalid response"):
        _snapshot(session)


def test_the_registry_builds_the_kalshi_adapter_with_the_runtime_timeouts():
    adapter = build_dfs_provider("kalshi", DFSProviderRuntime())

    assert isinstance(adapter, KalshiAdapter)
    assert adapter.name == "kalshi"
    assert adapter.timeout == (3.0, 8.0)


def test_the_statistic_catalog_maps_every_kalshi_label_to_its_market_category():
    from app.services.statistic_catalog import StatisticCatalog

    catalog = StatisticCatalog.load(StatisticCatalog.DEFAULT_PATH)
    mapped = {
        label: next(
            statistic.market_category
            for statistic in catalog.statistics
            if label in statistic.provider_mappings.get("kalshi", ())
        )
        for label in SERIES_STATISTIC_LABELS.values()
    }

    assert mapped == {
        "Points": "PTS",
        "Rebounds": "REB",
        "Assists": "AST",
        "Three Pointers": "3PM",
        "Blocks": "BLK",
        "Steals": "STL",
    }
