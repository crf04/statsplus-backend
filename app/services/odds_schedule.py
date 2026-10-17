"""Pure scheduling and credit-budget policy for Odds API collection (#377).

Nothing here touches a clock, a database, or the network, so the cadence and
the guard are testable with literal call counts.  ``OddsCollectionCoordinator``
supplies the facts (now, event state, quota) and carries out the plan.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math

#: Credits one ``/events/{id}/odds`` call costs: 5 markets x 1 region.
CALL_COST = 5
#: First poll of an event: props that are not posted yet answer empty, free.
#: Thirteen hours of hourly polls plus the closing fetch is at most 14 paid
#: calls (70 credits) per game however early a book posts, so about 250 games
#: in a peak month cost at most 17,500 of the 20,000-credit plan
#: (crf04/statsplus#215).
POLL_WINDOW_OPENS = timedelta(hours=13)
#: The one extra fetch before tip, stored as the closing snapshot.
CLOSING_OPENS = timedelta(minutes=10)
#: The closing fetch is jittered to the beat, so tip-10 to tip-5; a missed one
#: is not made up later (the last pregame quote stands, unmarked).
CLOSING_CLOSES = timedelta(minutes=5)
HOURLY = timedelta(hours=1)
#: Beats are five minutes apart; a poll is due a little early so latency
#: jitter never pushes an hourly poll a whole beat late.
CADENCE_SLACK = timedelta(minutes=2)
TIER_REEVALUATION = timedelta(hours=1)
#: No call may leave fewer credits than this: ``x-requests-remaining`` never
#: reaches 0 because of this job.
MIN_REMAINING_AFTER_CALL = 1


class Tier(str, Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    CLOSING = "closing"
    HALTED = "halted"


_TIER_ORDER = (Tier.NORMAL, Tier.DEGRADED, Tier.CLOSING, Tier.HALTED)
_CADENCE = {Tier.NORMAL: HOURLY, Tier.DEGRADED: 2 * HOURLY}


@dataclass(frozen=True, slots=True)
class GameClock:
    """A scheduled game as the budget sees it."""

    commence_time: datetime
    closing_done: bool = False


@dataclass(frozen=True, slots=True)
class EventState:
    """An event inside the polling horizon."""

    odds_event_id: str
    commence_time: datetime
    #: Completion of the last successful (or empty) poll; failures excluded.
    last_poll_at: datetime | None = None
    closing_done: bool = False


@dataclass(frozen=True, slots=True)
class PlannedCall:
    odds_event_id: str
    kind: str  # "hourly" | "closing"


@dataclass(frozen=True, slots=True)
class CyclePlan:
    tier: Tier
    tier_evaluated_at: datetime | None
    calls: tuple[PlannedCall, ...]


def next_reset(now: datetime, reset_day: int) -> datetime:
    """The next 00:00 UTC on ``reset_day`` (clamped to short months)."""

    now = now.astimezone(timezone.utc)

    def at(year: int, month: int) -> datetime:
        day = min(reset_day, monthrange(year, month)[1])
        return datetime(year, month, day, tzinfo=timezone.utc)

    candidate = at(now.year, now.month)
    if candidate > now:
        return candidate
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return at(year, month)


def budget_needs(now: datetime, upcoming: Sequence[GameClock], lead_hours: int) -> tuple[int, int, int]:
    """Credits to finish every scheduled game before reset, three ways.

    Returns ``(need_normal, need_degraded, need_closing)``: hourly polls
    from the assumed posting lead plus the closing snapshot, half the hourly
    polls plus closing, and closing only.
    """

    normal = degraded = closing = 0
    for game in upcoming:
        hours_to_tip = (game.commence_time - now) / HOURLY
        if hours_to_tip <= 0:
            continue
        hourly = math.ceil(min(lead_hours, hours_to_tip))
        final = 0 if game.closing_done else 1
        normal += hourly + final
        degraded += math.ceil(hourly / 2) + final
        closing += final
    return CALL_COST * normal, CALL_COST * degraded, CALL_COST * closing


def target_tier(
    *,
    remaining: int | None,
    reserve: int,
    needs: tuple[int, int, int],
) -> Tier:
    """The tier the credits support right now, ignoring flapping control."""

    if remaining is None:
        return Tier.NORMAL
    if remaining < CALL_COST + MIN_REMAINING_AFTER_CALL:
        return Tier.HALTED
    available = remaining - reserve
    need_normal, need_degraded, need_closing = needs
    if available >= need_normal:
        return Tier.NORMAL
    if available >= need_degraded:
        return Tier.DEGRADED
    # The reserve exists to fund closing snapshots, so the closing-only tier
    # may spend it; only credits that cannot fund every remaining closing halt.
    if remaining - MIN_REMAINING_AFTER_CALL >= need_closing:
        return Tier.CLOSING
    return Tier.HALTED


def step_tier(current: Tier, target: Tier) -> Tier:
    """Move one step from ``current`` toward ``target``."""

    here, there = _TIER_ORDER.index(current), _TIER_ORDER.index(target)
    if here == there:
        return current
    return _TIER_ORDER[here + (1 if there > here else -1)]


def evaluate_tier(
    *,
    now: datetime,
    current: Tier,
    evaluated_at: datetime | None,
    remaining: int | None,
    reserve: int,
    needs: tuple[int, int, int],
) -> tuple[Tier, datetime | None]:
    """Re-evaluate hourly, one step at a time; a near-empty quota halts now."""

    if remaining is not None and remaining < CALL_COST + MIN_REMAINING_AFTER_CALL:
        return Tier.HALTED, now
    if (
        evaluated_at is not None
        and now - evaluated_at < TIER_REEVALUATION
        # A halted collector re-reads the quota every beat, so a monthly reset
        # seen in free discovery reopens it without waiting out the hour.
        and current is not Tier.HALTED
    ):
        return current, evaluated_at
    target = target_tier(remaining=remaining, reserve=reserve, needs=needs)
    return step_tier(current, target), now


def plan_cycle(
    *,
    now: datetime,
    events: Sequence[EventState],
    upcoming: Sequence[GameClock],
    tier: Tier,
    tier_evaluated_at: datetime | None,
    remaining: int | None,
    reserve: int,
    lead_hours: int,
) -> CyclePlan:
    """Decide which paid calls this beat makes.

    Closing snapshots go first and may spend the reserve; hourly polls may
    not.  No call is planned at or after tip.
    """

    needs = budget_needs(now, upcoming, lead_hours)
    tier, evaluated_at = evaluate_tier(
        now=now,
        current=tier,
        evaluated_at=tier_evaluated_at,
        remaining=remaining,
        reserve=reserve,
        needs=needs,
    )
    if tier is Tier.HALTED:
        return CyclePlan(tier, evaluated_at, ())

    closing = sorted(
        (
            event
            for event in events
            if not event.closing_done
            and event.commence_time - CLOSING_OPENS <= now < event.commence_time - CLOSING_CLOSES
        ),
        key=lambda event: (event.commence_time, event.odds_event_id),
    )
    hourly: list[EventState] = []
    cadence = _CADENCE.get(tier)
    if cadence is not None:
        hourly = sorted(
            (
                event
                for event in events
                if not event.closing_done
                and event.commence_time - POLL_WINDOW_OPENS <= now < event.commence_time - CLOSING_OPENS
                and (event.last_poll_at is None or event.last_poll_at + cadence - CADENCE_SLACK <= now)
            ),
            key=lambda event: (event.commence_time, event.odds_event_id),
        )

    budget = math.inf if remaining is None else remaining
    calls: list[PlannedCall] = []
    for kind, group, floor in (
        ("closing", closing, MIN_REMAINING_AFTER_CALL),
        ("hourly", hourly, max(reserve, MIN_REMAINING_AFTER_CALL)),
    ):
        for event in group:
            if budget - CALL_COST >= floor:
                calls.append(PlannedCall(event.odds_event_id, kind))
                budget -= CALL_COST
    return CyclePlan(tier, evaluated_at, tuple(calls))


__all__ = [
    "CALL_COST",
    "CLOSING_CLOSES",
    "CLOSING_OPENS",
    "CyclePlan",
    "EventState",
    "GameClock",
    "HOURLY",
    "MIN_REMAINING_AFTER_CALL",
    "POLL_WINDOW_OPENS",
    "PlannedCall",
    "Tier",
    "budget_needs",
    "evaluate_tier",
    "next_reset",
    "plan_cycle",
    "step_tier",
    "target_tier",
]
