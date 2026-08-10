"""Contract tests for the shared freshness and time-window authority."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from app.domain.freshness import (
    MAX_TIME_WINDOW_SECONDS,
    MIN_TIME_WINDOW_SECONDS,
    TimeWindowDomainError,
    TimeWindowPolicyError,
    cache_window_policy,
    exact_age_seconds,
    exact_seconds,
    exact_timedelta,
    time_window_seconds,
    time_window_timedelta,
    within_fresh_window,
    within_max_age,
)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        ("299.999999", True),
        ("300", False),
        ("300.000001", False),
    ],
)
def test_a_fresh_window_excludes_its_own_endpoint(age, expected) -> None:
    assert within_fresh_window(Decimal(age), Decimal(300)) is expected


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        ("1799.999999", True),
        ("1800", True),
        ("1800.000001", False),
    ],
)
def test_a_maximum_age_includes_its_own_endpoint(age, expected) -> None:
    assert within_max_age(Decimal(age), Decimal(1800)) is expected


@pytest.mark.parametrize("predicate", [within_fresh_window, within_max_age])
def test_a_boundary_is_never_decided_against_an_inexact_number(predicate) -> None:
    with pytest.raises(ValueError):
        predicate(300.0, Decimal(300))
    with pytest.raises(ValueError):
        predicate(Decimal(300), 300.0)


def test_a_duration_is_exact_seconds_from_whole_microseconds() -> None:
    assert exact_seconds(timedelta(hours=1 / 3)) == Decimal("1200")
    assert str(exact_seconds(timedelta(hours=1 / 3))) == "1200.000000"


@pytest.mark.parametrize(
    ("quantity", "unit_seconds", "expected"),
    [
        (300, 1, "300"),
        ("0.5", 3600, "1800"),
        (7, 86400, "604800"),
        ("1E-6", 1, "0.000001"),
        ("1E+9", 1, "1000000000"),
        ("277777.7777777777", 3600, "999999999.99999972"),
    ],
)
def test_a_configured_window_converts_to_exact_seconds(
    quantity, unit_seconds, expected
) -> None:
    with localcontext() as context:
        context.prec = 4
        value = time_window_seconds(quantity, unit_seconds=unit_seconds, field="window")
    assert value == Decimal(expected)


@pytest.mark.parametrize(
    "quantity",
    [
        True,
        False,
        float("inf"),
        float("nan"),
        "not-a-number",
        None,
        0,
        -1,
        "1E-200",
        "1E+129",
        "0.0000009",
        "1000000000.000001",
    ],
)
def test_a_window_outside_the_time_window_domain_is_refused(quantity) -> None:
    with pytest.raises(ValueError) as error:
        time_window_seconds(quantity, unit_seconds=1, field="a window")

    assert "a window" in str(error.value)


def test_the_time_window_domain_boundaries_are_themselves_accepted() -> None:
    assert time_window_seconds(
        MIN_TIME_WINDOW_SECONDS, unit_seconds=1, field="window"
    ) == MIN_TIME_WINDOW_SECONDS
    assert time_window_seconds(
        MAX_TIME_WINDOW_SECONDS, unit_seconds=1, field="window"
    ) == MAX_TIME_WINDOW_SECONDS
    with pytest.raises(TimeWindowDomainError):
        time_window_seconds(
            MAX_TIME_WINDOW_SECONDS + Decimal("0.000001"),
            unit_seconds=1,
            field="window",
        )


@pytest.mark.parametrize(
    ("quantity", "unit_seconds", "expected"),
    [
        (72, 3600, timedelta(hours=72)),
        (7, 86400, timedelta(days=7)),
        ("0.5", 3600, timedelta(minutes=30)),
        ("1E-6", 1, timedelta(microseconds=1)),
        ("1E+9", 1, timedelta(seconds=1_000_000_000)),
        (Decimal("6"), 3600, timedelta(hours=6)),
    ],
)
def test_a_configured_window_becomes_a_whole_microsecond_timedelta(
    quantity, unit_seconds, expected
) -> None:
    with localcontext() as context:
        context.prec = 4
        window = time_window_timedelta(
            quantity, unit_seconds=unit_seconds, field="a window"
        )

    assert window == expected
    assert window.microseconds == expected.microseconds


@pytest.mark.parametrize(
    "quantity",
    [
        "1e129",
        "1E-200",
        0,
        -1,
        True,
        float("inf"),
        float("nan"),
        None,
        "277777.7777777778",
    ],
)
def test_a_window_outside_the_domain_never_reaches_timedelta(quantity) -> None:
    with pytest.raises(ValueError) as error:
        time_window_timedelta(quantity, unit_seconds=3600, field="a window")

    assert "a window" in str(error.value)
    assert "1e129" not in str(error.value)
    assert "277777" not in str(error.value)


@pytest.mark.parametrize(
    ("seconds", "expected_microseconds"),
    [
        ("300.0000005", 300_000_000),
        ("300.0000015", 300_000_002),
        ("1199.99999999999988", 1_200_000_000),
    ],
)
def test_a_window_finer_than_a_microsecond_lands_on_the_microsecond_grid(
    seconds, expected_microseconds
) -> None:
    with localcontext() as context:
        context.prec = 3
        window = exact_timedelta(Decimal(seconds), field="a window")

    assert window // timedelta(microseconds=1) == expected_microseconds


def test_a_timedelta_is_built_from_exact_seconds_only() -> None:
    with pytest.raises(ValueError):
        exact_timedelta(300.0, field="a window")


def test_a_cache_policy_accepts_a_fresh_window_at_its_stale_ceiling() -> None:
    fresh, stale = cache_window_policy(
        300, "300", fresh_field="fresh_seconds", stale_field="stale_if_error_seconds"
    )

    assert fresh == Decimal(300)
    assert stale == Decimal(300)


def test_a_cache_policy_refuses_a_fresh_window_past_its_stale_ceiling() -> None:
    with pytest.raises(TimeWindowPolicyError) as error:
        cache_window_policy(
            "300.000001",
            300,
            fresh_field="fresh_seconds",
            stale_field="stale_if_error_seconds",
        )

    assert "fresh_seconds" in str(error.value)
    assert "stale_if_error_seconds" in str(error.value)


@pytest.mark.parametrize(
    ("fresh", "stale"),
    [("0", 300), (300, "1e129"), (300, float("nan")), (True, 300)],
)
def test_a_cache_policy_bounds_both_windows_before_ordering(fresh, stale) -> None:
    with pytest.raises(ValueError):
        cache_window_policy(
            fresh,
            stale,
            fresh_field="fresh_seconds",
            stale_field="stale_if_error_seconds",
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("12.5"), "12.5"),
        ("12.5", "12.5"),
        (0, "0"),
        (12, "12"),
        (0.5, "0.5"),
    ],
)
def test_an_observation_age_normalizes_to_one_exact_finite_decimal(
    value, expected
) -> None:
    with localcontext() as context:
        context.prec = 1
        age = exact_age_seconds(value, field="a cache age")

    assert isinstance(age, Decimal)
    assert age == Decimal(expected)
    assert age.is_finite()


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "nan",
        "-inf",
        True,
        False,
        -1,
        Decimal("-0.000001"),
        "1E+129",
        "1E-129",
        "not-a-number",
        None,
        object(),
    ],
)
def test_an_unusable_observation_age_is_one_sanitized_error(value) -> None:
    with pytest.raises(ValueError) as error:
        exact_age_seconds(value, field="a cache age")

    assert "a cache age" in str(error.value)
    assert str(value) not in str(error.value)


def test_an_observation_age_holds_the_exact_domain_boundaries() -> None:
    assert exact_age_seconds("1E+128", field="a cache age") == Decimal("1E+128")
    assert exact_age_seconds("1E-128", field="a cache age") == Decimal("1E-128")
