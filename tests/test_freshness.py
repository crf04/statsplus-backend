"""Contract tests for the shared freshness and time-window authority."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from app.domain.freshness import (
    MAX_TIME_WINDOW_SECONDS,
    MIN_TIME_WINDOW_SECONDS,
    TimeWindowDomainError,
    exact_seconds,
    time_window_seconds,
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
