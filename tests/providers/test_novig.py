"""Offline tests for the Novig DFS snapshot adapter.

The recorded payloads are real Novig responses (see ``novig_recorded``); the
ladder tests below build their markets from one recorded row so the structure
stays Novig's while the bids are chosen to pin one rule each.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

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
from app.providers.novig import PLAYER_STATISTIC_MARKET_TYPES, NovigAdapter, _Pacer
from app.providers.registry import DFSProviderRuntime, build_dfs_provider
from app.services.statistic_catalog import MatchState, StatisticCatalog
from app.utils import telemetry
from tests.providers.novig_recorded import (
    NYK_EVENT,
    PHI_EVENT,
    ReplaySession,
    adapter,
    load,
    market_row,
)

BARNES_24_5 = "01a1406c-867f-7342-a5a7-2744d6b4f51a"
KAWHI_POINTS_29_5 = "01a1406c-867f-7342-a5a7-2654a74d9a06"
BARNES_POINTS_19_5 = "01a1405e-4d7a-7362-9d7a-5cd2767a45cb"


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def _context() -> RetrievalContext:
    return RetrievalContext(deadline=datetime(2030, 1, 1, tzinfo=timezone.utc))


def _snapshot(novig: NovigAdapter | None = None, query: NBAMarketQuery | None = None):
    return (novig or adapter()).get_snapshot(query or NBAMarketQuery(), _context())


def _market(snapshot, market_id: str):
    return next(m for m in snapshot.markets if m.market_id == market_id)


# ---------------------------------------------------------------- ladders


def _order(price: str) -> list[dict]:
    return [{"orderId": "recorded-order", "price": price, "qty": 1000}]


def _ladder(lines: dict[str, tuple[str, str] | None]):
    """One player's POINTS ladder from a recorded row.

    ``lines`` maps a strike to its (best Over bid, best Under bid); a missing
    side is None inside the pair, and None for the whole line means the book
    fetch fails.  Returns a replay session and the market id of each strike.
    """

    template = market_row(BARNES_24_5)
    items, books, ids, failures = [], {}, {}, {}
    for strike, bids in lines.items():
        market_id = f"ladder-{strike}"
        over_id, under_id = f"over-{strike}", f"under-{strike}"
        row = deepcopy(template)
        row["marketId"] = market_id
        row["description"] = f"Scottie Barnes {strike} POINTS"
        row["strike"] = strike
        row["outcomes"] = [
            {"outcomeId": over_id, "name": f"Over {strike}", "status": "TBD"},
            {"outcomeId": under_id, "name": f"Under {strike}", "status": "TBD"},
        ]
        items.append(row)
        ids[strike] = market_id
        if bids is None:
            failures[f"/markets/{market_id}/book"] = 403
            continue
        orders = {}
        if bids[0] is not None:
            orders[over_id] = _order(bids[0])
        if bids[1] is not None:
            orders[under_id] = _order(bids[1])
        books[market_id] = {"marketId": market_id, "seq": 1, "orders": orders}
    session = ReplaySession(
        pages=[{"items": items}],
        events={NYK_EVENT: load("events.json")[NYK_EVENT]},
        books=books,
        failures=failures,
    )
    return session, ids


def _variants(snapshot, ids: dict[str, str]) -> dict[str, str]:
    return {
        strike: _market(snapshot, market_id).variant.value
        for strike, market_id in ids.items()
    }


def test_the_standard_line_is_the_one_whose_over_midpoint_is_nearest_one_half():
    # Over midpoint = (best Over bid + (1 - best Under bid)) / 2:
    #   19.5 -> (0.15 + 0.50) / 2 = 0.325
    #   24.5 -> (0.40 + 0.55) / 2 = 0.475
    #   29.5 -> (0.55 + 0.70) / 2 = 0.625
    session, ids = _ladder(
        {"19.5": ("0.15", "0.50"), "24.5": ("0.40", "0.45"), "29.5": ("0.55", "0.30")}
    )

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert _variants(snapshot, ids) == {
        "19.5": "alternate",
        "24.5": "standard",
        "29.5": "alternate",
    }
    assert {m.variant_label for m in snapshot.markets} == {"standard", "alternate"}


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # Both midpoints are exactly 0.5.
        (
            {"24.5": ("0.50", "0.50"), "19.5": ("0.40", "0.40")},
            {"19.5": "standard", "24.5": "alternate"},
        ),
        # Midpoints 0.45 and 0.55 are the same distance from 0.5.
        (
            {"19.5": ("0.50", "0.40"), "14.5": ("0.35", "0.45")},
            {"14.5": "standard", "19.5": "alternate"},
        ),
    ],
)
def test_a_tie_between_midpoints_goes_to_the_lower_line(lines, expected):
    session, ids = _ladder(lines)

    assert _variants(_snapshot(adapter(session)), ids) == expected


def test_a_single_line_without_a_midpoint_is_standard_and_unpriced():
    session, ids = _ladder({"24.5": (None, "0.55")})

    snapshot = _snapshot(adapter(session))
    market = _market(snapshot, ids["24.5"])

    assert market.variant is MarketVariant.STANDARD
    assert market.is_priced is False
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_several_lines_without_any_midpoint_are_all_alternate():
    session, ids = _ladder(
        {"19.5": (None, "0.55"), "24.5": ("0.10", None), "29.5": (None, None)}
    )

    snapshot = _snapshot(adapter(session))

    assert _variants(snapshot, ids) == {
        "19.5": "alternate",
        "24.5": "alternate",
        "29.5": "alternate",
    }


def test_a_failed_book_makes_the_ladder_undetermined_so_no_line_is_standard():
    session, ids = _ladder(
        {"19.5": ("0.40", "0.40"), "24.5": None, "29.5": ("0.20", "0.60")}
    )

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert _variants(snapshot, ids) == {
        "19.5": "alternate",
        "24.5": "alternate",
        "29.5": "alternate",
    }
    # The books that were read still price their markets.
    assert _market(snapshot, ids["19.5"]).is_priced is True
    assert _market(snapshot, ids["24.5"]).is_priced is False


def test_one_line_is_standard_even_when_its_book_failed_and_the_snapshot_is_partial():
    session, ids = _ladder({"24.5": None})

    snapshot = _snapshot(adapter(session))
    market = _market(snapshot, ids["24.5"])

    assert market.variant is MarketVariant.STANDARD
    assert market.is_priced is False
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.fanout_complete is False
    assert snapshot.coverage.warning_codes == (CoverageCode.FIXTURE_FAILED,)
    assert snapshot.coverage.diagnostic_details == (
        "novig book unavailable for 1 of 1 markets",
    )


# ----------------------------------------------------------------- prices


def test_selection_prices_are_one_over_the_ask_of_that_side():
    snapshot = _snapshot()

    # Over bid 0.03 and Under bid 0.60: Over ask 1 - 0.60, Under ask 1 - 0.03.
    kawhi = _market(snapshot, KAWHI_POINTS_29_5)
    assert [
        (s.direction, s.price_kind, s.price_value, s.price_scope)
        for s in kawhi.selections
    ] == [
        (SelectionDirection.HIGHER, PriceKind.DECIMAL, Decimal("2.5"), PriceScope.SELECTION),
        (
            SelectionDirection.LOWER,
            PriceKind.DECIMAL,
            Decimal("1.030928"),
            PriceScope.SELECTION,
        ),
    ]
    # Over bid 0.15 and Under bid 0.50: Over ask 0.50, Under ask 0.85.
    barnes = _market(snapshot, BARNES_POINTS_19_5)
    assert [s.decimal_price for s in barnes.selections] == [
        Decimal("2"),
        Decimal("1.176471"),
    ]
    assert [s.american_price for s in barnes.selections] == [None, None]


def test_a_side_without_an_ask_leaves_both_sides_unpriced():
    # The recorded Barnes 24.5 book has only an Under bid, so the Under ask
    # exists on no side that has an Over bid to price against.
    market = _market(_snapshot(), BARNES_24_5)

    assert [s.price_kind for s in market.selections] == [
        PriceKind.UNPRICED,
        PriceKind.UNPRICED,
    ]
    assert [s.price_value for s in market.selections] == [None, None]


def test_an_empty_recorded_book_is_unpriced_not_a_failure():
    snapshot = _snapshot()
    market = _market(snapshot, "01a13f9f-78ca-76b0-b6a4-75a115b6cb85")  # Brunson

    assert market.is_priced is False
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_the_best_bid_is_the_highest_resting_price():
    session, ids = _ladder({"24.5": ("0.40", "0.45")})
    book = session.books[ids["24.5"]]
    book["orders"]["under-24.5"] = [
        {"orderId": "a", "price": "0.30", "qty": 5},
        {"orderId": "b", "price": "0.45", "qty": 5},
        {"orderId": "c", "price": "0.20", "qty": 5},
    ]

    market = _market(_snapshot(adapter(session)), ids["24.5"])

    assert [s.decimal_price for s in market.selections] == [
        Decimal("1.818182"),  # 1 / (1 - 0.45)
        Decimal("1.666667"),  # 1 / (1 - 0.40)
    ]


# ------------------------------------------------------ scope and statistics


def test_only_full_game_player_markets_are_normalized_and_the_rest_are_typed_exclusions():
    snapshot = _snapshot()

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.fetched_count == 27
    assert snapshot.coverage.eligible_count == 20
    assert snapshot.coverage.skipped_count == 7
    assert snapshot.coverage.skipped_reasons == (
        CoverageCode.NON_PLAYER_MARKET,  # TOTAL, SPREAD, TEAM_TOTAL, MONEY
        CoverageCode.NON_PROJECTION_MARKET,  # TRIPLE_DOUBLE, DOUBLE_DOUBLE
        CoverageCode.NON_GAME_MARKET,  # CHAMPIONSHIP_WINNER
    )
    assert snapshot.coverage.diagnostic_details == ()
    assert len(snapshot.markets) == 20
    assert {m.scoring_period for m in snapshot.markets} == {ScoringPeriod.FULL_GAME}
    assert {m.scoring_period_label for m in snapshot.markets} == {None}
    assert {m.statistic.label for m in snapshot.markets} == {
        "POINTS",
        "ASSISTS",
        "THREE_POINTERS_MADE",
        "POINTS_REBOUNDS",
        "REBOUNDS_ASSISTS",
    }


def test_exactly_one_line_per_player_statistic_and_game_is_standard():
    snapshot = _snapshot()

    standard = sorted(
        (m.athlete.name, m.statistic.label, str(m.threshold.value))
        for m in snapshot.markets
        if m.variant is MarketVariant.STANDARD
    )

    assert standard == [
        ("Jalen Brunson", "POINTS_REBOUNDS", "21.5"),
        ("Karl-Anthony Towns", "POINTS_REBOUNDS", "30.5"),
        ("Kawhi Leonard", "POINTS", "29.5"),
        ("Scottie Barnes", "POINTS", "19.5"),
        ("Tyrese Maxey", "POINTS", "21.5"),
    ]


def test_a_market_status_outside_the_query_is_excluded_by_the_status_filter():
    session = ReplaySession()
    session.pages[0]["items"][0]["status"] = "CLOSED"
    session.pages[0]["items"][1]["status"] = "OPEN"

    snapshot = _snapshot(
        adapter(session), NBAMarketQuery(market_statuses=(MarketStatus.SUSPENDED,))
    )

    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_reasons == (
        CoverageCode.INELIGIBLE_STATUS,
        CoverageCode.STATUS_FILTER,
        CoverageCode.NON_PLAYER_MARKET,
        CoverageCode.NON_PROJECTION_MARKET,
        CoverageCode.NON_GAME_MARKET,
    )


def test_every_statistic_label_maps_to_its_market_category():
    catalog = StatisticCatalog.load_default()

    categories = {
        label: catalog.resolve(
            "novig", label, scoring_period=ScoringPeriod.FULL_GAME, unit="count"
        )
        for label in sorted(PLAYER_STATISTIC_MARKET_TYPES)
    }

    assert {
        label: match.canonical.market_category for label, match in categories.items()
    } == {
        "ASSISTS": "AST",
        "BLOCKS": "BLK",
        "POINTS": "PTS",
        "POINTS_ASSISTS": "PA",
        "POINTS_REBOUNDS": "PR",
        "POINTS_REBOUNDS_ASSISTS": "PRA",
        "REBOUNDS": "REB",
        "REBOUNDS_ASSISTS": "RA",
        "STEALS": "STL",
        "STEALS_BLOCKS": "STKS",
        "THREE_POINTERS_MADE": "3PM",
        "TURNOVERS": "TOV",
    }
    unmapped = catalog.resolve(
        "novig",
        "DOUBLE_DOUBLE",
        scoring_period=ScoringPeriod.FULL_GAME,
        unit="count",
    )
    assert unmapped.state is MatchState.UNMAPPED


# ------------------------------------------------------------ evidence


def test_athlete_and_event_evidence_are_what_the_resolvers_need():
    market = _market(_snapshot(), BARNES_POINTS_19_5)

    assert market.athlete.provider_id == "scottie-barnes"
    assert market.athlete.name == "Scottie Barnes"
    assert market.event.provider_id == NYK_EVENT
    assert market.event.label == "Toronto Raptors @ New York Knicks"
    assert market.event.home_team.name == "New York Knicks"
    assert market.event.away_team.name == "Toronto Raptors"
    assert market.event.starts_at == datetime(2026, 10, 15, 23, 35, tzinfo=timezone.utc)
    assert market.event.status_label == "OPEN_PREGAME"
    assert market.starts_at == datetime(2026, 10, 15, 23, 35, tzinfo=timezone.utc)
    assert market.threshold.value == Decimal("19.5")
    assert market.threshold.unit == "count"
    assert market.status is MarketStatus.AVAILABLE
    assert [s.selection_id for s in market.selections] == [
        row_outcome["outcomeId"]
        for row_outcome in market_row(BARNES_POINTS_19_5)["outcomes"]
    ]
    assert [s.label for s in market.selections] == ["Over 19.5", "Under 19.5"]


def test_the_second_event_resolves_from_its_own_event_read():
    market = _market(_snapshot(), "01a13b8c-20ef-7d32-868d-926b7c3400b5")  # Maxey

    assert market.event.provider_id == PHI_EVENT
    assert market.event.away_team.name == "Philadelphia 76ers"
    assert market.event.home_team.name == "New York Knicks"
    assert market.event.starts_at == datetime(2026, 10, 20, 23, 5, tzinfo=timezone.utc)


def test_a_player_name_is_normalized_into_the_provider_id():
    session = ReplaySession()
    row = session.pages[0]["items"][0]
    row["description"] = "Nikola Jokić 24.5 POINTS"

    market = _market(_snapshot(adapter(session)), BARNES_24_5)

    assert market.athlete.provider_id == "nikola-jokic"
    assert market.athlete.name == "Nikola Jokić"


def test_an_event_that_is_not_pregame_excludes_its_markets():
    session = ReplaySession()
    session.events[PHI_EVENT]["status"] = "OPEN_INGAME"

    snapshot = _snapshot(adapter(session))

    assert {m.event.provider_id for m in snapshot.markets} == {NYK_EVENT}
    assert len(snapshot.markets) == 15
    assert CoverageCode.INELIGIBLE_EVENT_STATUS in snapshot.coverage.skipped_reasons
    assert snapshot.status is SnapshotStatus.COMPLETE
    # No book is read for a market whose event cannot carry it.
    assert len(session.urls("/book")) == 15


# ----------------------------------------------------- coverage and failures


def test_a_row_that_breaks_the_description_pattern_is_malformed_and_partial():
    session = ReplaySession()
    session.pages[0]["items"][0]["description"] = "Scottie Barnes POINTS"

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 19
    assert snapshot.coverage.skipped_count == 8
    assert snapshot.coverage.skipped_reasons == (
        CoverageCode.MALFORMED_RECORD,
        CoverageCode.NON_PLAYER_MARKET,
        CoverageCode.NON_PROJECTION_MARKET,
        CoverageCode.NON_GAME_MARKET,
    )
    assert snapshot.coverage.diagnostic_details == (
        "description must be '<name> <strike> <market type>'",
    )


def test_a_description_strike_that_disagrees_with_the_strike_is_malformed():
    session = ReplaySession()
    session.pages[0]["items"][0]["strike"] = "25.5"

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.diagnostic_details == (
        "description strike conflicts with strike",
    )


def test_an_event_that_cannot_be_read_makes_its_markets_malformed_and_partial():
    session = ReplaySession(failures={f"/events/{PHI_EVENT}": 403})

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert {m.event.provider_id for m in snapshot.markets} == {NYK_EVENT}
    assert snapshot.coverage.skipped_count == 7 + 5
    assert CoverageCode.FIXTURE_FAILED in snapshot.coverage.skipped_reasons
    assert snapshot.coverage.diagnostic_details == ("event detail unavailable",)


def test_when_no_event_can_be_read_the_retrieval_fails_as_unavailable():
    session = ReplaySession(
        failures={f"/events/{NYK_EVENT}": 403, f"/events/{PHI_EVENT}": 403}
    )

    with pytest.raises(ProviderUnavailableError, match="Novig could not be reached"):
        _snapshot(adapter(session))


def test_failed_books_leave_their_markets_unpriced_and_the_snapshot_partial():
    session = ReplaySession(
        failures={f"/markets/{KAWHI_POINTS_29_5}/book": requests.ConnectionError("down")}
    )

    snapshot = _snapshot(adapter(session))
    market = _market(snapshot, KAWHI_POINTS_29_5)

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 20
    assert market.is_priced is False
    assert market.variant is MarketVariant.STANDARD  # a lone line stays standard
    assert snapshot.coverage.warning_codes == (CoverageCode.FIXTURE_FAILED,)
    assert snapshot.coverage.diagnostic_details == (
        "novig book unavailable for 1 of 20 markets",
    )
    # The other priced market is unaffected.
    assert _market(snapshot, BARNES_POINTS_19_5).is_priced is True


def test_a_book_naming_an_unknown_outcome_counts_as_a_failed_book():
    session = ReplaySession()
    session.books[KAWHI_POINTS_29_5]["orders"] = {
        "not-an-outcome": [{"orderId": "x", "price": "0.5", "qty": 1}]
    }

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert _market(snapshot, KAWHI_POINTS_29_5).is_priced is False


@pytest.mark.parametrize(
    "book",
    [
        {"marketId": "m", "seq": 1},
        {"orders": {"o": [{"price": "1.2", "qty": 5}]}},
        {"orders": {"o": [{"price": "abc", "qty": 5}]}},
        {"orders": {"o": "not-a-list"}},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_book_is_a_failed_book_not_a_failed_retrieval(book):
    session = ReplaySession()
    session.books[KAWHI_POINTS_29_5] = book

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert _market(snapshot, KAWHI_POINTS_29_5).is_priced is False


def test_a_later_page_that_fails_keeps_the_earlier_pages_and_is_partial():
    session = ReplaySession(later_page_failure=403)

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.pagination_complete is False
    assert len(snapshot.markets) == 15
    assert snapshot.coverage.warning_codes == (CoverageCode.PAGE_FETCH_FAILED,)
    assert snapshot.coverage.diagnostic_details[0].startswith(
        "novig market page 2 unavailable"
    )


def test_a_repeated_cursor_stops_pagination_as_partial():
    session = ReplaySession()
    session.pages[1]["next"] = session.pages[0]["next"]
    session.pages.append(session.pages[0])

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.warning_codes == (
        CoverageCode.PAGINATION_PAGE_MISMATCH,
    )


def test_a_first_page_failure_is_one_unavailable_error():
    session = ReplaySession(failures={"/catalog/markets": requests.ConnectionError("down")})

    with pytest.raises(ProviderUnavailableError, match="Novig could not be reached"):
        _snapshot(adapter(session))


def test_a_first_page_that_is_not_a_market_page_is_an_invalid_response():
    session = ReplaySession(pages=[{"unexpected": []}])

    with pytest.raises(ProviderUnavailableError, match="invalid response") as caught:
        _snapshot(adapter(session))

    assert caught.value.provider_reason == "malformed_response"


def test_a_venue_with_no_player_markets_is_a_complete_empty_snapshot():
    session = ReplaySession(pages=[{"items": []}])

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.fetched_count == 0
    assert session.urls("/book") == []


def test_a_venue_with_only_team_lines_is_a_complete_empty_snapshot():
    page = load("markets.page1.json")
    page["items"] = [r for r in page["items"] if r["marketType"] == "SPREAD"]
    page.pop("next")

    snapshot = _snapshot(adapter(ReplaySession(pages=[page])))

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_count == 1


def test_an_expired_deadline_reads_nothing_and_fails_as_unavailable():
    session = ReplaySession()
    expired = RetrievalContext(deadline=datetime(2026, 1, 1, tzinfo=timezone.utc))

    with pytest.raises(ProviderUnavailableError, match="deadline exceeded"):
        adapter(session).get_snapshot(NBAMarketQuery(), expired)

    assert session.calls == []


# ------------------------------------------------------------- transport


def test_requests_state_the_league_page_size_cursor_and_book_depth():
    session = ReplaySession()

    _snapshot(adapter(session))

    assert [params for url, params in session.calls if url.endswith("/catalog/markets")] == [
        {"league": "NBA", "limit": 200},
        {
            "league": "NBA",
            "limit": 200,
            "after": "01a13cfa-9a3e-7830-82f9-5c3f2ac34c94",
        },
    ]
    book_calls = [call for call in session.calls if call[0].endswith("/book")]
    assert len(book_calls) == 20
    assert {params["depth"] for _, params in book_calls if params} == {1}
    assert len(session.urls(f"/events/{NYK_EVENT}")) == 1
    assert len(session.urls(f"/events/{PHI_EVENT}")) == 1


def test_provider_events_use_the_novig_operation_catalog():
    _snapshot()

    events = telemetry.get_recorded_provider_events()

    assert {e["provider"] for e in events} == {"novig"}
    operations = [e["operation"] for e in events]
    assert operations.count("market_page") == 2
    assert operations.count("event_detail") == 2
    assert operations.count("market_book") == 20
    assert set(operations) <= telemetry.PROVIDER_OPERATION_CATALOG["novig"]


def test_the_adapter_sends_browser_like_headers_because_novig_blocks_some_clients():
    session = ReplaySession()

    adapter(session)

    assert session.headers["Accept"] == "application/json"
    assert session.headers["User-Agent"].startswith("Mozilla/5.0")


def test_the_registry_builds_the_adapter_with_bounded_concurrency():
    built = build_dfs_provider("novig", DFSProviderRuntime(detail_concurrency=9))

    assert isinstance(built, NovigAdapter)
    assert built.detail_concurrency == 4


def test_the_pacer_spaces_starts_and_refuses_a_slot_past_the_deadline():
    pacer = _Pacer(10.0)

    assert pacer.reserve(5.0) == 0.0
    # The next slot opens in ten seconds, which is past a five second budget.
    assert pacer.reserve(5.0) is None
    wait = pacer.reserve(60.0)
    assert wait is not None and 9.0 < wait <= 10.0
