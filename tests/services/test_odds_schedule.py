"""Cadence and credit-guard policy for Odds API collection (#377).

Everything here drives the pure planner with an injected clock and literal
quota values; no network and no database.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

import pytest

from app.services.odds_schedule import (
    EventState,
    GameClock,
    Tier,
    budget_needs,
    evaluate_tier,
    next_reset,
    plan_cycle,
    step_tier,
    target_tier,
)

UTC = timezone.utc
TIP = datetime(2026, 10, 22, 0, 30, tzinfo=UTC)
BEAT = timedelta(minutes=5)


def plan(now, events, *, tier=Tier.NORMAL, remaining=20000, reserve=1000, evaluated=None, upcoming=()):
    return plan_cycle(
        now=now,
        events=events,
        upcoming=upcoming,
        tier=tier,
        tier_evaluated_at=evaluated,
        remaining=remaining,
        reserve=reserve,
        lead_hours=10,
    )


def run_one_event(tier, remaining=20000):
    """Calls made for one game over its last 15 hours, beat by beat."""

    last, closing_done, made = None, False, []
    now = TIP - timedelta(hours=15)
    while now < TIP + timedelta(hours=1):
        event = EventState("e1", TIP, last, closing_done)
        for call in plan(now, [event], tier=tier, evaluated=now, remaining=remaining).calls:
            made.append((TIP - now, call.kind))
            last = now
            closing_done = closing_done or call.kind == "closing"
        now += BEAT
    return made


# -- cadence ---------------------------------------------------------------


def test_normal_tier_polls_hourly_from_tip_minus_14h_then_closes_at_tip_minus_10m():
    made = run_one_event(Tier.NORMAL)

    assert [offset for offset, _ in made] == [
        timedelta(hours=h) for h in range(14, 0, -1)
    ] + [timedelta(minutes=10)]
    assert [kind for _, kind in made] == ["hourly"] * 14 + ["closing"]


def test_degraded_tier_polls_every_two_hours_and_keeps_the_closing():
    made = run_one_event(Tier.DEGRADED)

    assert [offset for offset, _ in made] == [
        timedelta(hours=h) for h in (14, 12, 10, 8, 6, 4, 2)
    ] + [timedelta(minutes=10)]


def test_closing_tier_takes_only_the_closing_snapshot():
    assert run_one_event(Tier.CLOSING) == [(timedelta(minutes=10), "closing")]


def test_halted_tier_makes_no_call_at_all_while_the_quota_stays_low():
    assert run_one_event(Tier.HALTED, remaining=5) == []


def test_a_halted_collector_reopens_when_the_quota_resets_without_waiting_an_hour():
    now = TIP - timedelta(hours=3)

    # Halted a minute ago on 5 credits; discovery now reports a fresh month.
    first = plan(now, [], tier=Tier.HALTED, evaluated=now - timedelta(minutes=1), remaining=20000)

    assert (first.tier, first.tier_evaluated_at) == (Tier.CLOSING, now)


def test_no_call_is_planned_at_or_after_tip():
    event = EventState("e1", TIP, None, False)

    assert plan(TIP, [event]).calls == ()
    assert plan(TIP + timedelta(seconds=1), [event]).calls == ()
    assert plan(TIP - timedelta(seconds=1), [event]).calls == ()


def test_the_closing_fetch_is_only_allowed_from_tip_minus_10_until_tip_minus_5():
    event = EventState("e1", TIP, None, False)

    def kinds(offset):
        return [c.kind for c in plan(TIP - offset, [event]).calls]

    assert kinds(timedelta(minutes=10, seconds=1)) == ["hourly"]
    assert kinds(timedelta(minutes=10)) == ["closing"]
    assert kinds(timedelta(minutes=5, seconds=1)) == ["closing"]
    # A missed closing is not made up in the last five minutes.
    assert kinds(timedelta(minutes=5)) == []
    assert kinds(timedelta(minutes=1)) == []


def test_the_window_opens_exactly_fourteen_hours_before_tip():
    event = EventState("e1", TIP, None, False)

    assert plan(TIP - timedelta(hours=14, seconds=1), [event]).calls == ()
    assert len(plan(TIP - timedelta(hours=14), [event]).calls) == 1


# -- the guard ---------------------------------------------------------------


def test_budget_needs_for_one_game_twelve_hours_out():
    upcoming = [GameClock(TIP)]

    assert budget_needs(TIP - timedelta(hours=12), upcoming, 10) == (55, 30, 5)
    # Inside the lead only the remaining hours count, and a finished closing
    # costs nothing more.
    assert budget_needs(TIP - timedelta(hours=3), upcoming, 10) == (20, 15, 5)
    assert budget_needs(TIP - timedelta(hours=3), [GameClock(TIP, True)], 10) == (15, 10, 0)


@pytest.mark.parametrize(
    "remaining, expected",
    [
        (1055, Tier.NORMAL),     # 55 available covers 55 of hourly polls
        (1054, Tier.DEGRADED),   # one credit short of hourly
        (1030, Tier.DEGRADED),   # exactly the 30 needed at 2h cadence
        (1029, Tier.CLOSING),    # short of 2h cadence; closing is only 5
        (6, Tier.CLOSING),       # reserve funds the closing; 6-1 >= 5
        (5, Tier.HALTED),        # a call would take the quota to 0
        (None, Tier.NORMAL),     # nothing observed yet
    ],
)
def test_target_tier_thresholds_with_a_thousand_credit_reserve(remaining, expected):
    needs = budget_needs(TIP - timedelta(hours=12), [GameClock(TIP)], 10)

    assert target_tier(remaining=remaining, reserve=1000, needs=needs) == expected


def test_tiers_move_one_step_per_evaluation_and_only_hourly():
    needs = (55, 30, 5)
    start = TIP - timedelta(hours=12)

    first = evaluate_tier(now=start, current=Tier.NORMAL, evaluated_at=None,
                          remaining=100, reserve=1000, needs=needs)
    assert first == (Tier.DEGRADED, start)  # target is CLOSING, one step only

    within_the_hour = evaluate_tier(now=start + timedelta(minutes=59), current=Tier.DEGRADED,
                                    evaluated_at=start, remaining=100, reserve=1000, needs=needs)
    assert within_the_hour == (Tier.DEGRADED, start)

    next_hour = evaluate_tier(now=start + timedelta(hours=1), current=Tier.DEGRADED,
                              evaluated_at=start, remaining=100, reserve=1000, needs=needs)
    assert next_hour == (Tier.CLOSING, start + timedelta(hours=1))

    recovering = evaluate_tier(now=start + timedelta(hours=2), current=Tier.CLOSING,
                               evaluated_at=start + timedelta(hours=1), remaining=19000,
                               reserve=1000, needs=needs)
    assert recovering[0] == Tier.DEGRADED  # recovery is one step too
    assert step_tier(Tier.NORMAL, Tier.HALTED) == Tier.DEGRADED


def test_a_nearly_empty_quota_halts_immediately_without_waiting_for_the_hour():
    now = TIP - timedelta(hours=3)

    assert evaluate_tier(now=now, current=Tier.NORMAL, evaluated_at=now - timedelta(minutes=1),
                         remaining=5, reserve=1000, needs=(55, 30, 5)) == (Tier.HALTED, now)


def test_hourly_polls_cannot_spend_the_reserve_but_closings_can():
    events = [EventState(f"e{i}", TIP + timedelta(seconds=i), None, False) for i in range(3)]
    hourly_now = TIP - timedelta(hours=5)
    closing_now = TIP - timedelta(minutes=9)

    hourly = plan(hourly_now, events, remaining=1010)
    closings = plan(closing_now, events, remaining=1010)

    assert [c.kind for c in hourly.calls] == ["hourly", "hourly"]  # 1005, 1000; a third would dip to 995
    assert [c.kind for c in closings.calls] == ["closing"] * 3     # closings may use the reserve
    assert [c.kind for c in plan(closing_now, events, remaining=11).calls] == ["closing", "closing"]
    # 11 -> 6 -> 1: the second call leaves 1 and a third would reach 0.


def test_closings_are_planned_before_hourly_polls():
    soon = EventState("soon", TIP, None, False)
    later = EventState("later", TIP + timedelta(hours=8), None, False)

    calls = plan(TIP - timedelta(minutes=8), [later, soon]).calls

    assert [(c.odds_event_id, c.kind) for c in calls] == [("soon", "closing"), ("later", "hourly")]


def test_next_reset_is_the_next_midnight_utc_on_the_reset_day():
    assert next_reset(datetime(2026, 10, 15, 13, tzinfo=UTC), 20) == datetime(2026, 10, 20, tzinfo=UTC)
    assert next_reset(datetime(2026, 10, 20, 0, tzinfo=UTC), 20) == datetime(2026, 11, 20, tzinfo=UTC)
    assert next_reset(datetime(2026, 12, 25, tzinfo=UTC), 20) == datetime(2027, 1, 20, tzinfo=UTC)
    assert next_reset(datetime(2027, 2, 10, tzinfo=UTC), 31) == datetime(2027, 2, 28, tzinfo=UTC)


# -- a month replayed through the planner -----------------------------------


def month_of_games(per_day, first_day=datetime(2026, 11, 1, tzinfo=UTC)):
    tips = []
    for day, count in enumerate(per_day):
        start = first_day + timedelta(days=day, hours=23)
        tips.extend(start + timedelta(minutes=30 * i) for i in range(count))
    return sorted(tips)


def replay(tips, *, plan_credits=20000, lead_hours=10, reserve=1000):
    """Drive the planner beat by beat; the fake API charges 5 only once the
    props are posted (within ``lead_hours`` of tip) and answers empty before."""

    clocks = {tip_index: GameClock(tip) for tip_index, tip in enumerate(tips)}
    last_poll, closing_done = {}, set()
    remaining, tier, evaluated = plan_credits, Tier.NORMAL, None
    spent = closings = paid_after_tip = 0
    lowest = remaining
    tier_seen = set()
    now = tips[0] - timedelta(hours=15)
    end = tips[-1] + timedelta(hours=1)
    horizon = timedelta(hours=36)
    while now < end:
        lo = bisect_left(tips, now)
        in_horizon = range(lo, bisect_right(tips, now + horizon))
        events = [EventState(str(i), tips[i], last_poll.get(i), i in closing_done) for i in in_horizon]
        result = plan_cycle(
            now=now, events=events, upcoming=[clocks[i] for i in range(lo, len(tips))],
            tier=tier, tier_evaluated_at=evaluated, remaining=remaining,
            reserve=reserve, lead_hours=lead_hours,
        )
        tier, evaluated = result.tier, result.tier_evaluated_at
        tier_seen.add(tier)
        for call in result.calls:
            i = int(call.odds_event_id)
            if now >= tips[i]:
                paid_after_tip += 1
            cost = 5 if tips[i] - now <= timedelta(hours=lead_hours) else 0
            remaining -= cost
            spent += cost
            lowest = min(lowest, remaining)
            last_poll[i] = now
            if call.kind == "closing":
                closing_done.add(i)
                closings += 1
                clocks[i] = GameClock(tips[i], True)
        now += BEAT
    return {"spent": spent, "lowest": lowest, "closings": closings,
            "after_tip": paid_after_tip, "tiers": tier_seen}


def test_an_average_month_of_222_games_costs_exactly_55_credits_a_game():
    tips = month_of_games([8, 8, 8, 8, 8] + [7] * 26)
    assert len(tips) == 222

    result = replay(tips)

    assert result["spent"] == 222 * 55 == 12210
    assert result["closings"] == 222
    assert result["after_tip"] == 0
    assert result["tiers"] == {Tier.NORMAL}


def test_a_busy_250_game_month_stays_inside_the_plan_at_normal_cadence():
    tips = month_of_games([9] * 7 + [8] * 24 + [])
    assert len(tips) == 255

    result = replay(tips)

    assert result["spent"] == 255 * 55 == 14025
    assert result["tiers"] == {Tier.NORMAL}


def test_ceiling_load_of_15_games_a_day_degrades_and_stays_under_the_plan():
    tips = month_of_games([15] * 31)
    assert len(tips) == 465  # 465 x 55 = 25,575 would exceed 20,000

    result = replay(tips)

    assert result["spent"] < 20000
    assert result["lowest"] >= 1
    assert result["after_tip"] == 0
    assert Tier.DEGRADED in result["tiers"]
    assert result["closings"] == 465  # degraded keeps every closing snapshot


def test_an_unsustainable_month_walks_down_to_closing_only_and_never_hits_zero():
    tips = month_of_games([25] * 31)
    assert len(tips) == 775

    result = replay(tips)

    assert result["spent"] == 18980
    assert result["lowest"] == 1020  # the thousand-credit reserve is never touched
    assert result["closings"] == 775  # and no game lost its closing snapshot
    assert result["after_tip"] == 0
    assert {Tier.DEGRADED, Tier.CLOSING} <= result["tiers"]
