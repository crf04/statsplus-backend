"""The 2026-27 closing-quote gap backfill and gap check (crf04/statsplus#429).

The client is the recorded-fixture fake; the event mapper, name resolver,
ledger, sportsbook quote store and the game-log reader are the real code over a
migrated SQLite database.  No test makes or could make an Odds API call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, insert, select

from app.models.closing_lines import ClosingLinePurchase, ClosingLineSpend
from app.models.sportsbook_odds import OddsApiPoll, SportsbookPropQuote
from app.services.closing_line_backfill import (
    ALL_ODDS_MARKETS,
    ClosingLineBackfill,
    ClosingQuoteWriter,
    target_games,
)
from app.services.closing_lines import (
    ALL_MARKETS,
    ClosingLineReader,
    ClosingLineStore,
    closing_quote_gaps,
)
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteStore
from tests.support.closing_lines_world import (
    BOS_NYK,
    BOS_NYK_EVENT,
    CLOCK,
    CURRY,
    DEN_LAL,
    DEN_LAL_EVENT,
    GSW_LAC,
    GSW_LAC_EVENT,
    JOKIC,
    LEBRON,
    TATUM,
    TIPS,
    FakeHistoricalClient,
    build_world,
)
from tests.support.odds_world import load

SEASON = "2026-27"
#: Every game of the world has tipped by then.
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


@pytest.fixture
def world(tmp_path):
    world = build_world(tmp_path, season=SEASON)
    world.store = ClosingLineStore(world.engine, lock_runs=False, market=ALL_MARKETS)
    world.quotes = SportsbookQuoteStore(world.engine)
    # The live collector closed two of the three games; DEN @ LAL is the gap.
    for game_id, event_id, player in ((GSW_LAC, GSW_LAC_EVENT, CURRY), (BOS_NYK, BOS_NYK_EVENT, TATUM)):
        _live_closing_poll(world, game_id, event_id, player)
    return world


def _live_closing_poll(world, game_id, event_id, player):
    observed = TIPS[game_id] - timedelta(minutes=7)
    world.quotes.record_poll(
        odds_event_id=event_id,
        game_id=game_id,
        started_at=observed,
        completed_at=observed,
        cost=5,
        remaining=90000,
        is_closing=True,
        quotes=[ResolvedQuote(player, "PTS", "draftkings", 27.5, -110, -110, observed)],
        commence_time=TIPS[game_id],
    )


def _client():
    client = FakeHistoricalClient()
    client.bodies[DEN_LAL_EVENT] = load("historical_event_odds_den_lal_five_markets")
    return client


def _backfill(world, client, *, max_credits=15000, names=None, covered=None):
    events = world.catalog_service.get_events(SEASON)
    if covered is None:
        covered = {GSW_LAC, BOS_NYK}
    names = names or world.names
    return ClosingLineBackfill(
        store=world.store,
        client=client,
        event_mapper=world.event_mapper,
        names=names,
        events=events,
        max_credits=max_credits,
        keep_remaining=5000,
        clock=lambda: CLOCK,
        season=SEASON,
        markets=ALL_ODDS_MARKETS,
        writer=ClosingQuoteWriter(ledger=world.store, names=names, quotes=world.quotes),
        started_by=NOW,
        covered=covered,
    )


def _closing_quotes(world, game_id):
    table = SportsbookPropQuote.__table__
    with world.engine.connect() as connection:
        rows = connection.execute(
            select(
                table.c.canonical_player_id,
                table.c.market_category,
                table.c.book,
                table.c.line,
                table.c.over_price,
                table.c.under_price,
            ).where(table.c.canonical_game_id == game_id, table.c.is_closing.is_(True))
        )
        return sorted(tuple(row) for row in rows)


def _gaps(world):
    events = world.catalog_service.get_events(SEASON)
    return [event["nba_game_id"] for event in closing_quote_gaps(world.engine, events, now=NOW)]


# -- the gap check ---------------------------------------------------------


def test_the_gap_check_names_the_started_game_without_closing_quotes(world):
    assert _gaps(world) == [DEN_LAL]


def test_the_gap_check_reports_none_when_every_game_is_closed_and_names_one_removed(world):
    _live_closing_poll(world, DEN_LAL, DEN_LAL_EVENT, LEBRON)
    assert _gaps(world) == []

    table = SportsbookPropQuote.__table__
    with world.engine.begin() as connection:
        connection.execute(delete(table).where(table.c.canonical_game_id == BOS_NYK))

    assert _gaps(world) == [BOS_NYK]


def _event(game_id, *, tip, status="Scheduled", classification="Regular Season", **extra):
    return {
        "nba_game_id": game_id,
        "scheduled_at": tip,
        "status_text": status,
        "classification": classification,
        **extra,
    }


def test_the_gap_check_leaves_out_games_not_yet_started_postponed_or_outside_the_game_log_phases(tmp_path):
    engine = build_world(tmp_path, season=SEASON).engine
    past = NOW - timedelta(hours=3)
    events = [
        _event("0022600001", tip=past),  # started: its tip has passed, status not yet final
        _event("0022600002", tip=NOW + timedelta(hours=2)),  # not started
        _event("0022600003", tip=past, status="Postponed"),
        _event("0012600004", tip=past, status="Final", classification="Preseason"),
        _event("0042600005", tip=past, status="Final", classification="Playoffs"),
    ]

    gaps = closing_quote_gaps(engine, events, now=NOW)

    assert [event["nba_game_id"] for event in gaps] == ["0022600001", "0042600005"]


# -- the backfill ----------------------------------------------------------


def test_the_backfill_buys_only_the_gap_game_for_all_five_markets_at_its_closing_snapshot(world):
    client = _client()

    summary = _backfill(world, client).run()

    assert client.odds_calls == [
        (
            DEN_LAL_EVENT,
            datetime(2025, 11, 5, 0, 25, tzinfo=UTC),
            (
                "player_points",
                "player_rebounds",
                "player_assists",
                "player_threes",
                "player_points_rebounds_assists",
            ),
        )
    ]
    assert (summary.planned, summary.bought, summary.credits) == (1, 1, 51)
    assert _gaps(world) == []


def test_the_bought_snapshot_is_stored_as_every_books_closing_quote_with_prices(world):
    _backfill(world, _client()).run()

    # Offshore Bovada is dropped; every allowlisted quote is a closing row.
    assert _closing_quotes(world, DEN_LAL) == [
        (LEBRON, "3PM", "fanduel", 1.5, -140, 115),
        (LEBRON, "AST", "draftkings", 7.5, 105, -125),
        (LEBRON, "PRA", "draftkings", 39.5, -110, -110),
        (LEBRON, "PRA", "fanduel", 40.5, -110, -110),
        (LEBRON, "PTS", "draftkings", 24.5, -115, -105),
        (LEBRON, "PTS", "fanduel", 25.5, -110, -110),
        (LEBRON, "REB", "fanduel", 7.5, -105, -115),
        (JOKIC, "PTS", "draftkings", 28.5, -110, -110),
        (JOKIC, "REB", "draftkings", 12.5, -120, 100),
        (JOKIC, "REB", "fanduel", 13.5, -110, -110),
    ]
    polls = OddsApiPoll.__table__
    with world.engine.connect() as connection:
        poll = connection.execute(select(polls).where(polls.c.canonical_game_id == DEN_LAL)).one()
    # A closing poll for the game's own tip, observed at the snapshot's time.
    assert (poll.odds_event_id, poll.is_closing, poll.outcome) == (DEN_LAL_EVENT, True, "complete")
    assert poll.commence_time.replace(tzinfo=UTC) == TIPS[DEN_LAL]
    assert poll.completed_at.replace(tzinfo=UTC) == datetime(2025, 11, 5, 0, 24, 41, tzinfo=UTC)


def test_the_game_logs_read_the_bought_close_one_book_per_market(world):
    _backfill(world, _client()).run()

    lines = ClosingLineReader(world.engine).markets_for_player(LEBRON, [DEN_LAL], season=SEASON)

    assert {market: (value.value, value.book) for market, value in lines[DEN_LAL].items()} == {
        "3PM": (1.5, "fanduel"),
        "AST": (7.5, "draftkings"),
        "PRA": (39.5, "draftkings"),
        "PTS": (24.5, "draftkings"),
        "REB": (7.5, "fanduel"),
    }


def test_the_purchase_is_recorded_on_the_ledger_with_its_credits(world):
    _backfill(world, _client()).run()

    table = ClosingLinePurchase.__table__
    with world.engine.connect() as connection:
        row = connection.execute(select(table)).one()
    assert (row.canonical_game_id, row.market_category, row.outcome, row.credits) == (
        DEN_LAL,
        "ALL",
        "complete",
        50,
    )


def test_a_rerun_spends_nothing_and_writes_nothing(world):
    _backfill(world, _client()).run()
    before = _closing_quotes(world, DEN_LAL)
    second = _client()

    summary = _backfill(world, second).run()

    assert (second.credits_spent, second.quota_calls, second.odds_calls) == (0, 0, [])
    assert (summary.planned, summary.credits) == (0, 0)
    assert _closing_quotes(world, DEN_LAL) == before


def test_a_crash_after_the_purchase_is_finished_from_the_ledger_without_a_call(world):
    class Crashes:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("database went away while resolving names")

    with pytest.raises(RuntimeError, match="went away"):
        _backfill(world, _client(), names=Crashes()).run()
    assert _closing_quotes(world, DEN_LAL) == []
    second = _client()

    summary = _backfill(world, second).run()

    assert second.odds_calls == [] and second.credits_spent == 0
    assert summary.resumed == 1
    assert len(_closing_quotes(world, DEN_LAL)) == 10


def test_finishing_a_purchase_twice_writes_its_quotes_once(world):
    class Crashes:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("killed after the quotes reached the ledger")

    with pytest.raises(RuntimeError, match="killed"):
        _backfill(world, _client(), names=Crashes()).run()
    (purchase,) = world.store.purchased()
    (game,) = [
        game
        for game in target_games(world.catalog_service.get_events(SEASON), started_by=NOW)
        if game.game_id == DEN_LAL
    ]
    writer = ClosingQuoteWriter(ledger=world.store, names=world.names, quotes=world.quotes)

    writer.complete(game, purchase)
    writer.complete(game, purchase)

    assert len(_closing_quotes(world, DEN_LAL)) == 10
    polls = OddsApiPoll.__table__
    with world.engine.connect() as connection:
        count = connection.execute(
            select(func.count()).select_from(polls).where(polls.c.canonical_game_id == DEN_LAL)
        ).scalar()
    assert count == 1


def test_the_cap_counts_only_this_seasons_spend(world):
    # 2025-26's campaign spent its credits on other game days.
    with world.engine.begin() as connection:
        connection.execute(
            insert(ClosingLineSpend.__table__).values(
                kind="events_list", subject="2025-04-13", credits=12464, remaining=None, spent_at=CLOCK
            )
        )

    summary = _backfill(world, _client(), max_credits=51).run()

    assert (summary.bought, summary.credits, summary.stop) == (1, 51, None)


def test_the_plan_names_the_gap_and_estimates_fifty_credits_a_game(world):
    plan = _backfill(world, _client()).plan()

    assert [game.game_id for game in plan.pending] == [DEN_LAL]
    assert (plan.games_total, plan.covered, plan.estimated_credits) == (3, 2, 51)


def test_an_hourly_quote_is_not_a_close_so_the_game_is_still_a_gap(world):
    observed = TIPS[DEN_LAL] - timedelta(hours=2)
    world.quotes.record_poll(
        odds_event_id=DEN_LAL_EVENT,
        game_id=DEN_LAL,
        started_at=observed,
        completed_at=observed,
        cost=5,
        remaining=90000,
        is_closing=False,
        quotes=[ResolvedQuote(LEBRON, "PTS", "draftkings", 23.5, -110, -110, observed)],
        commence_time=TIPS[DEN_LAL],
    )

    assert _gaps(world) == [DEN_LAL]
    assert [game.game_id for game in _backfill(world, _client()).plan().pending] == [DEN_LAL]


def test_a_started_game_the_catalog_has_not_marked_final_is_bought_too(world):
    events = [
        {**event, "status_text": "Scheduled", "status_code": 1} if event["nba_game_id"] == DEN_LAL else event
        for event in world.catalog_service.get_events(SEASON)
    ]
    backfill = ClosingLineBackfill(
        store=world.store,
        client=_client(),
        event_mapper=world.event_mapper,
        names=world.names,
        events=events,
        max_credits=15000,
        keep_remaining=5000,
        season=SEASON,
        markets=ALL_ODDS_MARKETS,
        started_by=NOW,
        covered={GSW_LAC, BOS_NYK},
    )

    assert [game.game_id for game in backfill.plan().pending] == [DEN_LAL]


# -- the gap check follows the game's current close (review of #703) ----------


def _reschedule(world, game_id, tip):
    from sqlalchemy import update

    from app.models.event_catalog import EventCatalogEntry

    table = EventCatalogEntry.__table__
    with world.engine.begin() as connection:
        connection.execute(update(table).where(table.c.nba_game_id == game_id).values(scheduled_at=tip))


def test_a_game_closed_only_for_an_earlier_tip_is_a_gap(world):
    _reschedule(world, BOS_NYK, TIPS[BOS_NYK] + timedelta(days=2))

    assert _gaps(world) == [DEN_LAL, BOS_NYK]
    assert [game.game_id for game in _backfill(world, _client(), covered=_covered(world)).plan().pending] == [
        DEN_LAL,
        BOS_NYK,
    ]


def test_a_game_whose_current_close_was_empty_is_a_gap(world):
    new_tip = TIPS[BOS_NYK] + timedelta(days=2)
    _reschedule(world, BOS_NYK, new_tip)
    world.quotes.record_unstored_poll(
        odds_event_id=BOS_NYK_EVENT,
        game_id=BOS_NYK,
        started_at=new_tip - timedelta(minutes=7),
        completed_at=new_tip - timedelta(minutes=7),
        outcome="empty",
        is_closing=True,
        cost=0,
        commence_time=new_tip,
    )

    assert _gaps(world) == [DEN_LAL, BOS_NYK]


def _covered(world):
    from app.services.closing_lines import games_with_closing_quotes

    return games_with_closing_quotes(world.engine, (DEN_LAL, GSW_LAC, BOS_NYK))


def test_a_backfill_after_an_empty_live_close_is_served_and_clears_the_gap(world):
    # The live closing request started before tip-5 and came back empty after
    # the 00:25 snapshot the backfill buys.
    answered = TIPS[DEN_LAL] - timedelta(minutes=3)
    world.quotes.record_unstored_poll(
        odds_event_id=DEN_LAL_EVENT,
        game_id=DEN_LAL,
        started_at=answered - timedelta(minutes=3),
        completed_at=answered,
        outcome="empty",
        is_closing=True,
        cost=0,
        commence_time=TIPS[DEN_LAL],
    )
    assert _gaps(world) == [DEN_LAL]

    summary = _backfill(world, _client(), covered=_covered(world)).run()

    assert (summary.bought, summary.credits) == (1, 51)
    assert _gaps(world) == []
    lines = ClosingLineReader(world.engine).markets_for_player(LEBRON, [DEN_LAL], season=SEASON)
    assert {market: value.book for market, value in lines[DEN_LAL].items()} == {
        "3PM": "fanduel",
        "AST": "draftkings",
        "PRA": "draftkings",
        "PTS": "draftkings",
        "REB": "fanduel",
    }
