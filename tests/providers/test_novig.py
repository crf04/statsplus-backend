"""Offline tests for the Novig DFS snapshot adapter.

The recorded payloads are real Novig responses (see ``novig_recorded``); the
ladder tests below build their markets from one recorded row so the structure
stays Novig's while the bids are chosen to pin one rule each.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
from app.providers.novig import PLAYER_STATISTIC_MARKET_TYPES, NovigAdapter
from app.providers.registry import DFSProviderRuntime, build_dfs_provider
from app.services.statistic_catalog import MatchState, StatisticCatalog
from app.utils import telemetry
from tests.providers.novig_recorded import (
    HOU_EVENT,
    NYK_EVENT,
    OBSERVED_AT,
    Response,
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


def _bid(outcome_id: str, market_id: str, price: float) -> dict:
    return {
        "id": "recorded-bid",
        "price": price,
        "qty": 1000,
        "originalQty": 1000,
        "status": "OPEN",
        "outcomeId": outcome_id,
        "marketId": market_id,
        "isBid": True,
        "currency": "CASH",
    }


def _ladder(lines: dict[str, tuple[float | None, float | None] | None]):
    """One player's POINTS ladder built from a recorded catalog row and book.

    ``lines`` maps a strike to its (best Over bid, best Under bid); None for a
    side means no bid, and None for the whole line means the batch that holds
    it cannot be read.  Returns a replay session and the market id of each
    strike.
    """

    template = market_row(BARNES_24_5)
    book_template = next(
        e for e in load("batch.json") if e["market"]["id"] == BARNES_24_5
    )
    items, batch, ids, unreadable = [], [], {}, set()
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
            unreadable.add(market_id)
            continue
        entry = deepcopy(book_template)
        entry["market"]["id"] = market_id
        entry["market"]["strike"] = float(strike)
        entry["market"]["outcomes"] = [
            {"id": over_id, "index": 0, "description": f"Over {strike}", "marketId": market_id},
            {"id": under_id, "index": 1, "description": f"Under {strike}", "marketId": market_id},
        ]
        entry["ladders"] = {
            over_id: {"bids": [] if bids[0] is None else [_bid(over_id, market_id, bids[0])], "asks": []},
            under_id: {"bids": [] if bids[1] is None else [_bid(under_id, market_id, bids[1])], "asks": []},
        }
        batch.append(entry)
    session = ReplaySession(
        pages=[{"items": items}],
        events={
            "items": [
                e for e in load("events.json")["items"] if e["eventId"] == NYK_EVENT
            ],
            "next": None,
        },
        batch=batch,
        unreadable=unreadable,
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
        {"19.5": (0.15, 0.50), "24.5": (0.40, 0.45), "29.5": (0.55, 0.30)}
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
            {"24.5": (0.50, 0.50), "19.5": (0.40, 0.40)},
            {"19.5": "standard", "24.5": "alternate"},
        ),
        # Midpoints 0.45 and 0.55 are the same distance from 0.5.
        (
            {"19.5": (0.50, 0.40), "14.5": (0.35, 0.45)},
            {"14.5": "standard", "19.5": "alternate"},
        ),
    ],
)
def test_a_tie_between_midpoints_goes_to_the_lower_line(lines, expected):
    session, ids = _ladder(lines)

    assert _variants(_snapshot(adapter(session)), ids) == expected


def test_a_single_line_without_a_midpoint_is_standard_and_unpriced():
    session, ids = _ladder({"24.5": (None, 0.55)})

    snapshot = _snapshot(adapter(session))
    market = _market(snapshot, ids["24.5"])

    assert market.variant is MarketVariant.STANDARD
    assert market.is_priced is False
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_several_lines_without_any_midpoint_are_all_alternate():
    session, ids = _ladder(
        {"19.5": (None, 0.55), "24.5": (0.10, None), "29.5": (None, None)}
    )

    snapshot = _snapshot(adapter(session))

    assert _variants(snapshot, ids) == {
        "19.5": "alternate",
        "24.5": "alternate",
        "29.5": "alternate",
    }


def test_an_unread_market_is_malformed_and_does_not_unsettle_the_standard_line():
    # 151 strikes make two batches (150 + 1); the lone last one cannot be read.
    # Midpoints: 1.5 -> 0.5 exactly, 2.5 -> 0.3; the rest sit at 0.5 too, so
    # make them all the same except the two pinned lines.
    lines = {f"{n}.5": (0.10, 0.60) for n in range(1, 152)}  # midpoint 0.25
    lines["1.5"] = (0.40, 0.40)  # midpoint 0.5
    lines["2.5"] = (0.20, 0.60)  # midpoint 0.3
    session, ids = _ladder(lines)
    session.unreadable = {ids["151.5"]}

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 150
    standard = [m for m in snapshot.markets if m.variant is MarketVariant.STANDARD]
    assert [str(m.threshold.value) for m in standard] == ["1.5"]
    assert snapshot.coverage.skipped_count == 1
    assert snapshot.coverage.skipped_reasons == (CoverageCode.FIXTURE_FAILED,)
    assert snapshot.coverage.warning_codes == (
        CoverageCode.MALFORMED_RECORD,
        CoverageCode.FIXTURE_FAILED,
    )
    assert snapshot.coverage.diagnostic_details == (
        "market book unavailable",
        "novig book unavailable for 1 of 151 markets",
    )


def test_when_no_market_can_be_read_the_retrieval_fails_as_unavailable():
    session, _ids = _ladder({"24.5": None})

    with pytest.raises(ProviderUnavailableError, match="Novig could not be reached"):
        _snapshot(adapter(session))


def test_a_stale_market_is_dropped_alone_and_the_rest_of_its_batch_is_read():
    session, ids = _ladder(
        {"19.5": (0.40, 0.40), "24.5": (0.50, 0.50), "29.5": (0.20, 0.60)}
    )
    session.stale = {ids["24.5"]}
    session.batch = [e for e in session.batch if e["market"]["id"] != ids["24.5"]]

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert {m.market_id for m in snapshot.markets} == {ids["19.5"], ids["29.5"]}
    assert snapshot.coverage.skipped_count == 1
    assert snapshot.coverage.skipped_reasons == (CoverageCode.INELIGIBLE_STATUS,)
    batch_calls = [p["marketIds"] for u, p in session.calls if u.endswith("/batch")]
    assert len(batch_calls) == 5
    # The line that was dropped is not unread: the others are still decided.
    assert _variants(snapshot, {"19.5": ids["19.5"], "29.5": ids["29.5"]}) == {
        "19.5": "standard",
        "29.5": "alternate",
    }


def test_a_market_the_batch_does_not_return_is_dropped_as_no_longer_open():
    session, ids = _ladder({"19.5": (0.40, 0.40), "24.5": (0.50, 0.50)})
    session.batch = [e for e in session.batch if e["market"]["id"] != ids["24.5"]]

    snapshot = _snapshot(adapter(session))

    assert [m.market_id for m in snapshot.markets] == [ids["19.5"]]
    assert snapshot.coverage.skipped_reasons == (CoverageCode.INELIGIBLE_STATUS,)
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_a_market_without_a_player_is_excluded_as_non_player():
    session = ReplaySession()
    entry = next(e for e in session.batch if e["market"]["id"] == BARNES_24_5)
    entry["market"]["player"] = None

    snapshot = _snapshot(adapter(session))

    assert len(snapshot.markets) == 19
    assert snapshot.coverage.skipped_count == 8
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_markets_are_read_in_batches_of_at_most_150():
    session, ids = _ladder({f"{n}.5": (0.40, 0.40) for n in range(1, 152)})

    snapshot = _snapshot(adapter(session))

    batches = [p["marketIds"].split(",") for u, p in session.calls if u.endswith("/batch")]
    assert sorted(len(b) for b in batches) == [1, 150]
    assert len(snapshot.markets) == 151


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


def test_a_two_sided_recorded_book_prices_both_sides():
    # Brunson 21.5 PR: best Over bid 0.10 and best Under bid 0.10 (a second
    # level at 0.01 is ignored), so both asks are 0.90.
    market = _market(_snapshot(), "01a13f9f-78ca-76b0-b6a4-75a115b6cb85")

    assert [s.decimal_price for s in market.selections] == [
        Decimal("1.111111"),
        Decimal("1.111111"),
    ]


def test_an_empty_recorded_book_is_unpriced_not_a_failure():
    snapshot = _snapshot()
    market = _market(snapshot, "01a1406c-867f-7342-a5a7-2686b16b8b92")  # Barnes 7.5 AST

    assert market.is_priced is False
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_the_best_bid_is_the_highest_open_resting_price():
    session, ids = _ladder({"24.5": (0.40, 0.45)})
    ladder = session.batch[0]["ladders"]["under-24.5"]
    ladder["bids"] = [
        _bid("under-24.5", ids["24.5"], 0.30),
        _bid("under-24.5", ids["24.5"], 0.45),
        {**_bid("under-24.5", ids["24.5"], 0.49), "status": "CANCELED"},
        {**_bid("under-24.5", ids["24.5"], 0.48), "qty": 0},
        _bid("under-24.5", ids["24.5"], 0.20),
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
        ("Kawhi Leonard", "THREE_POINTERS_MADE", "0.5"),
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

    assert market.athlete.provider_id == "8678a814-8259-4d41-9d35-9c9a599fb6ea"
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


def test_the_player_identity_is_the_batch_players_id_and_full_name():
    market = _market(_snapshot(), BARNES_24_5)

    assert market.athlete.provider_id == "8678a814-8259-4d41-9d35-9c9a599fb6ea"
    assert market.athlete.name == "Scottie Barnes"


def test_the_description_is_never_parsed_for_the_player():
    session = ReplaySession()
    session.pages[0]["items"][0]["description"] = "garbled"

    market = _market(_snapshot(adapter(session)), BARNES_24_5)

    assert market.athlete.name == "Scottie Barnes"


def test_an_event_that_is_not_pregame_excludes_its_markets():
    session = ReplaySession()
    next(
        e for e in session.events["items"] if e["eventId"] == PHI_EVENT
    )["status"] = "OPEN_INGAME"

    snapshot = _snapshot(adapter(session))

    assert {m.event.provider_id for m in snapshot.markets} == {NYK_EVENT}
    assert len(snapshot.markets) == 15
    assert CoverageCode.INELIGIBLE_EVENT_STATUS in snapshot.coverage.skipped_reasons
    assert snapshot.status is SnapshotStatus.COMPLETE
    # No book is read for a market whose event cannot carry it.
    (batch,) = [p["marketIds"] for u, p in session.calls if u.endswith("/batch")]
    assert len(batch.split(",")) == 15


# ----------------------------------------------------- coverage and failures


def test_a_row_with_a_non_numeric_strike_is_malformed_and_partial():
    session = ReplaySession()
    session.pages[0]["items"][0]["strike"] = "n/a"

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 19
    assert snapshot.coverage.skipped_reasons == (
        CoverageCode.MALFORMED_RECORD,
        CoverageCode.NON_PLAYER_MARKET,
        CoverageCode.NON_PROJECTION_MARKET,
        CoverageCode.NON_GAME_MARKET,
    )
    assert snapshot.coverage.diagnostic_details == ("strike must be numeric",)


def test_a_market_without_one_over_and_one_under_is_malformed():
    session = ReplaySession()
    session.pages[0]["items"][0]["outcomes"][1]["name"] = "Over 24.5"

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.diagnostic_details == (
        "market outcomes must be one Over and one Under",
    )


def test_an_event_the_list_lacks_makes_its_markets_malformed_and_partial():
    session = ReplaySession()
    session.events["items"] = [
        e for e in session.events["items"] if e["eventId"] != PHI_EVENT
    ]

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert {m.event.provider_id for m in snapshot.markets} == {NYK_EVENT}
    assert snapshot.coverage.skipped_count == 7 + 5
    assert CoverageCode.FIXTURE_FAILED in snapshot.coverage.skipped_reasons
    assert snapshot.coverage.diagnostic_details == ("event detail unavailable",)


def test_a_malformed_event_item_leaves_only_its_own_markets_malformed():
    session = ReplaySession()
    for item in session.events["items"]:
        if item["eventId"] == PHI_EVENT:
            item["description"] = "no teams here"

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert {m.event.provider_id for m in snapshot.markets} == {NYK_EVENT}


def test_when_the_event_list_cannot_be_read_the_retrieval_fails_as_unavailable():
    session = ReplaySession(failures={"/catalog/events": 403})

    with pytest.raises(ProviderUnavailableError, match="Novig could not be reached"):
        _snapshot(adapter(session))


def test_a_board_spanning_three_events_costs_pages_plus_one_event_list_plus_batches():
    session = ReplaySession()
    # Move one recorded NYK market into the third recorded event (Houston).
    moved = next(
        r for r in session.pages[0]["items"] if r["marketId"] == BARNES_24_5
    )
    moved["eventId"] = HOU_EVENT

    snapshot = _snapshot(adapter(session))

    assert {m.event.provider_id for m in snapshot.markets} == {
        NYK_EVENT,
        PHI_EVENT,
        HOU_EVENT,
    }
    assert snapshot.status is SnapshotStatus.COMPLETE
    kinds = [
        "page" if u.endswith("/catalog/markets")
        else "events" if u.endswith("/catalog/events")
        else "batch" if u.endswith("/batch")
        else "other"
        for u, _ in session.calls
    ]
    assert sorted(kinds) == ["batch", "events", "page", "page"]
    assert len(session.calls) == 4


def test_a_batch_that_fails_other_than_by_404_leaves_its_markets_unread_and_partial():
    session = ReplaySession(failures={"/batch": requests.ConnectionError("down")})

    with pytest.raises(ProviderUnavailableError, match="Novig could not be reached"):
        _snapshot(adapter(session))


def test_a_book_naming_an_unknown_outcome_is_an_unread_market():
    session = ReplaySession()
    entry = next(e for e in session.batch if e["market"]["id"] == KAWHI_POINTS_29_5)
    entry["ladders"]["not-an-outcome"] = {
        "bids": [{"price": 0.5, "qty": 1, "status": "OPEN"}],
        "asks": [],
    }

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert KAWHI_POINTS_29_5 not in {m.market_id for m in snapshot.markets}
    assert snapshot.coverage.skipped_count == 8


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["ladders"].update({"x": {"asks": []}}),
        lambda e: e["ladders"].update({"x": {"bids": [{"price": 1.2, "qty": 5}]}}),
        lambda e: e["ladders"].update({"x": {"bids": [{"price": "abc", "qty": 5}]}}),
        lambda e: e["market"].update({"player": {"id": "p"}}),
        lambda e: e.pop("ladders"),
    ],
)
def test_one_malformed_batch_entry_costs_only_its_own_market(mutate):
    session = ReplaySession()
    mutate(next(e for e in session.batch if e["market"]["id"] == KAWHI_POINTS_29_5))

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 19
    assert KAWHI_POINTS_29_5 not in {m.market_id for m in snapshot.markets}
    assert snapshot.coverage.skipped_count == 8
    assert CoverageCode.MALFORMED_RECORD in snapshot.coverage.warning_codes
    assert _market(snapshot, BARNES_POINTS_19_5).is_priced is True


def test_a_malformed_entry_is_a_line_without_a_midpoint():
    session, ids = _ladder(
        {"19.5": (0.40, 0.40), "24.5": (0.50, 0.50), "29.5": (0.20, 0.60)}
    )
    session.batch[1].pop("ladders")  # 24.5 would have been midpoint 0.5

    snapshot = _snapshot(adapter(session))

    # Midpoints among the readable lines: 19.5 -> 0.5, 29.5 -> 0.3.
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert {m.market_id: m.variant.value for m in snapshot.markets} == {
        ids["19.5"]: "standard",
        ids["29.5"]: "alternate",
    }


def test_an_omitted_id_is_malformed_when_the_response_holds_an_unattributable_entry():
    session = ReplaySession()
    del session.batch[3]["market"]["id"]  # Kawhi POINTS 29.5, now unattributable

    snapshot = _snapshot(adapter(session))

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 19
    assert KAWHI_POINTS_29_5 not in {m.market_id for m in snapshot.markets}
    assert CoverageCode.FIXTURE_FAILED in snapshot.coverage.skipped_reasons
    assert CoverageCode.INELIGIBLE_STATUS not in snapshot.coverage.skipped_reasons
    assert "batch entry without a market id" in snapshot.coverage.diagnostic_details


def test_when_no_batch_entry_is_attributable_the_retrieval_fails():
    session = ReplaySession()
    for entry in session.batch:
        del entry["market"]["id"]

    with pytest.raises(ProviderUnavailableError, match="invalid response"):
        _snapshot(adapter(session))


def test_a_batch_that_is_not_a_list_is_an_invalid_response():
    session = ReplaySession(batch={"unexpected": []})

    with pytest.raises(ProviderUnavailableError, match="invalid response"):
        _snapshot(adapter(session))


def test_one_stale_id_in_a_batch_of_eight_costs_seven_requests():
    session, ids = _ladder({f"{n}.5": (0.40, 0.40) for n in range(1, 9)})
    session.stale = {ids["8.5"]}
    session.batch = [e for e in session.batch if e["market"]["id"] != ids["8.5"]]

    snapshot = _snapshot(adapter(session))

    # 8 -> 404; halves of 4: one 404, one ok; halves of 2: one 404, one ok;
    # halves of 1: the stale one 404 (dropped), the other ok.  1 + 2 + 2 + 2.
    assert len([1 for u, _ in session.calls if u.endswith("/batch")]) == 7
    assert len(snapshot.markets) == 7
    assert snapshot.coverage.skipped_reasons == (CoverageCode.INELIGIBLE_STATUS,)
    assert snapshot.status is SnapshotStatus.COMPLETE


def test_a_period_market_is_excluded_not_taken_as_a_full_game_line():
    session = ReplaySession()
    session.pages[0]["items"][0]["marketType"] = "POINTS_1Q"

    snapshot = _snapshot(adapter(session))

    assert BARNES_24_5 not in {m.market_id for m in snapshot.markets}
    assert len(snapshot.markets) == 19
    assert snapshot.coverage.skipped_reasons == (
        CoverageCode.NON_PROJECTION_MARKET,
        CoverageCode.NON_PLAYER_MARKET,
        CoverageCode.NON_GAME_MARKET,
    )
    assert snapshot.status is SnapshotStatus.COMPLETE


# ------------------------------------------------- the real resolvers (AC5)


class _AthleteCatalog:
    ROWS = [
        {"season": "2026-27", "player_id": 1628973, "display_name": "Jalen Brunson",
         "roster_status": "Active", "is_active": True, "team_id": 1610612752,
         "team_name": "New York Knicks", "team_abbreviation": "NYK"},
        {"season": "2026-27", "player_id": 1630567, "display_name": "Scottie Barnes",
         "roster_status": "Active", "is_active": True, "team_id": 1610612761,
         "team_name": "Toronto Raptors", "team_abbreviation": "TOR"},
        {"season": "2026-27", "player_id": 202695, "display_name": "Kawhi Leonard",
         "roster_status": "Active", "is_active": True, "team_id": 1610612746,
         "team_name": "Los Angeles Clippers", "team_abbreviation": "LAC"},
    ]

    def get_catalog(self, season, active_only=False):
        return [r for r in self.ROWS if r["season"] == season]


def _catalog_row(game_id, when, home, away):
    """One event-catalog row in the shape production stores.

    The collector writes nba_api's ``teamName`` (the nickname, "Knicks") and
    ``teamTricode`` ("NYK"), not the full team name.
    """

    (home_id, home_name, home_code), (away_id, away_name, away_code) = home, away
    return {
        "nba_game_id": game_id, "season": "2026-27", "scheduled_at": when,
        "home_team_id": home_id, "home_team_name": home_name,
        "home_team_tricode": home_code,
        "away_team_id": away_id, "away_team_name": away_name,
        "away_team_tricode": away_code,
        "status_text": "Scheduled", "status_code": 1, "postponed_status": None,
        "postponement_evidence": None, "classification": "Regular Season",
        "is_postponed": False,
    }


_KNICKS = (1610612752, "Knicks", "NYK")
_RAPTORS = (1610612761, "Raptors", "TOR")
_SIXERS = (1610612755, "76ers", "PHI")


class _EventCatalog:
    ROWS = [
        _catalog_row(
            "0022600001", datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc),
            _KNICKS, _RAPTORS,
        ),
        _catalog_row(
            "0022600077", datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc),
            _KNICKS, _SIXERS,
        ),
    ]

    def get_events(self, season):
        return [r for r in self.ROWS if r["season"] == season]

    def get_freshness(self, season):
        return {"season": season, "fresh": True, "last_success_at": "2026-10-15T12:00:00+00:00"}


def test_recorded_markets_resolve_to_the_expected_athlete_and_game():
    from app.services.athlete_resolver import AthleteResolver, MappingResolutionState
    from app.services.event_resolver import EventResolutionState, EventResolver

    snapshot = _snapshot()
    athletes = AthleteResolver(_AthleteCatalog())
    events = EventResolver(_EventCatalog())

    def resolved(market_id: str):
        market = _market(snapshot, market_id)
        return (
            athletes.resolve_market(market, "2026-27"),
            events.resolve_market(market, "2026-27"),
        )

    brunson, brunson_game = resolved("01a13f9f-78ca-76b0-b6a4-75a115b6cb85")
    assert brunson.state is MappingResolutionState.AUTO
    assert brunson.canonical_athlete.player_id == 1628973
    assert brunson_game.state is EventResolutionState.AUTO
    assert brunson_game.canonical_event_id == "0022600001"

    barnes, barnes_game = resolved(BARNES_POINTS_19_5)
    assert barnes.canonical_athlete.player_id == 1630567
    assert barnes_game.canonical_event_id == "0022600001"

    kawhi, _ = resolved(KAWHI_POINTS_29_5)
    assert kawhi.canonical_athlete.player_id == 202695

    # PHI @ NYK on 2026-10-20 is a second scheduled game.
    _, phi_game = resolved("01a13b8c-20ef-7d32-868d-926b7c3400b5")
    assert phi_game.state is EventResolutionState.AUTO
    assert phi_game.canonical_event_id == "0022600077"


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
    assert session.urls("/batch") == []


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


def test_requests_state_the_league_page_size_cursor_and_batch_ids():
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
    (batch_call,) = [call for call in session.calls if call[0].endswith("/batch")]
    assert batch_call[0] == "https://api.novig.us/nbx/v1/markets/book/batch"
    assert batch_call[1]["currency"] == "CASH"
    assert len(batch_call[1]["marketIds"].split(",")) == 20
    (event_call,) = [call for call in session.calls if call[0].endswith("/catalog/events")]
    assert event_call[1] == {"league": "NBA", "limit": 200}


def test_provider_events_use_the_novig_operation_catalog():
    _snapshot()

    events = telemetry.get_recorded_provider_events()

    assert {e["provider"] for e in events} == {"novig"}
    operations = [e["operation"] for e in events]
    assert operations.count("market_page") == 2
    assert operations.count("event_page") == 1
    assert operations.count("market_book_batch") == 1
    assert set(operations) <= telemetry.PROVIDER_OPERATION_CATALOG["novig"]


def test_the_adapter_sends_browser_like_headers_because_novig_blocks_some_clients():
    session = ReplaySession()

    adapter(session)

    assert session.headers["Accept"] == "application/json"
    assert session.headers["User-Agent"].startswith("Mozilla/5.0")
    assert session.headers["Origin"] == "https://novig.com"
    assert session.headers["Referer"] == "https://novig.com/"


def test_the_registry_builds_the_adapter_with_bounded_concurrency():
    built = build_dfs_provider("novig", DFSProviderRuntime(detail_concurrency=9))

    assert isinstance(built, NovigAdapter)
    assert built.detail_concurrency == 4


def test_event_team_evidence_carries_the_catalog_tricode_for_each_full_name():
    market = _market(_snapshot(), BARNES_POINTS_19_5)

    assert [
        (t.name, t.abbreviation)
        for t in (market.event.home_team, market.event.away_team)
    ] == [("New York Knicks", "NYK"), ("Toronto Raptors", "TOR")]


def test_a_team_name_outside_the_closed_table_states_no_abbreviation():
    session = ReplaySession()
    for item in session.events["items"]:
        if item["eventId"] == NYK_EVENT:
            item["description"] = "Toronto Raptors @ New York Knickerbockers"

    market = _market(_snapshot(adapter(session)), BARNES_POINTS_19_5)

    assert market.event.home_team.name == "New York Knickerbockers"
    assert market.event.home_team.abbreviation is None
    assert market.event.away_team.abbreviation == "TOR"


# -------------------------------------------------- catalog pacing and 429s


class _FakeClock:
    """A monotonic clock that only moves when the adapter sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _RateLimitedCatalog(ReplaySession):
    """Answer the first market-page requests from ``script``, then replay.

    Each script entry is a status or a (status, headers) pair.  Request start
    times are read from ``clock`` so spacing can be asserted.
    """

    def __init__(self, clock: _FakeClock, script: list, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clock = clock
        self.script = list(script)
        self.starts: list[tuple[str, float]] = []

    def get(self, url, params=None, **kwargs):
        if "/catalog/" in url:
            self.starts.append((url.rsplit("/", 1)[1], self.clock.now))
        if url.endswith("/catalog/markets") and self.script:
            entry = self.script.pop(0)
            status, headers = entry if isinstance(entry, tuple) else (entry, {})
            if status != 200:
                response = Response({"code": "THROTTLED"}, status_code=status)
                response.headers = headers
                return response
        return super().get(url, params=params, **kwargs)


def _paced(clock: _FakeClock, script: list, **session_kwargs):
    session = _RateLimitedCatalog(clock, script, **session_kwargs)
    return session, adapter(session, monotonic=clock.monotonic, sleep=clock.sleep)


def test_catalog_requests_start_at_least_0_4_seconds_apart():
    clock = _FakeClock()
    session, novig = _paced(clock, [])

    _snapshot(novig)

    starts = [at for _, at in session.starts]
    assert [name for name, _ in session.starts] == ["markets", "markets", "events"]
    assert starts == pytest.approx([100.0, 100.4, 100.8])
    assert clock.sleeps == pytest.approx([0.4, 0.4])


def test_the_batch_host_is_not_paced():
    clock = _FakeClock()
    session, novig = _paced(clock, [])

    _snapshot(novig)

    assert session.urls("/batch") != []
    assert len(clock.sleeps) == 2  # the two catalog gaps only


def test_a_429_then_a_200_succeeds_after_a_one_second_backoff():
    clock = _FakeClock()
    session, novig = _paced(clock, [429])

    snapshot = _snapshot(novig)

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 20
    assert clock.sleeps == pytest.approx([1.0, 0.4, 0.4])
    assert [name for name, _ in session.starts] == [
        "markets", "markets", "markets", "events"
    ]


def test_a_429_with_retry_after_waits_exactly_that_long():
    clock = _FakeClock()
    session, novig = _paced(clock, [(429, {"Retry-After": "3"}), 429])

    snapshot = _snapshot(novig)

    assert snapshot.status is SnapshotStatus.COMPLETE
    # Retry-After 3, then no header: backoff for the second retry is 2 s.
    assert clock.sleeps[:2] == pytest.approx([3.0, 2.0])


def test_persistent_429_raises_the_same_rate_limited_error_after_four_attempts():
    clock = _FakeClock()
    session, novig = _paced(clock, [429] * 10)

    with pytest.raises(ProviderUnavailableError) as raised:
        _snapshot(novig)

    assert [n for n, _ in session.starts].count("markets") == 4  # 1 try + 3 retries
    assert str(raised.value) == "Novig could not be reached."
    assert raised.value.__cause__.response.status_code == 429
    assert clock.sleeps[:3] == pytest.approx([1.0, 2.0, 4.0])


def test_retries_stop_when_the_next_wait_would_pass_the_deadline():
    clock = _FakeClock()
    session = _RateLimitedCatalog(clock, [429] * 10)
    novig = adapter(
        session,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: OBSERVED_AT + timedelta(seconds=clock.now - 100.0),
    )
    deadline = OBSERVED_AT + timedelta(seconds=2.5)

    with pytest.raises(ProviderUnavailableError):
        novig.get_snapshot(NBAMarketQuery(), RetrievalContext(deadline=deadline))

    # 1 s is affordable, the following 2 s is not within the 2.5 s left.
    assert [n for n, _ in session.starts].count("markets") == 2
    assert clock.sleeps[:1] == [1.0]
