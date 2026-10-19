"""Per-pick Over/Under prices (line_prices): conversion, tightness, scope, folding."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.providers.dfs import NBAMarketQuery, Selection, SelectionDirection
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
    _american_from_decimal,
    _side_prices,
)
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_projection_archive import (
    GAME_ID,
    OBSERVED_AT,
    SEASON,
    _line_market,
    _line_snapshot,
)

CATALOG = StatisticCatalog.load_default()


def _decimal_sides(over: str, under: str | None):
    return (
        Selection(selection_id="o", direction="over", decimal_price=Decimal(over)),
        Selection(
            selection_id="u",
            direction="under",
            decimal_price=None if under is None else Decimal(under),
        ),
    )


def _american_sides(over: int | None, under: int | None):
    return (
        Selection(selection_id="h", direction="Higher", american_price=over),
        Selection(selection_id="l", direction="Lower", american_price=under),
    )


def _prices(provider: str, selections) -> tuple[int | None, int | None]:
    return _side_prices(
        _line_market(
            CATALOG,
            provider=provider,
            market_id="m",
            statistic_id="points",
            threshold="26.5",
            selections=selections,
        )
    )


@pytest.mark.parametrize(
    ("decimal_odds", "american"),
    [
        ("2", 100),
        ("2.000000", 100),
        # Half away from zero, not banker's rounding: 100.5 is +101 ...
        ("2.005", 101),
        ("2.004", 100),
        # ... and below 2, 100 / 0.32 = 312.5 is -313.
        ("1.32", -313),
        ("1.999", -100),
        ("1.941748", -106),
        ("1.980198", -102),
        ("1.123596", -809),
        ("3", 200),
        ("1", None),
        ("0.5", None),
    ],
)
def test_decimal_to_american_rounds_half_away_from_zero(decimal_odds, american):
    assert _american_from_decimal(Decimal(decimal_odds)) == american


def test_novig_tatum_example_converts_each_side():
    assert _prices("novig", _decimal_sides("1.941748", "1.980198")) == (-106, -102)


def test_underdog_serves_its_quoted_american_price_for_higher_and_lower():
    assert _prices("underdog", _american_sides(-112, -115)) == (-112, -115)


def test_underdog_one_sided_price_leaves_the_other_side_null():
    assert _prices("underdog", _american_sides(-112, None)) == (-112, None)


def test_prediction_market_tightness_boundary_at_one_ten():
    # 1/2 + 1/1.666667 = 1.09999988 is within 1.10; 1.666666 gives 1.10000024.
    assert _prices("polymarket", _decimal_sides("2", "1.666667")) == (100, -150)
    assert _prices("polymarket", _decimal_sides("2", "1.666666")) == (None, None)


def test_a_thin_polymarket_book_is_omitted():
    # The Tatum example: asks add up to 139 cents.
    assert _prices("polymarket", _decimal_sides("2.000000", "1.123596")) == (None, None)


@pytest.mark.parametrize("provider", ["novig", "polymarket", "kalshi"])
def test_a_one_sided_prediction_market_is_omitted(provider):
    assert _prices(provider, _decimal_sides("1.941748", None)) == (None, None)


def test_entry_scope_multipliers_never_become_prices():
    selections = tuple(
        Selection(
            selection_id=direction.value,
            direction=direction,
            price_kind="multiplier",
            price_value=Decimal(3),
            price_scope="entry",
        )
        for direction in (SelectionDirection.HIGHER, SelectionDirection.LOWER)
    )
    assert _prices("prizepicks", selections) == (None, None)
    assert _prices("dabble", selections) == (None, None)
    # An entry-scope price stated in a price form (decimal here) is still the slip's.
    priced_entry = tuple(
        Selection(
            selection_id=direction.value,
            direction=direction,
            price_kind="decimal",
            price_value=Decimal("2.5"),
            price_scope="entry",
        )
        for direction in (SelectionDirection.HIGHER, SelectionDirection.LOWER)
    )
    assert _prices("underdog", priced_entry) == (None, None)
    # A provider outside the four never serves, whatever its selection states.
    assert _prices("dabble", _american_sides(-110, -110)) == (None, None)
    # Even a provider that does price a pick yields nothing for an entry payout.
    assert _prices("underdog", selections) == (None, None)


def _pool_player(tmp_path, snapshots):
    engine = create_engine(f"sqlite:///{tmp_path / 'line-prices.sqlite3'}")
    run_migrations(engine)
    archive = ProjectionArchive(engine, CATALOG)
    query = NBAMarketQuery(season=SEASON)
    providers = []
    for snapshot in snapshots:
        archive.ingest_complete_snapshot(snapshot, query=query, accepted_at=OBSERVED_AT)
        providers.append(ProjectionArchiveReadScope(provider=snapshot.provider, query=query))
    event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": (OBSERVED_AT + timedelta(hours=1)).isoformat(),
        "status_text": "Scheduled",
        "status_code": 1,
        "first_observed_started_at": None,
    }

    class ScopedEvents:
        def get_events_by_ids(self, _season, game_ids):
            return (event,) if GAME_ID in game_ids else ()

    reader = LatestProjectionPlayerPoolReader(
        engine,
        tuple(providers),
        clock=lambda: OBSERVED_AT + timedelta(minutes=10),
        event_reader=ScopedEvents(),
    )
    (player,) = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID).players
    return player


def _snapshot(provider, market_id, statistic_id, threshold, selections):
    return _line_snapshot(
        provider,
        (
            _line_market(
                CATALOG,
                provider=provider,
                market_id=market_id,
                statistic_id=statistic_id,
                threshold=threshold,
                selections=selections,
            ),
        ),
    )


def test_the_pool_serves_prices_only_for_priced_pick_providers_at_their_line(tmp_path):
    entry = tuple(
        Selection(
            selection_id=direction.value,
            direction=direction,
            price_kind="multiplier",
            price_value=Decimal(3),
            price_scope="entry",
        )
        for direction in (SelectionDirection.HIGHER, SelectionDirection.LOWER)
    )
    player = _pool_player(
        tmp_path,
        [
            _snapshot("underdog", "ud", "points", "26.5", _american_sides(-112, -112)),
            _snapshot("novig", "nv", "points", "26.5", _decimal_sides("1.941748", "1.980198")),
            _snapshot("polymarket", "pm", "points", "26.5", _decimal_sides("2.000000", "1.123596")),
            _snapshot("prizepicks", "pp", "points", "26.5", entry),
        ],
    )

    assert player.lines == {
        "novig": {"PTS": 26.5},
        "polymarket": {"PTS": 26.5},
        "prizepicks": {"PTS": 26.5},
        "underdog": {"PTS": 26.5},
    }
    assert player.line_prices == {
        "novig": {"PTS": {"over": -106, "under": -102}},
        "underdog": {"PTS": {"over": -112, "under": -112}},
    }


def test_a_market_whose_line_conflicts_loses_its_prices_too(tmp_path):
    snapshot = _line_snapshot(
        "underdog",
        (
            _line_market(
                CATALOG,
                provider="underdog",
                market_id="a",
                statistic_id="points",
                threshold="26.5",
                selections=_american_sides(-112, -112),
            ),
            _line_market(
                CATALOG,
                provider="underdog",
                market_id="b",
                statistic_id="points",
                threshold="27.5",
                selections=_american_sides(-112, -112),
            ),
            _line_market(
                CATALOG,
                provider="underdog",
                market_id="c",
                statistic_id="rebounds",
                threshold="7.5",
                selections=_american_sides(-120, None),
            ),
        ),
    )

    player = _pool_player(tmp_path, [snapshot])

    assert player.lines == {"underdog": {"REB": 7.5}}
    assert player.line_prices == {"underdog": {"REB": {"over": -120, "under": None}}}


def test_one_provider_stating_two_price_pairs_at_one_line_omits_the_market(tmp_path):
    snapshot = _line_snapshot(
        "underdog",
        tuple(
            _line_market(
                CATALOG,
                provider="underdog",
                market_id=market_id,
                statistic_id="points",
                threshold="26.5",
                selections=_american_sides(over, -112),
            )
            for market_id, over in (("a", -112), ("b", -105))
        ),
    )

    player = _pool_player(tmp_path, [snapshot])

    assert player.lines == {"underdog": {"PTS": 26.5}}
    assert player.line_prices == {}
