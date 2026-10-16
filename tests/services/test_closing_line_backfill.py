"""The closing-line backfill (#433), driven through its own public seams.

The client is a fake that answers from the recorded historical fixtures and
counts its calls; everything else (event mapper, name resolver, ledger, line
table, coverage query) is the real code over a migrated SQLite database.  No
test makes or could make an Odds API call.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.models.closing_lines import ClosingLine, ClosingLinePurchase
from app.providers.odds_api import OddsApiError
from app.services.closing_line_backfill import (
    closing_snapshot_date,
    spread,
    target_games,
)
from app.services.closing_lines import coverage_report
from app.services.event_resolver import EventResolver
from app.services.odds_identity import OddsEventMapper
from tests.support.closing_lines_world import (
    BOS_NYK,
    BOS_NYK_EVENT,
    BRUNSON,
    CLOCK,
    CURRY,
    DEN_LAL,
    DEN_LAL_EVENT,
    GORDON,
    GSW_LAC,
    GSW_LAC_EVENT,
    HAYES,
    JOKIC,
    KAWHI,
    LEBRON,
    MURRAY,
    PAYTON,
    REAVES,
    SEASON,
    TATUM,
    FakeHistoricalClient,
    build_world,
)

UTC = timezone.utc


def _lines(world):
    table = ClosingLine.__table__
    with world.engine.connect() as connection:
        rows = connection.execute(
            select(
                table.c.canonical_game_id,
                table.c.canonical_player_id,
                table.c.book,
                table.c.line,
            ).order_by(table.c.canonical_game_id, table.c.canonical_player_id)
        )
        return {(game, player): (book, line) for game, player, book, line in rows}


def _ledger(world):
    table = ClosingLinePurchase.__table__
    with world.engine.connect() as connection:
        return {
            row.canonical_game_id: row
            for row in connection.execute(select(table)).all()
        }


# -- which games, which snapshot ---------------------------------------------


def test_target_games_are_the_completed_regular_season_and_playoff_games():
    def event(game_id, status_text="Final", status_code=3, **extra):
        return {
            "nba_game_id": game_id,
            "scheduled_at": "2026-04-20T23:00:00+00:00",
            "home_team_id": 1,
            "away_team_id": 2,
            "status_text": status_text,
            "status_code": status_code,
            "classification": "",
            **extra,
        }

    games = target_games(
        [
            event("0012500001"),  # preseason
            event("0022500001"),  # regular season
            event("0022500002", status_text="Scheduled", status_code=1),  # not played
            event("0022500003", postponed_status="Postponed", status_text="Postponed"),
            event("0042500101"),  # playoffs
            event("0052500101"),  # play-in
            event("0032500001"),  # all-star
        ]
    )

    assert [game.game_id for game in games] == ["0022500001", "0042500101"]


@pytest.mark.parametrize(
    ("tip", "asked"),
    [
        # Not on a mark: the mark before it.
        (datetime(2025, 11, 5, 0, 31, tzinfo=UTC), datetime(2025, 11, 5, 0, 30, tzinfo=UTC)),
        (datetime(2025, 11, 5, 0, 34, 59, tzinfo=UTC), datetime(2025, 11, 5, 0, 30, tzinfo=UTC)),
        # On a mark: the previous mark, since the mark at tip is the game itself.
        (datetime(2025, 11, 5, 0, 30, tzinfo=UTC), datetime(2025, 11, 5, 0, 25, tzinfo=UTC)),
        # Across an hour and a day.
        (datetime(2025, 11, 6, 0, 0, tzinfo=UTC), datetime(2025, 11, 5, 23, 55, tzinfo=UTC)),
        (datetime(2025, 11, 5, 1, 1, 30, tzinfo=UTC), datetime(2025, 11, 5, 1, 0, tzinfo=UTC)),
    ],
)
def test_the_closing_snapshot_asked_for_is_the_last_five_minute_mark_before_tip(tip, asked):
    assert closing_snapshot_date(tip) == asked


def test_a_game_belongs_to_the_us_eastern_day_it_tips_on_not_the_utc_day():
    from datetime import date

    from app.services.closing_line_backfill import TargetGame

    def day(tip):
        return TargetGame("g", tip, 1, 2).day

    # 02:00Z is 21:00 the evening before in November (EST) ...
    assert day(datetime(2025, 11, 5, 2, 0, tzinfo=UTC)) == date(2025, 11, 4)
    # ... and 23:00Z is the afternoon of the same Eastern day.
    assert day(datetime(2025, 11, 5, 23, 0, tzinfo=UTC)) == date(2025, 11, 5)
    # Daylight time: 02:00Z in April is 22:00 the evening before.
    assert day(datetime(2026, 4, 13, 2, 0, tzinfo=UTC)) == date(2026, 4, 12)


def test_spot_check_games_are_spread_over_the_season():
    games = list(range(10))

    assert spread(games, 3) == [0, 4, 9]
    assert spread(games, 2) == [0, 9]
    assert spread(games, 1) == [5]
    assert spread(games, 20) == games


# -- a first run ---------------------------------------------------------------


def test_a_first_run_stores_one_line_per_player_from_the_first_book_that_posted_him(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    summary = world.backfill(client).run()

    assert _lines(world) == {
        # DraftKings first, whoever else posted him (FanDuel and BetMGM had
        # LeBron, and offshore Bovada a 99.5 that is never read).
        (DEN_LAL, LEBRON): ("draftkings", 24.5),
        (DEN_LAL, REAVES): ("draftkings", 14.5),
        (DEN_LAL, JOKIC): ("draftkings", 28.5),
        # DraftKings posted Murray at two points; that book is skipped for him
        # and FanDuel's single line stands.
        (DEN_LAL, MURRAY): ("fanduel", 21.5),
        # DraftKings did not post Gordon: FanDuel.
        (DEN_LAL, GORDON): ("fanduel", 11.5),
        # Only BetMGM posted Hayes.
        (DEN_LAL, HAYES): ("betmgm", 6.5),
        (GSW_LAC, CURRY): ("draftkings", 27.5),
        (GSW_LAC, KAWHI): ("draftkings", 24.5),
    }
    assert (summary.bought, summary.empty, summary.failed, summary.unmatched) == (2, 1, 0, 0)
    assert summary.stop is None


def test_each_stored_line_names_its_snapshot_event_and_purchase_time(tmp_path):
    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient()).run()

    table = ClosingLine.__table__
    with world.engine.connect() as connection:
        row = connection.execute(
            select(table).where(
                table.c.canonical_game_id == DEN_LAL, table.c.canonical_player_id == LEBRON
            )
        ).one()

    assert (row.market_category, row.book, row.line) == ("PTS", "draftkings", 24.5)
    # The snapshot the API answered with, not the minute asked for (00:25:00).
    assert row.snapshot_at.replace(tzinfo=UTC) == datetime(2025, 11, 5, 0, 24, 41, tzinfo=UTC)
    assert row.odds_event_id == DEN_LAL_EVENT
    assert row.bought_at.replace(tzinfo=UTC) == CLOCK


def test_the_ledger_records_what_each_call_cost_and_returned(tmp_path):
    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient(remaining=100000)).run()

    ledger = _ledger(world)

    assert {game: (row.outcome, row.http_status, row.credits) for game, row in ledger.items()} == {
        DEN_LAL: ("complete", 200, 10),
        GSW_LAC: ("complete", 200, 10),
        # An empty snapshot is recorded too, at no cost, and not retried.
        BOS_NYK: ("empty", 200, 0),
    }
    # Day one: 1 for the event list, then 10 for each of its two games.
    assert (ledger[DEN_LAL].remaining, ledger[GSW_LAC].remaining) == (99989, 99979)
    assert ledger[DEN_LAL].unresolved_count == 1  # "Zach Unknown" is on no roster
    assert ledger[GSW_LAC].unresolved_count == 0
    assert ledger[DEN_LAL].requested_for.replace(tzinfo=UTC) == datetime(
        2025, 11, 5, 0, 25, tzinfo=UTC
    )
    # The snapshot the API answered with is kept beside the minute asked for.
    assert ledger[DEN_LAL].snapshot_at.replace(tzinfo=UTC) == datetime(
        2025, 11, 5, 0, 24, 41, tzinfo=UTC
    )
    assert ledger[GSW_LAC].snapshot_at.replace(tzinfo=UTC) == datetime(
        2025, 11, 5, 1, 54, 40, tzinfo=UTC
    )


def test_the_closing_snapshot_is_requested_for_the_player_points_market_only(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    world.backfill(client).run()

    assert client.odds_calls == [
        (DEN_LAL_EVENT, datetime(2025, 11, 5, 0, 25, tzinfo=UTC), ("player_points",)),
        (GSW_LAC_EVENT, datetime(2025, 11, 5, 1, 55, tzinfo=UTC), ("player_points",)),
        # Tip exactly on the mark: the mark before it.
        (BOS_NYK_EVENT, datetime(2025, 11, 5, 23, 55, tzinfo=UTC), ("player_points",)),
    ]


def test_event_ids_are_listed_once_per_game_day_in_a_window_around_its_games(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    world.backfill(client).run()

    assert client.event_calls == [
        # Nov 4 (Eastern): an hour before its first tip, covering both games.
        (
            datetime(2025, 11, 4, 23, 30, tzinfo=UTC),
            datetime(2025, 11, 4, 21, 30, tzinfo=UTC),
            datetime(2025, 11, 5, 5, 0, tzinfo=UTC),
        ),
        # Nov 5 (Eastern): the Celtics game, whose tip is past UTC midnight.
        (
            datetime(2025, 11, 5, 23, 0, tzinfo=UTC),
            datetime(2025, 11, 5, 21, 0, tzinfo=UTC),
            datetime(2025, 11, 6, 3, 0, tzinfo=UTC),
        ),
    ]


def test_credits_are_the_event_lists_plus_ten_per_game_with_props(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    summary = world.backfill(client).run()

    assert client.credits_spent == 22  # 2 event lists + 2 paid games; the empty one is free
    assert (summary.credits, summary.event_calls, summary.odds_calls) == (22, 2, 3)
    assert summary.remaining == 99978


# -- matching ---------------------------------------------------------------


def test_a_player_traded_since_is_matched_to_the_team_he_played_for_in_that_game(tmp_path):
    """The catalog lists Reaves with Golden State; the box score says the Lakers."""

    world = build_world(tmp_path)

    world.backfill(FakeHistoricalClient()).run()

    assert _lines(world)[(DEN_LAL, REAVES)] == ("draftkings", 14.5)


def test_a_finished_seasons_old_event_catalog_still_maps_its_events(tmp_path):
    """The live resolver refuses a catalog that was not refreshed lately."""

    world = build_world(tmp_path)
    live = OddsEventMapper(
        resolver=EventResolver(world.catalog_service, settings=world.settings),
        mapping_repository=None,
        season=SEASON,
    )
    client = FakeHistoricalClient()
    listed = client.historical_events(
        datetime(2025, 11, 4, 23, 30, tzinfo=UTC),
        datetime(2025, 11, 4, 21, 30, tzinfo=UTC),
        datetime(2025, 11, 5, 5, 0, tzinfo=UTC),
    ).events

    assert live.map(listed)[0] == []  # stale: nothing maps
    mapped, unresolved = world.event_mapper.map(listed)
    assert sorted(item.game_id for item in mapped) == [DEN_LAL, GSW_LAC]
    assert [event.home_team for event in unresolved] == ["Seattle SuperSonics"]


def test_a_game_with_no_odds_api_event_is_counted_and_not_bought(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.listed = [item for item in client.listed if item["id"] != GSW_LAC_EVENT]

    summary = world.backfill(client).run()

    assert summary.unmatched == 1
    assert GSW_LAC not in _ledger(world)
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT, BOS_NYK_EVENT]


# -- never buy a recorded game twice ----------------------------------------------


def test_a_rerun_spends_nothing_on_games_that_are_stored(tmp_path):
    world = build_world(tmp_path)
    first = FakeHistoricalClient()
    world.backfill(first).run()
    lines_before = _lines(world)
    second = FakeHistoricalClient()

    summary = world.backfill(second).run()

    assert (second.credits_spent, second.quota_calls) == (0, 0)
    assert second.event_calls == [] and second.odds_calls == []
    assert (summary.planned, summary.already_stored, summary.credits) == (0, 3, 0)
    assert _lines(world) == lines_before


def test_a_crash_after_the_purchase_is_recorded_does_not_buy_the_game_again(
    tmp_path, monkeypatch
):
    world = build_world(tmp_path)
    first = FakeHistoricalClient()

    def crash(*args, **kwargs):
        raise RuntimeError("killed after the receipt was written")

    monkeypatch.setattr(world.store, "store_quotes", crash)
    with pytest.raises(RuntimeError, match="killed"):
        world.backfill(first).run()
    monkeypatch.undo()

    assert _ledger(world)[DEN_LAL].outcome == "received"
    assert _ledger(world)[DEN_LAL].credits == 10
    second = FakeHistoricalClient()
    summary = world.backfill(second).run()

    assert DEN_LAL_EVENT not in [call[0] for call in second.odds_calls]
    assert summary.unparsed == 1  # reported: its credits are spent and recorded
    assert (DEN_LAL, LEBRON) not in _lines(world)
    assert (GSW_LAC, CURRY) in _lines(world)  # the rest of the season continues


def test_a_crash_while_resolving_is_finished_from_the_ledger_without_a_call(tmp_path):
    world = build_world(tmp_path)

    class Crashes:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("database went away while resolving names")

    with pytest.raises(RuntimeError, match="went away"):
        world.backfill(FakeHistoricalClient(), names=Crashes()).run()
    assert _ledger(world)[DEN_LAL].outcome == "purchased"
    assert _lines(world) == {}
    second = FakeHistoricalClient()

    summary = world.backfill(second).run()

    assert DEN_LAL_EVENT not in [call[0] for call in second.odds_calls]
    assert summary.resumed == 1
    assert _lines(world)[(DEN_LAL, LEBRON)] == ("draftkings", 24.5)
    assert _ledger(world)[DEN_LAL].outcome == "complete"


def test_a_failed_call_is_recorded_and_retried_by_the_next_run(tmp_path):
    world = build_world(tmp_path)
    flaky = FakeHistoricalClient()
    flaky.errors[GSW_LAC_EVENT] = OddsApiError("server", status=503, path="/odds")

    summary = world.backfill(flaky).run()

    failed = _ledger(world)[GSW_LAC]
    assert (failed.outcome, failed.http_status, failed.failure_reason) == ("failed", 503, "server")
    assert failed.credits is None
    assert summary.failed == 1 and summary.stop is None
    again = FakeHistoricalClient()

    world.backfill(again).run()

    assert [call[0] for call in again.odds_calls] == [GSW_LAC_EVENT]
    assert _ledger(world)[GSW_LAC].outcome == "complete"


@pytest.mark.parametrize("reason", ["unauthorized", "invalid_request", "rate_limited"])
def test_a_refused_key_or_a_rate_limit_stops_the_run_at_once(tmp_path, reason):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.errors[DEN_LAL_EVENT] = OddsApiError(reason, status=401, path="/odds")

    summary = world.backfill(client).run()

    assert summary.stop == reason
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT]


def test_repeated_failures_stop_the_run(tmp_path, monkeypatch):
    from app.services import closing_line_backfill

    monkeypatch.setattr(closing_line_backfill, "MAX_FAILURE_STREAK", 2)
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.errors = {
        event: OddsApiError("transport", path="/odds")
        for event in (DEN_LAL_EVENT, GSW_LAC_EVENT, BOS_NYK_EVENT)
    }

    summary = world.backfill(client).run()

    assert (summary.failed, summary.stop) == (2, "repeated_failures")
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT, GSW_LAC_EVENT]


def test_a_day_whose_event_list_fails_is_skipped_and_retried_by_the_next_run(tmp_path):
    world = build_world(tmp_path)
    broken = FakeHistoricalClient()
    broken.events_error = OddsApiError("server", status=503, path="/events")

    summary = world.backfill(broken).run()

    assert (summary.days_failed, summary.stop, summary.bought) == (2, None, 0)
    assert broken.odds_calls == [] and _ledger(world) == {}
    again = FakeHistoricalClient()

    world.backfill(again).run()

    assert set(_ledger(world)) == {DEN_LAL, GSW_LAC, BOS_NYK}


def test_repeated_event_list_failures_stop_the_run(tmp_path, monkeypatch):
    from app.services import closing_line_backfill

    monkeypatch.setattr(closing_line_backfill, "MAX_FAILURE_STREAK", 2)
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.events_error = OddsApiError("transport", path="/events")

    summary = world.backfill(client).run()

    assert (summary.days_failed, summary.stop) == (2, "repeated_failures")


# -- the guards -------------------------------------------------------------------


def test_the_spend_cap_stops_before_the_call_that_would_pass_it(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    summary = world.backfill(client, max_credits=20).run()

    # The event list (1) and one game (10) are 11; a second game would be 21.
    assert summary.stop == "max_credits"
    assert client.credits_spent == 11
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT]


def test_a_run_may_spend_exactly_its_cap_but_not_a_credit_more(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    summary = world.backfill(client, max_credits=21).run()

    # Both games of day one make exactly 21; day two's event list would be 22.
    assert summary.stop == "max_credits"
    assert client.credits_spent == 21
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT, GSW_LAC_EVENT]


def test_a_cap_equal_to_the_plans_estimate_lets_it_finish(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    backfill = world.backfill(client, max_credits=32)  # 3 games x 10 + 2 days x 1

    summary = backfill.run()

    # Every game call is assumed to cost its full 10 until it is answered, so
    # the free, empty Celtics game still needs 10 of headroom: 22 + 10 = 32.
    assert summary.stop is None and client.credits_spent == 22


@pytest.mark.parametrize(
    ("remaining", "event_lists", "games", "left"),
    [
        # 5022 -> list 5021 -> game 5011 -> game 5001 -> list 5000 (the floor
        # itself is allowed) -> the empty game would be assumed to cost 10:
        # 4990 is below the floor, so it is refused.
        (5022, 2, 2, 5000),
        # One credit less: the second list would end at 4999, below the floor.
        (5021, 1, 2, 5000),
        # Not even the first list may take the account below the floor.
        (5000, 0, 0, 5000),
    ],
)
def test_the_remaining_floor_stops_before_a_call_that_would_go_below_it(
    tmp_path, remaining, event_lists, games, left
):
    world = build_world(tmp_path)
    client = FakeHistoricalClient(remaining=remaining)

    summary = world.backfill(client, keep_remaining=5000).run()

    assert (len(client.event_calls), len(client.odds_calls)) == (event_lists, games)
    assert summary.stop == "keep_remaining"
    assert summary.remaining == client.remaining == left


def test_the_remaining_floor_holds_when_responses_omit_the_remaining_header(tmp_path):
    """The run counts its own spend from the account's last known balance."""

    world = build_world(tmp_path)
    client = FakeHistoricalClient(remaining=5021)
    client.omit_remaining_on_calls = True

    summary = world.backfill(client, keep_remaining=5000).run()

    # Same arithmetic as with the header: list (5020), two games (5000), then
    # day two's list would end at 4999, below the floor.
    assert (len(client.event_calls), len(client.odds_calls)) == (1, 2)
    assert summary.stop == "keep_remaining"
    assert summary.remaining == 5000


def test_an_account_whose_remaining_credits_are_unknown_is_not_spent_from(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.report_remaining = False

    summary = world.backfill(client).run()

    assert summary.stop == "remaining_unknown"
    assert client.event_calls == [] and client.odds_calls == []


def test_a_game_dearer_than_the_documented_price_ends_the_run(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.surcharge = 40

    summary = world.backfill(client).run()

    assert summary.stop == "unexpected_cost"
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT]
    assert _ledger(world)[DEN_LAL].credits == 50  # recorded, and not bought again


def test_the_backfill_refuses_a_client_for_any_other_region(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.regions = "us_dfs"

    with pytest.raises(ValueError, match="'us' only"):
        world.backfill(client)


# -- spot check, dry run, report ----------------------------------------------


def test_the_plan_estimates_ten_credits_a_game_and_one_per_game_day(tmp_path):
    world = build_world(tmp_path)

    plan = world.backfill(FakeHistoricalClient()).plan()

    assert (plan.games_total, plan.already_stored, len(plan.pending), plan.days) == (3, 0, 3, 2)
    assert plan.estimated_credits == 32


def test_a_plan_leaves_out_what_is_recorded_and_buys_what_failed(tmp_path):
    world = build_world(tmp_path)
    flaky = FakeHistoricalClient()
    flaky.errors[GSW_LAC_EVENT] = OddsApiError("server", status=503, path="/odds")
    world.backfill(flaky).run()

    plan = world.backfill(FakeHistoricalClient()).plan()

    assert [game.game_id for game in plan.pending] == [GSW_LAC]
    assert (plan.already_stored, plan.days, plan.estimated_credits) == (2, 1, 11)


def test_a_spot_check_buys_only_the_requested_number_of_games(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()

    summary = world.backfill(client).run(spot_check=2)

    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT, BOS_NYK_EVENT]
    assert set(_ledger(world)) == {DEN_LAL, BOS_NYK}
    assert summary.planned == 2


def test_a_spot_checked_game_is_not_bought_again_by_the_full_run(tmp_path):
    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient()).run(spot_check=1)  # the middle game
    full = FakeHistoricalClient()

    world.backfill(full).run()

    assert GSW_LAC_EVENT not in [call[0] for call in full.odds_calls]
    assert set(_ledger(world)) == {DEN_LAL, GSW_LAC, BOS_NYK}


def test_coverage_counts_players_with_twenty_minutes_by_source_book(tmp_path):
    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient()).run()

    report = coverage_report(world.engine, SEASON)

    # 20+ minutes: LeBron, Reaves, Jokic, Murray, Gordon | Curry, Payton, Kawhi
    # | Tatum, Brunson.  Hayes played 12 and is not counted.
    assert report.players == 10
    assert report.with_line == 7
    assert report.by_book == {"draftkings": 5, "fanduel": 2}
    assert report.missing == (
        (GSW_LAC, "Gary Payton II", 22.0),
        (BOS_NYK, "Jayson Tatum", 36.0),
        (BOS_NYK, "Jalen Brunson", 38.0),
    )
    assert report.games_by_outcome == {"complete": 2, "empty": 1}
    assert round(report.share, 3) == 0.7


def test_a_failed_attempt_is_not_counted_against_coverage(tmp_path):
    world = build_world(tmp_path)
    flaky = FakeHistoricalClient()
    flaky.errors[GSW_LAC_EVENT] = OddsApiError("server", status=503, path="/odds")
    world.backfill(flaky).run()

    report = coverage_report(world.engine, SEASON)

    assert report.games_by_outcome == {"complete": 1, "empty": 1}
    assert report.players == 7  # five on the Lakers game, two on the Celtics game


def test_the_credits_logged_per_call_never_carry_a_key_or_a_url(tmp_path, caplog):
    world = build_world(tmp_path)
    caplog.set_level("INFO")

    world.backfill(FakeHistoricalClient()).run()

    lines = [record.getMessage() for record in caplog.records if "historical" in record.getMessage()]
    assert lines == [
        "historical events day=2025-11-04 listed=3 cost=1 total=1 campaign=1 remaining=99999",
        "historical odds game=0022500101 date=2025-11-05T00:25Z cost=10 total=11 campaign=11 remaining=99989",
        "historical odds game=0022500102 date=2025-11-05T01:55Z cost=10 total=21 campaign=21 remaining=99979",
        "historical events day=2025-11-05 listed=1 cost=1 total=22 campaign=22 remaining=99978",
        "historical odds game=0022500103 date=2025-11-05T23:55Z cost=0 total=22 campaign=22 remaining=99978",
    ]
    assert not any("apikey" in text_.lower() or "http" in text_.lower() for text_ in lines)


def test_lines_are_rebuilt_from_the_ledger_alone(tmp_path):
    """The quotes kept on the ledger are enough to write every line again."""

    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient()).run()
    with world.engine.begin() as connection:
        connection.execute(text("DELETE FROM closing_lines"))
        connection.execute(
            text("UPDATE closing_line_purchases SET outcome = 'purchased' WHERE outcome = 'complete'")
        )
    rebuild = FakeHistoricalClient()

    summary = world.backfill(rebuild).run()

    assert rebuild.odds_calls == [] and summary.resumed == 2
    assert _lines(world)[(DEN_LAL, MURRAY)] == ("fanduel", 21.5)
    assert (GSW_LAC, PAYTON) not in _lines(world)
    assert (BOS_NYK, TATUM) not in _lines(world) and (BOS_NYK, BRUNSON) not in _lines(world)


# -- accounting that must hold when a response cannot be used --------------------


def test_a_paid_answer_that_is_not_json_is_recorded_with_its_cost_and_not_bought_again(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.undecodable.add(DEN_LAL_EVENT)

    summary = world.backfill(client).run()

    ledger = _ledger(world)[DEN_LAL]
    assert (ledger.outcome, ledger.http_status, ledger.credits) == ("received", 200, 10)
    assert (summary.unparsed, summary.credits) == (1, 22)  # its 10 credits are counted
    assert (DEN_LAL, LEBRON) not in _lines(world)
    again = FakeHistoricalClient()
    rerun = world.backfill(again).run()
    assert DEN_LAL_EVENT not in [call[0] for call in again.odds_calls]
    assert rerun.unparsed == 1


@pytest.mark.parametrize(
    "tamper",
    [
        lambda body: body["data"].update(id="00000000000000000000000000000fff"),
        lambda body: body.update(timestamp="2030-01-01T00:00:00Z"),
    ],
    ids=["another events data", "a snapshot after the date asked for"],
)
def test_a_body_that_cannot_be_the_snapshot_asked_for_is_quarantined_not_stored(tmp_path, tamper):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    tamper(client.bodies[DEN_LAL_EVENT])

    summary = world.backfill(client).run()

    ledger = _ledger(world)[DEN_LAL]
    assert (ledger.outcome, ledger.credits, ledger.quotes) == ("received", 10, None)
    assert summary.unparsed == 1
    assert not [key for key in _lines(world) if key[0] == DEN_LAL]
    assert (GSW_LAC, CURRY) in _lines(world)  # the other games are unaffected
    again = FakeHistoricalClient()
    tamper(again.bodies[DEN_LAL_EVENT])
    world.backfill(again).run()
    assert DEN_LAL_EVENT not in [call[0] for call in again.odds_calls]


def test_a_call_that_may_have_been_charged_counts_against_the_cap(tmp_path):
    """A timed-out request may have been served and billed before the drop."""

    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.errors[DEN_LAL_EVENT] = OddsApiError("transport", path="/odds")

    summary = world.backfill(client, max_credits=21).run()

    # list 1 + the unknown game assumed 10 + the next game 10 = 21; day two's
    # list would be 22.
    assert [call[0] for call in client.odds_calls] == [DEN_LAL_EVENT, GSW_LAC_EVENT]
    assert (summary.stop, summary.failed) == ("max_credits", 1)
    assert _ledger(world)[DEN_LAL].outcome == "failed"


def test_an_events_list_answered_with_a_body_that_is_not_json_counts_against_the_cap(tmp_path):
    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    client.events_error = OddsApiError("invalid_response", status=200, path="/events")

    summary = world.backfill(client, max_credits=1).run()

    assert len(client.event_calls) == 1
    assert (summary.days_failed, summary.stop) == (1, "max_credits")


def test_the_spend_cap_counts_the_whole_backfill_not_one_run(tmp_path):
    world = build_world(tmp_path)
    first = FakeHistoricalClient()
    first_summary = world.backfill(first, max_credits=11).run()
    assert (first_summary.stop, first.credits_spent) == ("max_credits", 11)
    assert world.store.credits_recorded() == 11  # one list and one game

    again = FakeHistoricalClient()
    second = world.backfill(again, max_credits=11).run()

    # Nothing more fits under the same cap, so the second run buys nothing.
    assert again.credits_spent == 0
    assert (second.stop, second.credits, second.campaign_credits) == ("max_credits", 0, 11)
    more = FakeHistoricalClient()
    third = world.backfill(more, max_credits=31).run()
    # With 11 already spent: a list (12), the second game (22), day two's list
    # (23); the empty third game is assumed to cost 10 and 33 passes the cap.
    assert more.credits_spent == 12
    assert (third.stop, third.credits, third.campaign_credits) == ("max_credits", 12, 23)


def test_every_events_list_is_logged_with_its_day_cost_and_size(tmp_path):
    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient()).run()

    with world.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT day, credits, remaining, events_listed FROM closing_line_event_lists ORDER BY list_id")
        ).all()

    assert [tuple(row) for row in rows] == [
        ("2025-11-04", 1, 99999, 3),
        ("2025-11-05", 1, 99978, 1),
    ]


def test_a_recorded_purchase_cannot_be_overwritten_by_a_later_attempt(tmp_path):
    from sqlalchemy.exc import IntegrityError

    world = build_world(tmp_path)
    world.backfill(FakeHistoricalClient()).run()
    before = _ledger(world)[DEN_LAL]

    with pytest.raises(IntegrityError):
        world.store.record_receipt(
            game_id=DEN_LAL, odds_event_id="e", requested_for=CLOCK, credits=0,
            remaining=1, bought_at=CLOCK,
        )
    with pytest.raises(IntegrityError):
        world.store.record_failure(
            game_id=DEN_LAL, odds_event_id="e", requested_for=CLOCK, http_status=500,
            reason="server", bought_at=CLOCK,
        )

    after = _ledger(world)[DEN_LAL]
    assert (after.outcome, after.credits, after.odds_event_id) == (
        before.outcome, before.credits, before.odds_event_id,
    ) == ("complete", 10, DEN_LAL_EVENT)


def test_a_paid_run_is_refused_without_a_lock_that_works_across_processes(tmp_path):
    from app.services.closing_lines import ClosingLineStore, RunRefused

    world = build_world(tmp_path)
    client = FakeHistoricalClient()
    locked = ClosingLineStore(world.engine)  # the default: SQLite cannot serialize runs

    with pytest.raises(RunRefused, match="needs PostgreSQL"):
        world.backfill(client, store=locked).run()

    assert client.quota_calls == 0 and client.odds_calls == [] and client.event_calls == []
    # Planning is a read and is fine on any database.
    assert world.backfill(client, store=locked).plan().estimated_credits == 32
