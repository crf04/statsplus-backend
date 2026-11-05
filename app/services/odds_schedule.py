"""Pure scheduling and credit-budget policy for Odds API collection (#377).

Nothing here touches a clock, a database, or the network, so the cadence and
the guard are testable with literal call counts.  ``OddsCollectionCoordinator``
supplies the facts (now, event state, quota) and carries out the plan.
"""

from __future__ import annotations

import math
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo

#: Credits one ``/events/{id}/odds`` call costs: 5 markets x 1 region.
CALL_COST = 5
#: The one extra fetch before tip, stored as the closing snapshot.
CLOSING_OPENS = timedelta(minutes=10)
#: The closing fetch is jittered to the beat, so tip-10 to tip-5; a missed one
#: is not made up later (the last pregame quote stands, unmarked).
CLOSING_CLOSES = timedelta(minutes=5)
#: The last three hours before tip poll twice as often as the day.
FINAL_OPENS = timedelta(hours=3)
HOURLY = timedelta(hours=1)
#: The night and pause windows follow the league's wall clock, DST included.
EASTERN = ZoneInfo("America/New_York")
#: Beats are five minutes apart; a slot may be served a little early so
#: latency jitter never pushes an hourly poll a whole beat late.
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


def _eastern(tip: datetime, days: int, hour: int) -> datetime:
    """``hour``:00 New York time, ``days`` after the tip's New York date."""

    day = tip.astimezone(EASTERN).date() + timedelta(days=days)
    return datetime.combine(day, time(hour), tzinfo=EASTERN).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Window:
    """One stretch of a game's polling schedule, located from its tip."""

    kind: str
    opens: Callable[[datetime], datetime]
    closes: Callable[[datetime], datetime]
    #: Spacing of the window's slots by tier, from where the window opens; a
    #: tier missing here makes no call.
    cadence: Mapping[Tier, timedelta]
    #: One populated (paid) poll ends the window for the game; empty answers
    #: are free and repeat at the cadence.
    once: bool = False


#: A game's schedule (crf04/statsplus#430), highest precedence first.  Each
#: window lies later in the game's day than the next, so a window is cut
#: short where a higher one opens, and one that starts after it ends is empty
#: (a noon tip's day window).  Outside every window nothing is polled: 02:00
#: to 10:00 ET on tip day, and before the night window.
WINDOWS = (
    # Zero spacing: every beat until the snapshot is taken (closing_done).
    Window(
        "closing",
        lambda tip: tip - CLOSING_OPENS,
        lambda tip: tip - CLOSING_CLOSES,
        {Tier.NORMAL: timedelta(0), Tier.DEGRADED: timedelta(0), Tier.CLOSING: timedelta(0)},
        once=True,
    ),
    Window(
        "final",
        lambda tip: tip - FINAL_OPENS,
        lambda tip: tip - CLOSING_OPENS,
        {Tier.NORMAL: HOURLY / 2, Tier.DEGRADED: HOURLY},
    ),
    Window(
        "day",
        lambda tip: _eastern(tip, 0, 10),
        lambda tip: tip - FINAL_OPENS,
        {Tier.NORMAL: HOURLY, Tier.DEGRADED: 2 * HOURLY},
    ),
    # Books post opening lines the evening before; one populated poll keeps them.
    Window(
        "night",
        lambda tip: _eastern(tip, -1, 22),
        lambda tip: _eastern(tip, 0, 2),
        {Tier.NORMAL: HOURLY, Tier.DEGRADED: HOURLY},
        once=True,
    ),
)


@dataclass(frozen=True, slots=True)
class Span:
    window: Window
    opens: datetime
    closes: datetime


@lru_cache(maxsize=4096)
def _spans(tip: datetime) -> tuple[Span, ...]:
    # Offsets from tip are elapsed time, never New York wall-clock arithmetic.
    tip = tip.astimezone(UTC)
    spans, cutoff = [], tip
    for window in WINDOWS:
        opens = window.opens(tip)
        spans.append(Span(window, opens, min(window.closes(tip), cutoff)))
        cutoff = min(cutoff, opens)
    return tuple(spans)


def window_at(tip: datetime, now: datetime) -> Span | None:
    """The window a game with this tip is in at ``now``, if any."""

    return next((span for span in _spans(tip) if span.opens <= now < span.closes), None)


def _populated_in(span: Span, last_populated_at: datetime | None) -> bool:
    return span.window.once and last_populated_at is not None and last_populated_at >= span.opens


def _slots(span: Span, cadence: timedelta) -> int:
    """How many slots ``span.opens + k * cadence`` lie before the span closes."""

    return math.ceil((span.closes - span.opens) / cadence)


def _slot(span: Span, cadence: timedelta, now: datetime) -> int:
    """Index of the latest slot ``now`` may serve; negative before the first.
    Slack lets a slot be served early, never adds one."""

    return min((now + CADENCE_SLACK - span.opens) // cadence, _slots(span, cadence) - 1)


@dataclass(frozen=True, slots=True)
class GameClock:
    """A scheduled game as the budget sees it."""

    commence_time: datetime
    closing_done: bool = False
    #: Completion of the last populated (paid) poll.
    last_populated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EventState:
    """An event inside the polling horizon."""

    odds_event_id: str
    commence_time: datetime
    #: Completion of the last successful (or empty) poll; failures excluded.
    last_poll_at: datetime | None = None
    closing_done: bool = False
    #: Completion of the last populated (paid) poll.
    last_populated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlannedCall:
    odds_event_id: str
    kind: str  # a Window.kind


@dataclass(frozen=True, slots=True)
class CyclePlan:
    tier: Tier
    tier_evaluated_at: datetime | None
    calls: tuple[PlannedCall, ...]


def next_reset(now: datetime, reset_day: int) -> datetime:
    """The next 00:00 UTC on ``reset_day`` (clamped to short months)."""

    now = now.astimezone(UTC)

    def at(year: int, month: int) -> datetime:
        day = min(reset_day, monthrange(year, month)[1])
        return datetime(year, month, day, tzinfo=UTC)

    candidate = at(now.year, now.month)
    if candidate > now:
        return candidate
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return at(year, month)


def _paid_calls(game: GameClock, now: datetime, tier: Tier) -> int:
    """Calls the planner makes for ``game`` from ``now`` to tip if every
    response is populated: each slot not yet past, the one ``now`` may serve
    included (it may already be served, so this can count one high)."""

    if game.closing_done:
        return 0
    calls = 0
    for span in _spans(game.commence_time):
        cadence = span.window.cadence.get(tier)
        if cadence is None or max(span.opens, now) >= span.closes:
            continue
        if span.window.once:
            calls += 0 if _populated_in(span, game.last_populated_at) else 1
        else:
            calls += _slots(span, cadence) - max(_slot(span, cadence, now), 0)
    return calls


def budget_needs(now: datetime, upcoming: Sequence[GameClock]) -> tuple[int, int, int]:
    """Credits to finish every scheduled game before reset, three ways.

    Returns ``(need_normal, need_degraded, need_closing)``: what the planner
    would spend from ``now`` to each tip in that tier, with props posted.
    """

    return tuple(
        CALL_COST * sum(_paid_calls(game, now, tier) for game in upcoming)
        for tier in (Tier.NORMAL, Tier.DEGRADED, Tier.CLOSING)
    )


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
) -> CyclePlan:
    """Decide which paid calls this beat makes.

    Closing snapshots go first and may spend the reserve; other polls may
    not.  No call is planned at or after tip.
    """

    needs = budget_needs(now, upcoming)
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

    due: list[tuple[EventState, str]] = []
    for event in events:
        span = None if event.closing_done else window_at(event.commence_time, now)
        cadence = None if span is None else span.window.cadence.get(tier)
        if cadence is None or _populated_in(span, event.last_populated_at):
            continue
        # A slot is served by any poll from CADENCE_SLACK before it, so each
        # slot is polled once however the beats drift.
        if (
            not cadence
            or event.last_poll_at is None
            or event.last_poll_at < span.opens + _slot(span, cadence, now) * cadence - CADENCE_SLACK
        ):
            due.append((event, span.window.kind))
    due.sort(key=lambda item: (item[1] != "closing", item[0].commence_time, item[0].odds_event_id))

    budget = math.inf if remaining is None else remaining
    calls: list[PlannedCall] = []
    for event, kind in due:
        floor = MIN_REMAINING_AFTER_CALL if kind == "closing" else max(reserve, MIN_REMAINING_AFTER_CALL)
        if budget - CALL_COST >= floor:
            calls.append(PlannedCall(event.odds_event_id, kind))
            budget -= CALL_COST
    return CyclePlan(tier, evaluated_at, tuple(calls))


__all__ = [
    "CALL_COST",
    "CLOSING_CLOSES",
    "CLOSING_OPENS",
    "EASTERN",
    "FINAL_OPENS",
    "HOURLY",
    "MIN_REMAINING_AFTER_CALL",
    "WINDOWS",
    "CyclePlan",
    "EventState",
    "GameClock",
    "PlannedCall",
    "Span",
    "Tier",
    "Window",
    "budget_needs",
    "evaluate_tier",
    "next_reset",
    "plan_cycle",
    "step_tier",
    "target_tier",
    "window_at",
]
