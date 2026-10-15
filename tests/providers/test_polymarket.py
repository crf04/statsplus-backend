"""Offline tests for the Polymarket DFS snapshot adapter.

The fixtures are real Gamma API payloads (https://gamma-api.polymarket.com):

* ``conformance.events.json``: ``/events?slug=nba-nyk-sas-2026-06-13`` (the
  recorded Finals game, its prop markets and one market of each other type),
  reopened -- ``closed``/``ended`` false, ``acceptingOrders`` true, tip-off
  moved to 2026-08-10 00:30Z so the board is pregame at the suite's clock --
  followed by the two untouched open events below.
* ``preseason.events.json``: ``/events?tag_slug=nba&closed=false&limit=100``
  as served on 2026-10-15: game events with only game markets
  (``nba-mia-orl-2026-10-16``) and a futures event
  (``will-lebron-james-retire-before-next-nba-season``).

Prices in the recorded game are its settled values (bids and asks at 0.999/1),
so the standard-line and price rules are also pinned on one real market whose
bid and ask are overridden with the literal values each test names.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    CoverageCode,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    RetrievalContext,
    ScoringPeriod,
    SelectionDirection,
    SnapshotStatus,
)
from app.providers.polymarket import PolymarketAdapter
from app.services.athlete_resolver import (
    AthleteResolver,
    MappingResolutionState,
    normalize_athlete_name,
)
from app.services.event_resolver import CanonicalEvent, EventResolutionState, EventResolver
from app.utils import telemetry

FIXTURES = Path(__file__).parents[1] / "fixtures" / "polymarket"
NOW = datetime(2026, 8, 9, 20, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


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
    """Serve one scripted response per call and record each request."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


def _events(name: str = "conformance.events.json") -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _context() -> RetrievalContext:
    return RetrievalContext(deadline="2030-01-01T00:00:00Z")


def _snapshot(*pages: object, query: NBAMarketQuery | None = None, adapter=None):
    session = FakeSession(*pages)
    adapter = adapter or PolymarketAdapter(session=session, now=lambda: NOW)
    adapter.session = session
    return adapter.get_snapshot(query or NBAMarketQuery(), _context()), session


def _by_id(snapshot) -> dict:
    return {market.market_id: market for market in snapshot.markets}


def _prop(events: list[dict], market_id: str) -> dict:
    return next(
        market
        for event in events
        for market in event["markets"]
        if market["id"] == market_id
    )


def _game_with(markets: list[dict]) -> list[dict]:
    """The recorded Knicks-Spurs event, carrying only ``markets``."""

    event = _events()[0]
    event["markets"] = markets
    return [event]


def _template() -> dict:
    """Real market 2503883 (OG Anunoby points 18.5) to derive props from."""

    return copy.deepcopy(_prop(_events(), "2503883"))


def _variant(
    market_id: str,
    *,
    line: float,
    bid: float | None,
    ask: float | None,
    name: str = "OG Anunoby",
    stat: str = "points",
    **overrides,
) -> dict:
    market = _template()
    market.update(
        id=market_id,
        line=line,
        sportsMarketType=stat,
        question=f"{name}: {stat.title()} O/U {line}",
        groupItemTitle=f"{name}: {stat.title()} O/U {line}",
    )
    for key, value in (("bestBid", bid), ("bestAsk", ask)):
        if value is None:
            market.pop(key, None)
        else:
            market[key] = value
    market.update(overrides)
    return market


def _variants(snapshot) -> dict[str, str]:
    return {market.market_id: market.variant.value for market in snapshot.markets}


# -- the recorded game ---------------------------------------------------------


def test_the_recorded_game_normalizes_every_in_scope_prop_with_typed_coverage():
    snapshot, session = _snapshot(_events())

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 30
    coverage = snapshot.coverage
    assert (coverage.fetched_count, coverage.eligible_count, coverage.skipped_count) == (
        42,
        30,
        12,
    )
    assert set(coverage.skipped_reasons) == {
        CoverageCode.NON_PLAYER_MARKET,
        CoverageCode.NON_GAME_MARKET,
    }
    assert session.calls == [
        (
            "https://gamma-api.polymarket.com/events",
            {"tag_slug": "nba", "closed": "false", "limit": 100, "offset": 0},
        )
    ]


def test_a_recorded_prop_carries_athlete_event_statistic_and_full_game_scope():
    snapshot, _ = _snapshot(_events())
    market = _by_id(snapshot)["2502862"]

    assert market.provider == "polymarket"
    assert market.athlete.provider_id == "victor-wembanyama"
    assert market.athlete.name == "Victor Wembanyama"
    assert market.team is None
    assert market.event.provider_id == "567958"
    assert market.event.label == "Knicks vs. Spurs"
    assert market.event.starts_at == datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)
    assert market.starts_at == datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)
    assert (market.event.home_team.provider_id, market.event.home_team.name) == (
        "100506",
        "Spurs",
    )
    assert market.event.home_team.abbreviation == "SAS"
    assert (market.event.away_team.provider_id, market.event.away_team.name) == (
        "100494",
        "Knicks",
    )
    assert market.event.away_team.abbreviation == "NYK"
    assert market.statistic.label == "rebounds"
    assert market.threshold.value == Decimal("11.5")
    assert market.scoring_period is ScoringPeriod.FULL_GAME
    assert market.scoring_period_label is None
    assert market.status is MarketStatus.AVAILABLE
    assert market.variant is MarketVariant.STANDARD


def test_a_recorded_prop_prices_over_at_the_ask_and_under_at_one_minus_the_bid():
    snapshot, _ = _snapshot(_events())
    # Real values: bestBid 0.999, bestAsk 1 -> Over 1 / 1, Under 1 / (1 - 0.999).
    settled = _by_id(snapshot)["2502862"]
    # Real values: bestBid 0.991, bestAsk 0.999.
    quoted = _by_id(snapshot)["2505624"]

    over, under = settled.selections
    assert (over.selection_id, over.direction, over.direction_label) == (
        "2502862:over",
        SelectionDirection.HIGHER,
        "Over",
    )
    assert (under.selection_id, under.direction) == (
        "2502862:under",
        SelectionDirection.LOWER,
    )
    assert over.decimal_price == Decimal("1")
    assert under.decimal_price == Decimal("1000")
    assert [s.price_kind.value for s in settled.selections] == ["decimal", "decimal"]
    assert [s.price_scope.value for s in settled.selections] == ["selection"] * 2
    over, under = quoted.selections
    assert str(over.decimal_price) == "1.001001"
    assert str(under.decimal_price) == "111.111111"


def test_prices_follow_the_ask_of_each_side_for_a_literal_quote():
    market = _variant("p1", line=17.5, bid=0.75, ask=0.8)

    snapshot, _ = _snapshot(_game_with([market]))

    over, under = snapshot.markets[0].selections
    assert over.decimal_price == Decimal("1.25")
    assert under.decimal_price == Decimal("4")


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (None, 0.8),  # no Under ask without a bid
        (0.75, None),  # no Over ask
        (1, 1),  # a bid of 1 leaves the Under no ask
        (0.5, 0),  # an ask of 0 is no ask
    ],
)
def test_a_missing_ask_on_either_side_leaves_both_sides_unpriced(bid, ask):
    snapshot, _ = _snapshot(_game_with([_variant("p1", line=17.5, bid=bid, ask=ask)]))

    market = snapshot.markets[0]
    assert [s.decimal_price for s in market.selections] == [None, None]
    assert [s.price_kind.value for s in market.selections] == ["unpriced", "unpriced"]
    assert market.is_priced is False


def test_the_recorded_player_name_comes_from_the_title_before_the_colon():
    snapshot, _ = _snapshot(_events())
    names = {m.market_id: (m.athlete.name, m.athlete.provider_id) for m in snapshot.markets}

    assert names["2502860"] == ("De'Aaron Fox", "de-aaron-fox")
    assert names["2502858"] == ("Karl-Anthony Towns", "karl-anthony-towns")


def test_a_title_without_a_colon_falls_back_to_the_question_then_is_malformed():
    fallback = _variant("p1", line=17.5, bid=0.4, ask=0.5, name="Nikola Jokić")
    fallback["groupItemTitle"] = "Nikola Jokić O/U 17.5"
    broken = _variant("p2", line=17.5, bid=0.4, ask=0.5)
    broken["groupItemTitle"] = "OG Anunoby O/U 17.5"
    broken["question"] = "OG Anunoby Points O/U 17.5"

    snapshot, _ = _snapshot(_game_with([fallback, broken]))

    assert [m.athlete.provider_id for m in snapshot.markets] == ["nikola-jokic"]
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.diagnostic_details == (
        "player name must precede ':' in the market title",
    )


# -- standard line (D3) --------------------------------------------------------


def test_the_recorded_game_has_at_most_one_standard_line_per_player_and_statistic():
    snapshot, _ = _snapshot(_events())

    standard = {}
    for market in snapshot.markets:
        if market.variant is MarketVariant.STANDARD:
            key = (market.athlete.provider_id, market.statistic.label)
            assert key not in standard
            standard[key] = market.market_id
    # The one real two-line group has no midpoint on either line, so neither of
    # its lines is guessed; every other group has a single line.
    assert _variants(snapshot)["2502857"] == "alternate"
    assert _variants(snapshot)["2503883"] == "alternate"
    assert len(standard) == 28
    assert sum(1 for v in _variants(snapshot).values() if v == "alternate") == 2
    assert {m.variant_label for m in snapshot.markets} == {"standard", "alternate"}


def test_the_line_with_the_midpoint_nearest_half_is_standard():
    markets = [
        _variant("far", line=19.5, bid=0.2, ask=0.22),  # midpoint 0.21
        _variant("near", line=17.5, bid=0.55, ask=0.57),  # midpoint 0.56
        _variant("mid", line=18.5, bid=0.4, ask=0.42),  # midpoint 0.41
    ]

    snapshot, _ = _snapshot(_game_with(markets))

    assert _variants(snapshot) == {
        "far": "alternate",
        "near": "standard",
        "mid": "alternate",
    }


def test_nearest_half_is_measured_from_half_on_both_sides():
    markets = [
        _variant("above", line=18.5, bid=0.65, ask=0.67),  # midpoint 0.66
        _variant("below", line=17.5, bid=0.44, ask=0.46),  # midpoint 0.45
    ]

    snapshot, _ = _snapshot(_game_with(markets))

    assert _variants(snapshot) == {"above": "alternate", "below": "standard"}


@pytest.mark.parametrize("reverse", [False, True])
def test_a_midpoint_tie_goes_to_the_lower_line_whatever_the_payload_order(reverse):
    markets = [
        _variant("low", line=17.5, bid=0.54, ask=0.56),  # midpoint 0.55
        _variant("high", line=18.5, bid=0.44, ask=0.46),  # midpoint 0.45
    ]
    if reverse:
        markets.reverse()

    snapshot, _ = _snapshot(_game_with(markets))

    assert _variants(snapshot) == {"low": "standard", "high": "alternate"}


def test_a_line_with_a_midpoint_beats_a_line_without_one():
    markets = [
        _variant("quoted", line=19.5, bid=0.1, ask=0.12),  # midpoint 0.11
        _variant("no_bid", line=17.5, bid=None, ask=0.5),
    ]

    snapshot, _ = _snapshot(_game_with(markets))

    assert _variants(snapshot) == {"quoted": "standard", "no_bid": "alternate"}


def test_with_no_midpoint_one_line_is_standard_and_several_are_all_alternate():
    single = [_variant("only", line=17.5, bid=None, ask=0.5)]
    several = [
        _variant("a", line=17.5, bid=None, ask=0.5),
        _variant("b", line=18.5, bid=None, ask=0.4),
    ]

    one, _ = _snapshot(_game_with(single))
    many, _ = _snapshot(_game_with(several))

    assert _variants(one) == {"only": "standard"}
    assert _variants(many) == {"a": "alternate", "b": "alternate"}


def test_standard_lines_are_chosen_per_player_statistic_and_game_independently():
    markets = [
        _variant("pts_a", line=17.5, bid=0.4, ask=0.44, stat="points"),
        _variant("pts_b", line=18.5, bid=0.3, ask=0.34, stat="points"),
        _variant("reb", line=5.5, bid=0.2, ask=0.24, stat="rebounds"),
        _variant("other", line=17.5, bid=0.1, ask=0.14, name="Josh Hart"),
    ]

    snapshot, _ = _snapshot(_game_with(markets))

    assert _variants(snapshot) == {
        "pts_a": "standard",
        "pts_b": "alternate",
        "reb": "standard",
        "other": "standard",
    }


def test_a_closed_line_does_not_take_part_in_the_standard_choice():
    markets = [
        _variant("closed", line=17.5, bid=0.5, ask=0.5, closed=True),
        _variant("open", line=18.5, bid=0.1, ask=0.12),
    ]

    snapshot, _ = _snapshot(_game_with(markets))

    assert _variants(snapshot) == {"open": "standard"}


# -- scope and exclusions (D4) -------------------------------------------------


@pytest.mark.parametrize(
    ("market_type", "code"),
    [
        ("moneyline", CoverageCode.NON_PLAYER_MARKET),
        ("spreads", CoverageCode.NON_PLAYER_MARKET),
        ("totals", CoverageCode.NON_PLAYER_MARKET),
        ("basketball_odd_even", CoverageCode.NON_PLAYER_MARKET),
        ("first_half_totals", CoverageCode.NON_GAME_MARKET),
        ("first_half_moneyline", CoverageCode.NON_GAME_MARKET),
        ("q1_totals", CoverageCode.NON_GAME_MARKET),
        ("q3_totals", CoverageCode.NON_GAME_MARKET),
        ("h2_team_totals", CoverageCode.NON_GAME_MARKET),
        ("q1_points", CoverageCode.NON_GAME_MARKET),
        ("first_half_points", CoverageCode.NON_GAME_MARKET),
        ("threes", CoverageCode.NON_PROJECTION_MARKET),
    ],
)
def test_game_period_and_unmapped_markets_are_excluded_with_a_typed_code(
    market_type, code
):
    market = _variant("x", line=225.5, bid=0.4, ask=0.5, stat=market_type)

    snapshot, _ = _snapshot(_game_with([market]))

    assert snapshot.markets == ()
    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.skipped_reasons == (code,)
    assert snapshot.coverage.skipped_count == 1


def test_a_market_without_a_type_is_excluded_as_not_a_projection():
    market = _variant("x", line=17.5, bid=0.4, ask=0.5)
    del market["sportsMarketType"]

    snapshot, _ = _snapshot(_game_with([market]))

    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_reasons == (CoverageCode.NON_PROJECTION_MARKET,)


def test_a_futures_event_is_one_excluded_record_however_many_outcomes_it_lists():
    futures = _events("preseason.events.json")[1]
    futures["markets"] = futures["markets"] * 110

    snapshot, _ = _snapshot([futures])

    assert snapshot.markets == ()
    assert snapshot.coverage.fetched_count == 1
    assert snapshot.coverage.skipped_reasons == (CoverageCode.NON_GAME_MARKET,)


def test_closed_inactive_and_closed_event_markets_are_excluded_as_ineligible():
    closed = _variant("closed", line=17.5, bid=0.4, ask=0.5, closed=True)
    inactive = _variant("inactive", line=18.5, bid=0.4, ask=0.5, active=False)
    event = _game_with([closed, inactive])[0]
    ended = copy.deepcopy(_events()[0])
    ended.update(id="other-event", ended=True, markets=[_variant("late", line=1.5, bid=0.4, ask=0.5)])

    snapshot, _ = _snapshot([event, ended])

    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_count == 3
    assert snapshot.coverage.skipped_reasons == (
        CoverageCode.INELIGIBLE_STATUS,
        CoverageCode.INELIGIBLE_EVENT_STATUS,
    )


def test_a_market_not_accepting_orders_is_suspended_and_follows_the_status_filter():
    market = _variant("held", line=17.5, bid=0.4, ask=0.5, acceptingOrders=False)

    both, _ = _snapshot(_game_with([market]))
    available_only, _ = _snapshot(
        _game_with([market]),
        query=NBAMarketQuery(market_statuses=(MarketStatus.AVAILABLE,)),
    )

    assert [(m.market_id, m.status) for m in both.markets] == [
        ("held", MarketStatus.SUSPENDED)
    ]
    assert available_only.markets == ()
    assert available_only.coverage.skipped_reasons == (CoverageCode.STATUS_FILTER,)


# -- empty board and evidence ---------------------------------------------------


def test_the_recorded_preseason_listing_is_a_complete_empty_snapshot():
    snapshot, _ = _snapshot(_events("preseason.events.json"))

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.is_complete
    assert (snapshot.coverage.fetched_count, snapshot.coverage.skipped_count) == (4, 4)
    assert set(snapshot.coverage.skipped_reasons) == {
        CoverageCode.NON_PLAYER_MARKET,
        CoverageCode.NON_GAME_MARKET,
    }


def test_an_empty_listing_is_a_complete_empty_snapshot():
    snapshot, _ = _snapshot([])

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.fetched_count == 0


def test_event_teams_fall_back_to_the_slug_away_then_home():
    event = _game_with([_variant("p1", line=17.5, bid=0.4, ask=0.5)])[0]
    del event["teams"]

    snapshot, _ = _snapshot([event])

    market = snapshot.markets[0]
    assert (market.event.home_team.abbreviation, market.event.away_team.abbreviation) == (
        "SAS",
        "NYK",
    )
    assert market.event.home_team.name is None


def test_an_event_naming_no_teams_is_one_typed_partial_record():
    broken = _game_with([_variant("p1", line=17.5, bid=0.4, ask=0.5)])[0]
    del broken["teams"]
    broken["slug"] = "nba-nyk-vs-sas"
    good = _events()[0]
    good["id"] = "other-event"
    good["markets"] = [_variant("p2", line=17.5, bid=0.4, ask=0.5)]

    snapshot, _ = _snapshot([broken, good])

    assert [m.market_id for m in snapshot.markets] == ["p2"]
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert CoverageCode.MISSING_EVENT_RELATIONSHIP in snapshot.coverage.skipped_reasons


def test_a_malformed_prop_is_partial_and_an_all_malformed_board_is_an_error():
    broken = _variant("bad", line=17.5, bid=0.4, ask=0.5)
    broken["line"] = "not-a-number"
    good = _variant("good", line=17.5, bid=0.4, ask=0.5)

    partial, _ = _snapshot(_game_with([good, broken]))
    assert partial.status is SnapshotStatus.PARTIAL
    assert [m.market_id for m in partial.markets] == ["good"]

    with pytest.raises(ProviderUnavailableError, match="invalid response"):
        _snapshot(_game_with([broken]))


@pytest.mark.parametrize(
    "override",
    [
        {"outcomes": '["Over", "Under"]'},
        {"outcomes": "not json"},
        {"bestAsk": 1.5},
        {"bestBid": "abc"},
    ],
)
def test_a_prop_that_is_not_a_yes_no_probability_market_is_malformed(override):
    broken = _variant("bad", line=17.5, bid=0.4, ask=0.5, **override)

    with pytest.raises(ProviderUnavailableError):
        _snapshot(_game_with([broken]))


# -- resolver evidence ------------------------------------------------------------


_PLAYERS = {
    "Victor Wembanyama": (1641705, 1610612759, "San Antonio Spurs", "SAS"),
    "Jalen Brunson": (1628973, 1610612752, "New York Knicks", "NYK"),
    "De'Aaron Fox": (1628368, 1610612759, "San Antonio Spurs", "SAS"),
}


class _AthleteCatalog:
    def get_catalog(self, season: str, *, active_only: bool = False):
        return [
            {
                "season": season,
                "player_id": player_id,
                "display_name": name,
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "team_id": team_id,
                "team_name": team_name,
                "team_abbreviation": team_abbreviation,
                "published_at": NOW,
            }
            for name, (player_id, team_id, team_name, team_abbreviation) in _PLAYERS.items()
        ]


class _EventCatalog:
    def get_events(self, season: str):
        return [
            {
                "nba_game_id": "0042500406",
                "season": season,
                "scheduled_at": datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc),
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
                "classification": "Playoffs",
                "first_seen_at": NOW,
                "last_seen_at": NOW,
                "is_postponed": False,
            }
        ]

    def get_freshness(self, season: str):
        return {"season": season, "fresh": True, "last_success_at": NOW.isoformat()}


def test_the_recorded_athlete_resolves_by_name_to_the_catalog_athlete():
    snapshot, _ = _snapshot(_events())
    market = _by_id(snapshot)["2502862"]

    result = AthleteResolver(_AthleteCatalog()).resolve_market(market, "2025-26")

    assert result.state is MappingResolutionState.AUTO
    assert result.canonical_athlete.player_id == 1641705
    assert normalize_athlete_name(market.athlete.name) == "victorwembanyama"


def test_the_recorded_event_resolves_by_teams_and_start_to_the_scheduled_game():
    snapshot, _ = _snapshot(_events())
    evidence = _by_id(snapshot)["2502862"].event

    result = EventResolver(_EventCatalog()).resolve("polymarket", evidence, "2025-26")

    assert result.state is EventResolutionState.AUTO
    assert result.canonical_event_id == "0042500406"
    assert isinstance(result.canonical_event, CanonicalEvent)


def test_recorded_markets_resolve_through_the_board_resolvers_to_literal_ids():
    snapshot, _ = _snapshot(_events())
    markets = _by_id(snapshot)
    season = "2025-26"
    # The calls dfs_board makes: a board view per resolver, then resolve_market.
    athletes = AthleteResolver(_AthleteCatalog()).for_board(
        season,
        [(m.provider, m.athlete.provider_id) for m in snapshot.markets],
    )
    games = EventResolver(_EventCatalog()).for_board(
        season, [(m.provider, m.event.provider_id) for m in snapshot.markets]
    )

    resolved = {
        market_id: (
            athletes.resolve_market(markets[market_id], season).canonical_athlete.player_id,
            games.resolve_market(markets[market_id], season).canonical_event_id,
        )
        for market_id in ("2502862", "2502856", "2502860")
    }

    assert resolved == {
        "2502862": (1641705, "0042500406"),  # Wembanyama rebounds
        "2502856": (1628973, "0042500406"),  # Brunson points
        "2502860": (1628368, "0042500406"),  # De'Aaron Fox points
    }
    # A player outside the catalog stays unresolved rather than guessed.
    unknown = athletes.resolve_market(markets["2502857"], season)
    assert unknown.state is MappingResolutionState.UNMATCHED
    assert unknown.canonical_athlete is None


def test_standard_lines_are_chosen_separately_for_each_game():
    game_a = _game_with(
        [
            _variant("a1", line=17.5, bid=0.55, ask=0.57),  # midpoint 0.56
            _variant("a2", line=18.5, bid=0.4, ask=0.42),  # midpoint 0.41
        ]
    )[0]
    game_b = copy.deepcopy(game_a)
    game_b["id"] = "second-game"
    game_b["markets"] = [
        _variant("b1", line=19.5, bid=0.44, ask=0.46),  # midpoint 0.45
        _variant("b2", line=20.5, bid=0.64, ask=0.66),  # midpoint 0.65
    ]

    snapshot, _ = _snapshot([game_a, game_b])

    assert _variants(snapshot) == {
        "a1": "standard",
        "a2": "alternate",
        "b1": "standard",
        "b2": "alternate",
    }


# -- timestamps and numeric domain ----------------------------------------------


def test_the_market_game_start_wins_over_a_stale_event_start():
    market = _variant("p1", line=17.5, bid=0.4, ask=0.5)
    market["gameStartTime"] = "2026-08-10 00:30:00+00"
    event = _game_with([market])[0]
    event["startTime"] = "2026-08-09T00:30:00Z"

    snapshot, _ = _snapshot([event])

    prop = snapshot.markets[0]
    assert prop.starts_at == datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)
    assert prop.event.starts_at == datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)
    result = EventResolver(_EventCatalog()).resolve_market(prop, "2025-26")
    assert result.canonical_event_id == "0042500406"


def test_the_event_start_is_used_only_when_the_market_has_no_game_start():
    market = _variant("p1", line=17.5, bid=0.4, ask=0.5)
    del market["gameStartTime"]
    event = _game_with([market])[0]
    event["startTime"] = "2026-08-11T01:00:00Z"

    snapshot, _ = _snapshot([event])

    assert snapshot.markets[0].event.starts_at == datetime(
        2026, 8, 11, 1, tzinfo=timezone.utc
    )


def test_prices_are_rounded_to_six_places():
    snapshot, _ = _snapshot(_game_with([_variant("p1", line=17.5, bid=0.6, ask=0.7)]))

    over, under = snapshot.markets[0].selections
    assert str(over.decimal_price) == "1.428571"  # 1 / 0.7
    assert str(under.decimal_price) == "2.500000"  # 1 / 0.4


@pytest.mark.parametrize("ask", ["1e-1000000", "1e-30"])
def test_an_unrepresentable_price_drops_only_its_own_market(ask):
    events = _events()
    broken = _prop(events, "2503883")
    broken["bestAsk"] = ask
    broken["bestBid"] = 0.5

    snapshot, _ = _snapshot(events)

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 29
    assert "2503883" not in _by_id(snapshot)
    assert snapshot.coverage.diagnostic_details == (
        "price is outside the supported numeric range",
    )


# -- pagination and transport -------------------------------------------------------


def _filler_events(count: int, *, first_id: int = 1000) -> list[dict]:
    template = _events("preseason.events.json")[1]
    events = []
    for index in range(count):
        event = copy.deepcopy(template)
        event["id"] = str(first_id + index)
        events.append(event)
    return events


def test_a_full_page_requests_the_next_offset_until_a_short_page():
    game = _events()
    page1 = _filler_events(100)
    adapter = PolymarketAdapter(now=lambda: NOW)

    snapshot, session = _snapshot(page1, game, adapter=adapter)

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 30
    assert [params["offset"] for _, params in session.calls] == [0, 100]
    assert snapshot.coverage.fetched_count == 100 + 42
    assert snapshot.coverage.pagination_complete is True


def test_an_event_repeated_across_pages_is_read_once():
    adapter = PolymarketAdapter(now=lambda: NOW)
    adapter.PAGE_SIZE = 1

    snapshot, session = _snapshot(_events()[:1], _events()[:1], [], adapter=adapter)

    assert len(session.calls) == 3
    assert len(snapshot.markets) == 30
    assert snapshot.coverage.fetched_count == 38


def test_a_page_failure_after_usable_data_is_partial_and_names_the_fetch_failure():
    adapter = PolymarketAdapter(now=lambda: NOW)
    adapter.PAGE_SIZE = 1

    snapshot, _ = _snapshot(
        _events()[:1], requests.ConnectionError("down"), adapter=adapter
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 30
    assert snapshot.coverage.pagination_complete is False
    assert CoverageCode.PAGE_FETCH_FAILED in snapshot.coverage.warning_codes


def test_a_listing_that_never_ends_stops_at_the_page_cap_as_partial():
    adapter = PolymarketAdapter(now=lambda: NOW)
    adapter.PAGE_SIZE = 1
    adapter.MAX_PAGES = 2

    snapshot, session = _snapshot(
        _events()[:1], _filler_events(1), adapter=adapter
    )

    assert len(session.calls) == 2
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.diagnostic_details == ("event listing exceeded the page cap",)


def test_the_first_page_failing_is_a_typed_unavailable_error():
    with pytest.raises(ProviderUnavailableError, match="could not be reached"):
        _snapshot(requests.ConnectionError("down"))
    with pytest.raises(ProviderUnavailableError, match="timed out"):
        _snapshot(requests.Timeout("slow"), requests.Timeout("slow"))
    with pytest.raises(ProviderUnavailableError, match="invalid response"):
        _snapshot({"events": []})


def test_the_snapshot_is_recorded_as_one_polymarket_provider_call():
    _snapshot(_events())

    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["provider"] == telemetry.PROVIDER_POLYMARKET == "polymarket"
    assert events[0]["operation"] == "get_snapshot"
    assert events[0]["outcome"] == telemetry.OUTCOME_SUCCESS
