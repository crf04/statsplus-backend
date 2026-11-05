"""Cadence and credit-guard policy for Odds API collection (#377, crf04/statsplus#430).

Everything here drives the pure planner with an injected clock and literal
quota values; no network and no database.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta

import pytest

from app.services.odds_schedule import (
    CALL_COST,
    EASTERN,
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

BEAT = timedelta(minutes=5)


def et(day, hour, minute=0, month=11, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


#: A 7:00pm ET tip on Thursday 2026-11-05 (EST) and a noon tip the same day.
TIP = et(5, 19)
NOON = et(5, 12)
NIGHT_BEFORE = et(4, 21)


def plan(now, events, *, tier=Tier.NORMAL, remaining=20000, reserve=1000, evaluated=None, upcoming=()):
    return plan_cycle(
        now=now,
        events=events,
        upcoming=upcoming,
        tier=tier,
        tier_evaluated_at=evaluated,
        remaining=remaining,
        reserve=reserve,
    )


def kinds(now, tip=TIP, *, last=None, populated=None, tier=Tier.NORMAL):
    event = EventState("e1", tip, last, False, populated)
    return [c.kind for c in plan(now, [event], tier=tier, evaluated=now).calls]


def walk(tip, start, tier=Tier.NORMAL, *, posted=lambda now: True, remaining=20000, delays=(0,), needs=None):
    """Calls made for one game, beat by beat from ``start`` until an hour
    after tip, as (New York time, kind); ``posted`` says whether a response at
    that instant carries props.  Beat ``i`` starts ``delays[i % len(delays)]``
    minutes late; ``needs`` collects (calls so far, normal ``budget_needs``)
    at each beat."""

    last = populated = None
    closing_done, made = False, []
    beat = start.astimezone(UTC)  # New York-aware arithmetic would follow the wall clock
    for i in range(int((tip + timedelta(hours=1) - beat) / BEAT)):
        now = beat + i * BEAT + timedelta(minutes=delays[i % len(delays)])
        if needs is not None:
            needs.append((len(made), budget_needs(now, [GameClock(tip, closing_done, populated)])[0]))
        event = EventState("e1", tip, last, closing_done, populated)
        for call in plan(now, [event], tier=tier, evaluated=now, remaining=remaining).calls:
            made.append((f"{now.astimezone(EASTERN):%d %H:%M}", call.kind))
            last = now
            populated = now if posted(now) else populated
            closing_done = closing_done or call.kind == "closing"
    return made


# -- cadence ---------------------------------------------------------------


def test_a_7pm_tip_polls_once_the_night_before_then_hourly_from_10_then_every_30_minutes():
    assert walk(TIP, NIGHT_BEFORE) == [
        ("04 22:00", "night"),
        ("05 10:00", "day"),
        ("05 11:00", "day"),
        ("05 12:00", "day"),
        ("05 13:00", "day"),
        ("05 14:00", "day"),
        ("05 15:00", "day"),
        ("05 16:00", "final"),
        ("05 16:30", "final"),
        ("05 17:00", "final"),
        ("05 17:30", "final"),
        ("05 18:00", "final"),
        ("05 18:30", "final"),
        ("05 18:50", "closing"),
    ]


def test_late_and_early_beats_serve_each_30_minute_slot_once():
    # Beats start 0-4 minutes late.  Chaining each poll off the last one let
    # 16:00, 16:28, 16:56, ... 18:48 through: seven final polls for six slots.
    needs = []
    made = walk(TIP, NIGHT_BEFORE, delays=(0, 1, 3, 0, 0, 2, 4), needs=needs)

    # The 16:00, 16:30, ... 18:30 slots, each served 2 minutes early to 4 late.
    assert [when for when, kind in made if kind == "final"] == [
        "05 16:00",
        "05 16:28",
        "05 17:03",
        "05 17:31",
        "05 17:59",
        "05 18:34",
    ]
    assert len(made) == 14
    assert needs[0] == (0, 14 * CALL_COST)
    # From every beat on, the guard's projection covers what is spent.
    assert all(need >= CALL_COST * (len(made) - before) for before, need in needs)


def test_empty_night_polls_repeat_hourly_until_one_is_populated():
    made = walk(TIP, NIGHT_BEFORE, posted=lambda now: now >= et(5, 0, 30))

    assert made[:5] == [
        ("04 22:00", "night"),
        ("04 23:00", "night"),
        ("05 00:00", "night"),
        ("05 01:00", "night"),  # the first populated answer ends the window
        ("05 10:00", "day"),
    ]


def test_a_night_with_no_props_stops_at_2am_and_nothing_polls_until_10am():
    made = walk(TIP, NIGHT_BEFORE, posted=lambda now: now >= et(5, 9))

    assert made[:5] == [
        ("04 22:00", "night"),
        ("04 23:00", "night"),
        ("05 00:00", "night"),
        ("05 01:00", "night"),
        ("05 10:00", "day"),
    ]


def test_what_each_instant_plans_for_a_7pm_tip():
    assert kinds(et(4, 21, 55)) == []
    assert kinds(et(4, 22)) == ["night"]
    assert kinds(et(4, 23), last=et(4, 22)) == ["night"]  # 22:00 answered empty
    assert kinds(et(4, 23), last=et(4, 22), populated=et(4, 22)) == []
    assert kinds(et(5, 1, 55), last=et(5, 1)) == []  # within the hour
    assert kinds(et(5, 2), last=et(5, 1)) == []
    assert kinds(et(5, 3)) == []
    assert kinds(et(5, 9, 55), last=et(4, 22)) == []
    assert kinds(et(5, 10), last=et(4, 22), populated=et(4, 22)) == ["day"]
    assert kinds(et(5, 10, 30), last=et(5, 10)) == []
    assert kinds(et(5, 16, 30), last=et(5, 16)) == ["final"]
    assert kinds(et(5, 16, 30), last=et(5, 16), tier=Tier.DEGRADED) == []
    assert kinds(et(5, 18, 52), last=et(5, 18, 30)) == ["closing"]


def test_a_noon_tip_polls_every_30_minutes_from_9am_despite_the_pause():
    assert kinds(et(5, 8, 55), NOON, last=et(4, 22)) == []
    assert kinds(et(5, 9), NOON, last=et(4, 22)) == ["final"]
    assert walk(NOON, NIGHT_BEFORE) == [
        ("04 22:00", "night"),
        ("05 09:00", "final"),
        ("05 09:30", "final"),
        ("05 10:00", "final"),
        ("05 10:30", "final"),
        ("05 11:00", "final"),
        ("05 11:30", "final"),
        ("05 11:50", "closing"),
    ]


def test_the_windows_follow_new_york_time_across_daylight_saving():
    march_tip = et(18, 19, month=3, year=2027)  # EDT, UTC-4

    # 10:00 ET is 15:00 UTC in November and 14:00 UTC in March.
    assert kinds(datetime(2026, 11, 5, 14, 55, tzinfo=UTC), last=et(4, 22)) == []
    assert kinds(datetime(2026, 11, 5, 15, tzinfo=UTC), last=et(4, 22)) == ["day"]
    assert kinds(datetime(2027, 3, 18, 14, tzinfo=UTC), march_tip) == ["day"]
    # 22:00 ET the night before is 03:00 UTC in November and 02:00 UTC in March.
    assert kinds(datetime(2026, 11, 5, 2, 55, tzinfo=UTC)) == []
    assert kinds(datetime(2026, 11, 5, 3, tzinfo=UTC)) == ["night"]
    assert kinds(datetime(2027, 3, 18, 2, tzinfo=UTC), march_tip) == ["night"]


def test_the_night_the_clocks_fall_back_holds_five_hourly_empty_polls():
    # Sunday 2026-11-01: 22:00 EDT Saturday to 02:00 EST is five hours.
    tip = et(1, 19)
    made = walk(tip, et(31, 21, month=10), posted=lambda now: False)

    assert [when for when, kind in made if kind == "night"] == [
        "31 22:00",
        "31 23:00",
        "01 00:00",
        "01 01:00",
        "01 01:00",
    ]


def test_degraded_tier_doubles_the_day_and_final_spacing_and_keeps_night_and_closing():
    assert walk(TIP, NIGHT_BEFORE, Tier.DEGRADED) == [
        ("04 22:00", "night"),
        ("05 10:00", "day"),
        ("05 12:00", "day"),
        ("05 14:00", "day"),
        ("05 16:00", "final"),
        ("05 17:00", "final"),
        ("05 18:00", "final"),
        ("05 18:50", "closing"),
    ]


def test_closing_tier_takes_only_the_closing_snapshot():
    assert walk(TIP, NIGHT_BEFORE, Tier.CLOSING) == [("05 18:50", "closing")]


def test_halted_tier_makes_no_call_at_all_while_the_quota_stays_low():
    assert walk(TIP, NIGHT_BEFORE, Tier.HALTED, remaining=5) == []


def test_a_halted_collector_reopens_when_the_quota_resets_without_waiting_an_hour():
    now = TIP - timedelta(hours=3)

    # Halted a minute ago on 5 credits; discovery now reports a fresh month.
    first = plan(now, [], tier=Tier.HALTED, evaluated=now - timedelta(minutes=1), remaining=20000)

    assert (first.tier, first.tier_evaluated_at) == (Tier.CLOSING, now)


def test_no_call_is_planned_at_or_after_tip():
    assert kinds(TIP) == []
    assert kinds(TIP + timedelta(seconds=1)) == []
    assert kinds(TIP - timedelta(seconds=1)) == []


def test_a_1am_tip_never_polls_its_night_window_after_tip():
    tip = et(5, 1)

    assert kinds(et(4, 23), tip) == ["final"]
    assert kinds(et(5, 0, 50), tip) == ["closing"]
    # Its 22:00-02:00 night window ends where its final window opens.
    assert kinds(et(5, 0, 57), tip) == []
    assert kinds(et(5, 1, 30), tip) == []


def test_the_closing_fetch_is_only_allowed_from_tip_minus_10_until_tip_minus_5():
    def at(offset):
        return kinds(TIP - offset)

    assert at(timedelta(minutes=10, seconds=1)) == ["final"]
    assert at(timedelta(minutes=10)) == ["closing"]
    assert at(timedelta(minutes=5, seconds=1)) == ["closing"]
    # A missed closing is not made up in the last five minutes.
    assert at(timedelta(minutes=5)) == []
    assert at(timedelta(minutes=1)) == []


# -- the guard ---------------------------------------------------------------


def test_budget_needs_for_one_game_seen_from_9pm_the_night_before():
    # 14 calls normal; 8 degraded (night, day 10/12/14, final 16/17/18,
    # closing); the closing alone.
    assert budget_needs(NIGHT_BEFORE, [GameClock(TIP)]) == (70, 40, 5)
    # A noon tip: night, six final polls from 9:00, closing; degraded polls
    # final at 9, 10 and 11.
    assert budget_needs(NIGHT_BEFORE, [GameClock(NOON)]) == (40, 25, 5)
    assert budget_needs(NIGHT_BEFORE, [GameClock(TIP), GameClock(NOON)]) == (110, 65, 10)


def test_budget_needs_drops_the_night_poll_once_it_is_populated_and_everything_after_closing():
    assert budget_needs(et(4, 23), [GameClock(TIP)]) == (70, 40, 5)
    assert budget_needs(et(4, 23), [GameClock(TIP, last_populated_at=et(4, 22))]) == (65, 35, 5)
    # Mid-afternoon: day 13, 14, 15 (degraded 13, 15), final and closing.
    assert budget_needs(et(5, 13), [GameClock(TIP, last_populated_at=et(5, 12))]) == (50, 30, 5)
    assert budget_needs(et(5, 18, 51), [GameClock(TIP, closing_done=True)]) == (0, 0, 0)
    assert budget_needs(TIP, [GameClock(TIP)]) == (0, 0, 0)


@pytest.mark.parametrize("tip", [TIP, NOON, et(5, 19, 30), et(1, 19)])
def test_budget_needs_is_what_the_planner_spends_from_that_instant(tip):
    def spend(start):
        return tuple(
            CALL_COST * len(walk(tip, start, tier)) for tier in (Tier.NORMAL, Tier.DEGRADED, Tier.CLOSING)
        )

    start = tip.astimezone(UTC) - timedelta(hours=24)
    while start < tip:
        assert budget_needs(start, [GameClock(tip)]) == spend(start), start
        start += BEAT


@pytest.mark.parametrize(
    "remaining, expected",
    [
        (1070, Tier.NORMAL),  # 70 available covers the normal cadence
        (1069, Tier.DEGRADED),  # one credit short of normal
        (1040, Tier.DEGRADED),  # exactly the 40 the degraded cadence needs
        (1039, Tier.CLOSING),  # short of degraded; closing is only 5
        (6, Tier.CLOSING),  # reserve funds the closing; 6-1 >= 5
        (5, Tier.HALTED),  # a call would take the quota to 0
        (None, Tier.NORMAL),  # nothing observed yet
    ],
)
def test_target_tier_thresholds_with_a_thousand_credit_reserve(remaining, expected):
    needs = budget_needs(NIGHT_BEFORE, [GameClock(TIP)])

    assert target_tier(remaining=remaining, reserve=1000, needs=needs) == expected


def test_tiers_move_one_step_per_evaluation_and_only_hourly():
    needs = (70, 40, 5)
    start = TIP - timedelta(hours=12)

    first = evaluate_tier(
        now=start, current=Tier.NORMAL, evaluated_at=None, remaining=100, reserve=1000, needs=needs
    )
    assert first == (Tier.DEGRADED, start)  # target is CLOSING, one step only

    within_the_hour = evaluate_tier(
        now=start + timedelta(minutes=59),
        current=Tier.DEGRADED,
        evaluated_at=start,
        remaining=100,
        reserve=1000,
        needs=needs,
    )
    assert within_the_hour == (Tier.DEGRADED, start)

    next_hour = evaluate_tier(
        now=start + timedelta(hours=1),
        current=Tier.DEGRADED,
        evaluated_at=start,
        remaining=100,
        reserve=1000,
        needs=needs,
    )
    assert next_hour == (Tier.CLOSING, start + timedelta(hours=1))

    recovering = evaluate_tier(
        now=start + timedelta(hours=2),
        current=Tier.CLOSING,
        evaluated_at=start + timedelta(hours=1),
        remaining=19000,
        reserve=1000,
        needs=needs,
    )
    assert recovering[0] == Tier.DEGRADED  # recovery is one step too
    assert step_tier(Tier.NORMAL, Tier.HALTED) == Tier.DEGRADED


def test_a_nearly_empty_quota_halts_immediately_without_waiting_for_the_hour():
    now = TIP - timedelta(hours=3)

    assert evaluate_tier(
        now=now,
        current=Tier.NORMAL,
        evaluated_at=now - timedelta(minutes=1),
        remaining=5,
        reserve=1000,
        needs=(70, 40, 5),
    ) == (Tier.HALTED, now)


def test_other_polls_cannot_spend_the_reserve_but_closings_can():
    events = [EventState(f"e{i}", TIP + timedelta(seconds=i), None, False) for i in range(3)]
    day_now = TIP - timedelta(hours=5)
    closing_now = TIP - timedelta(minutes=9)

    day = plan(day_now, events, remaining=1010)
    closings = plan(closing_now, events, remaining=1010)

    assert [c.kind for c in day.calls] == ["day", "day"]  # 1005, 1000; a third would dip to 995
    assert [c.kind for c in closings.calls] == ["closing"] * 3  # closings may use the reserve
    assert [c.kind for c in plan(closing_now, events, remaining=11).calls] == ["closing", "closing"]
    # 11 -> 6 -> 1: the second call leaves 1 and a third would reach 0.


def test_closings_are_planned_before_other_polls():
    soon = EventState("soon", TIP, None, False)
    later = EventState("later", TIP + timedelta(hours=2), None, False)

    calls = plan(TIP - timedelta(minutes=8), [later, soon]).calls

    assert [(c.odds_event_id, c.kind) for c in calls] == [("soon", "closing"), ("later", "final")]


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


def replay(tips, *, plan_credits=20000, posted_lead=None, reserve=1000):
    """Drive the planner beat by beat from the night before the first game.
    The fake API charges 5 once props are posted (``posted_lead`` before tip,
    or always when None) and answers empty, free, before."""

    clocks = {i: GameClock(tip) for i, tip in enumerate(tips)}
    last_poll, last_populated, closing_done = {}, {}, set()
    remaining, tier, evaluated = plan_credits, Tier.NORMAL, None
    spent = closings = paid_after_tip = 0
    lowest = remaining
    tier_seen = set()
    now = tips[0] - timedelta(hours=24)
    end = tips[-1] + timedelta(hours=1)
    horizon = timedelta(hours=36)
    while now < end:
        lo = bisect_left(tips, now)
        in_horizon = range(lo, bisect_right(tips, now + horizon))
        events = [
            EventState(str(i), tips[i], last_poll.get(i), i in closing_done, last_populated.get(i))
            for i in in_horizon
        ]
        result = plan_cycle(
            now=now,
            events=events,
            upcoming=[clocks[i] for i in range(lo, len(tips))],
            tier=tier,
            tier_evaluated_at=evaluated,
            remaining=remaining,
            reserve=reserve,
        )
        tier, evaluated = result.tier, result.tier_evaluated_at
        tier_seen.add(tier)
        for call in result.calls:
            i = int(call.odds_event_id)
            if now >= tips[i]:
                paid_after_tip += 1
            last_poll[i] = now
            if posted_lead is None or tips[i] - now <= posted_lead:
                remaining -= CALL_COST
                spent += CALL_COST
                last_populated[i] = now
            lowest = min(lowest, remaining)
            if call.kind == "closing":
                closing_done.add(i)
                closings += 1
            clocks[i] = GameClock(tips[i], i in closing_done, last_populated.get(i))
        now += BEAT
    return {
        "spent": spent,
        "lowest": lowest,
        "closings": closings,
        "after_tip": paid_after_tip,
        "tiers": tier_seen,
    }


def test_an_average_month_of_222_games_costs_16390_credits_with_props_up_the_night_before():
    # Tips every 30 minutes from 18:00 ET: 13, 14, 14, 15, 15, 16, 16, 17 paid
    # calls (each half hour later adds half a day poll), so a day of 8 games
    # is 120 calls and a day of 7 is 103.
    tips = month_of_games([8, 8, 8, 8, 8] + [7] * 26)
    assert len(tips) == 222

    result = replay(tips)

    assert result["spent"] == (5 * 120 + 26 * 103) * CALL_COST == 16390
    assert result["closings"] == 222
    assert result["after_tip"] == 0
    assert result["tiers"] == {Tier.NORMAL}


def test_an_empty_night_costs_nothing():
    # Props post 12 hours before tip, after every night poll and before 10:00 ET.
    tips = month_of_games([8, 8, 8, 8, 8] + [7] * 26)

    result = replay(tips, posted_lead=timedelta(hours=12))

    assert result["spent"] == 16390 - 222 * CALL_COST == 15280
    assert result["tiers"] == {Tier.NORMAL}


def test_a_peak_month_of_250_games_stays_at_normal_cadence_above_the_reserve():
    # 30 days of 8 and one of 10, whose 22:00 and 22:30 ET tips take 17 and 18.
    tips = month_of_games([8] * 30 + [10])
    assert len(tips) == 250

    result = replay(tips)

    assert result["spent"] == (30 * 120 + 120 + 17 + 18) * CALL_COST == 18775
    assert result["lowest"] == 20000 - 18775
    assert result["tiers"] == {Tier.NORMAL}


def test_ceiling_load_of_15_games_a_day_degrades_and_stays_under_the_plan():
    tips = month_of_games([15] * 31)
    assert len(tips) == 465

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

    assert result["spent"] < 19000
    assert result["lowest"] >= 1000  # the thousand-credit reserve is never touched
    assert result["closings"] == 775  # and no game lost its closing snapshot
    assert result["after_tip"] == 0
    assert {Tier.DEGRADED, Tier.CLOSING} <= result["tiers"]
