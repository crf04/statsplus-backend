"""One authority for exact durations, time windows, and the freshness boundary.

Three seams age the same observation: the provider snapshot cache decides
whether a Redis value may be served, the comparison board decides whether a
snapshot may enter a Comparison Group, and the configuration boundary decides
which windows either of them may be given at all.  If each stated the boundary
itself, one observation could be a stale cache read and a fresh comparison
member at the same instant, and a window the configuration accepted could be
refused when a request tried to use it.

This module is that single authority.  It imports no service and no provider
module, only the normalized numeric domain, so every seam can depend on it.

It states three things:

*Exact seconds.*  A duration is counted in whole microseconds and a configured
quantity is converted by a fully stated decimal context, so no age or window
depends on the ambient context a request was served inside.

*The time-window domain.*  A configured window is a duration a person could
operate: no finer than the microsecond every age is measured at, and no longer
than :data:`MAX_TIME_WINDOW_SECONDS`.  It is checked once, at startup, so
nothing a configuration accepted can be refused later by a request.

*The boundary.*  A fresh window is exclusive at its endpoint and a maximum age
is inclusive at its own, matching the two questions they answer: an observation
is fresh while it is *inside* its window, and it is usable while its age is *at
most* the permitted maximum.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
)

from app.domain.market_content import (
    MAX_EXACT_DIFFERENCE_SPAN,
    NumericDomainError,
    normalized_decimal,
)


#: Every decimal signal, trapped, so the context below can produce a number or
#: raise but never quietly record that it did something inexact.
_DECIMAL_SIGNALS = (
    Clamped,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
)


#: The finest window that can ever decide anything.  Every age this module
#: produces is counted in whole microseconds, so two windows closer together
#: than one microsecond classify every observation identically.
MIN_TIME_WINDOW_SECONDS = Decimal("1E-6")

#: The longest configurable window: a thousand million seconds, about
#: thirty-one years.  A window beyond it is not an operational policy but a
#: misconfiguration, and it is refused where the configuration is read rather
#: than where a request first tries to use it.
MAX_TIME_WINDOW_SECONDS = Decimal("1E+9")


#: The resolution every duration here is finally decided at.  A window becomes
#: a ``timedelta`` on this grid, which is the grid every observation age is
#: measured on, so two windows closer together than one microsecond classify
#: every observation identically.
_MICROSECONDS_PER_SECOND = Decimal(1_000_000)


class TimeWindowDomainError(NumericDomainError):
    """One configured time window outside the exact time-window domain.

    It is a :class:`NumericDomainError`, and so a ``ValueError``, because the
    configuration boundary already converts a ``ValueError`` into one
    sanitized ``ConfigurationError``.  Its message names the field and the
    domain, never the offending value.
    """


class TimeWindowPolicyError(NumericDomainError):
    """Two configured windows that cannot both be one cache policy.

    A fresh window longer than the age its value may still be served at is not
    an out-of-domain number -- each window is inside the domain on its own --
    so it is stated separately, and it is settled where the pair is built
    rather than where a request would first read the contradiction.
    """


def exact_context(precision: int) -> Context:
    """A context that states every field it depends on and inherits none.

    Precision, exponent range, rounding, capitals, and clamping are all named,
    and every signal is trapped, so a computation performed through this
    context's own methods either produces the exact number or raises -- it
    never quietly rounds, clamps, or records something inexact, and no
    thread-local state takes part in it.
    """

    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=list(_DECIMAL_SIGNALS),
    )


def exact_seconds(value: object) -> Decimal:
    """Convert a timedelta-like duration to exact decimal seconds.

    A duration is counted in whole microseconds, so the conversion is integer
    arithmetic and the result is written straight from those digits.  No
    decimal operation takes part, because every one of them reads the ambient
    context: under a caller's narrow precision the old division and addition
    rounded the age of an observation, and under a trapping context they
    refused to produce it at all, so one snapshot could be aged two ways or
    none.
    """

    days = getattr(value, "days", None)
    seconds = getattr(value, "seconds", None)
    microseconds = getattr(value, "microseconds", None)
    if days is None or seconds is None or microseconds is None:
        raise ValueError("an exact duration must be a timedelta")
    total = (days * 86400 + seconds) * 1_000_000 + microseconds
    sign = "-" if total < 0 else ""
    return Decimal(f"{sign}{abs(total)}E-6")


def exact_scaled_seconds(
    quantity: object, *, unit_seconds: int, field: str
) -> Decimal:
    """Convert a configured quantity of one time unit to exact seconds.

    A configured window is stated in hours, days, or seconds, and the board
    compares it against an age this module computed exactly, so the conversion
    must be exact too and must not depend on the context the request was served
    inside.  The quantity enters the normalized numeric domain first -- a
    nonfinite or absurdly scaled setting is one typed
    :class:`NumericDomainError`, never a silently rounded window -- and the
    multiplication is performed by a fully stated context wide enough for every
    digit both factors can carry.
    """

    if isinstance(quantity, bool):
        raise NumericDomainError(f"{field} must be a finite decimal")
    try:
        value = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise NumericDomainError(f"{field} must be a finite decimal") from error
    normalized_decimal(value, field=field)
    precision = MAX_EXACT_DIFFERENCE_SPAN + len(str(unit_seconds))
    return exact_context(precision).multiply(value, Decimal(unit_seconds))


def time_window_seconds(
    quantity: object, *, unit_seconds: int = 1, field: str
) -> Decimal:
    """One configured time window as exact seconds inside the window domain.

    This is where a window is accepted or refused, once, so every window the
    cache and the comparison board are handed can actually be used by both.
    The normalized numeric domain admits numbers no operator would mean as a
    duration -- ``1E-200`` seconds is finer than any clock here reads and
    ``1E+129`` seconds outlives the universe -- so a window is additionally
    bounded by :data:`MIN_TIME_WINDOW_SECONDS` and
    :data:`MAX_TIME_WINDOW_SECONDS`.
    """

    seconds = exact_scaled_seconds(quantity, unit_seconds=unit_seconds, field=field)
    if seconds < MIN_TIME_WINDOW_SECONDS or seconds > MAX_TIME_WINDOW_SECONDS:
        raise TimeWindowDomainError(
            f"{field} must be a duration between {MIN_TIME_WINDOW_SECONDS} and "
            f"{MAX_TIME_WINDOW_SECONDS} seconds"
        )
    return seconds


def exact_timedelta(seconds: Decimal, *, field: str) -> timedelta:
    """One whole-microsecond ``timedelta`` from exact seconds, built without a float.

    A duration handed to a service is compared against ages counted in whole
    microseconds, so the window becomes a ``timedelta`` on that same grid.  The
    seconds are bounded first, so the construction can never raise
    ``OverflowError`` for a duration no operator meant, and the scaling is
    performed by a fully stated context rather than by float arithmetic that
    would round a window differently than the age it decides.
    """

    value = _exact(seconds, field=field)
    if value < MIN_TIME_WINDOW_SECONDS or value > MAX_TIME_WINDOW_SECONDS:
        raise TimeWindowDomainError(
            f"{field} must be a duration between {MIN_TIME_WINDOW_SECONDS} and "
            f"{MAX_TIME_WINDOW_SECONDS} seconds"
        )
    precision = MAX_EXACT_DIFFERENCE_SPAN + len(str(_MICROSECONDS_PER_SECOND))
    scaled = exact_context(precision).multiply(value, _MICROSECONDS_PER_SECOND)
    # The one place a number is deliberately resolved rather than kept exact:
    # the microsecond grid the ages are already counted on.
    grid = exact_context(precision)
    grid.traps[Inexact] = False
    grid.traps[Rounded] = False
    return timedelta(microseconds=int(grid.to_integral_value(scaled)))


def time_window_timedelta(
    quantity: object, *, unit_seconds: int = 1, field: str
) -> timedelta:
    """One configured window as the exact duration a service compares with.

    This is the whole path a configured window takes: the normalized numeric
    domain, the time-window domain, and finally the microsecond grid.  Every
    seam that turns a setting or a direct override into a duration goes through
    it, so an operator, a service constructor, and a request all mean the same
    window, and an absurd one is one typed error rather than an
    ``OverflowError`` from the ``timedelta`` constructor.
    """

    return exact_timedelta(
        time_window_seconds(quantity, unit_seconds=unit_seconds, field=field),
        field=field,
    )


def cache_window_policy(
    fresh_seconds: object,
    stale_if_error_seconds: object,
    *,
    fresh_field: str = "fresh_seconds",
    stale_field: str = "stale_if_error_seconds",
) -> tuple[Decimal, Decimal]:
    """One cache policy: two windows in the domain, in the only usable order.

    The two windows are a single policy.  A value is served as a hit while it
    is inside the fresh window and as an explicit stale fallback while it is
    within the maximum age, so a fresh window longer than that maximum would
    make one observation both contemporaneous and past the age it may be
    served at.  Configuration and every runtime construction settle it here, so
    a directly built cache cannot hold a policy the configuration would refuse.
    """

    fresh_window = time_window_seconds(fresh_seconds, field=fresh_field)
    stale_window = time_window_seconds(stale_if_error_seconds, field=stale_field)
    if fresh_window > stale_window:
        raise TimeWindowPolicyError(
            f"{fresh_field} can never exceed {stale_field}"
        )
    return fresh_window, stale_window


def exact_age_seconds(value: object, *, field: str) -> Decimal:
    """One observation age as an exact, finite, non-negative decimal.

    An age crosses seams as evidence a reader is shown and a board compares, so
    it is normalized once, where it enters, rather than being re-judged by each
    reader.  A float or a string is admitted for the adapters that still
    produce one and is converted through its own digits, never through decimal
    arithmetic that would read an ambient context; a boolean, a nonfinite, a
    negative, or an absurdly scaled value is one typed error whose message
    names the field and never the value.
    """

    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise NumericDomainError(f"{field} must be an exact finite decimal")
    try:
        candidate = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise NumericDomainError(
            f"{field} must be an exact finite decimal"
        ) from error
    if not candidate.is_finite():
        raise NumericDomainError(f"{field} must be an exact finite decimal")
    normalized_decimal(candidate, field=field)
    if candidate < 0:
        raise NumericDomainError(f"{field} can never be negative")
    return candidate


def _exact(value: object, *, field: str) -> Decimal:
    """One exact decimal, because a float would compare inexactly at a boundary."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be an exact finite Decimal")
    return value


def within_fresh_window(age: Decimal, fresh_seconds: Decimal) -> bool:
    """Whether an observation of this age is still inside its fresh window.

    The endpoint is outside: an observation exactly one window old is no longer
    contemporaneous, it is the oldest thing the window would ever have covered.
    Every seam asks this one question, so a cache hit and a comparison member
    can never disagree about the same instant.
    """

    return _exact(age, field="an observation age") < _exact(
        fresh_seconds, field="a fresh window"
    )


def within_max_age(age: Decimal, max_age_seconds: Decimal) -> bool:
    """Whether an observation of this age is within a permitted maximum age.

    A maximum is inclusive at its endpoint, unlike a window: an age exactly at
    the maximum is the greatest age the policy permits, not the first one past
    it.  The stale-if-error ceiling and both canonical catalog TTLs are stated
    this way.
    """

    return _exact(age, field="an observation age") <= _exact(
        max_age_seconds, field="a maximum age"
    )


__all__ = [
    "MAX_TIME_WINDOW_SECONDS",
    "MIN_TIME_WINDOW_SECONDS",
    "TimeWindowDomainError",
    "TimeWindowPolicyError",
    "cache_window_policy",
    "exact_age_seconds",
    "exact_context",
    "exact_scaled_seconds",
    "exact_seconds",
    "exact_timedelta",
    "time_window_seconds",
    "time_window_timedelta",
    "within_fresh_window",
    "within_max_age",
]
