"""Tests for the shared NBA Stats retry configuration."""

from __future__ import annotations

import logging

import pytest
from urllib3.exceptions import MaxRetryError

from app.utils import telemetry
from app.utils.nba_api_config import RetryWithLogging


class _RetryableResponse:
    status = 500

    def get_redirect_location(self):
        return None


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def _retry_strategy(total: int) -> RetryWithLogging:
    return RetryWithLogging(
        total=total,
        backoff_factor=0,
        status_forcelist=[500],
        allowed_methods=["GET"],
    )


def test_terminal_retry_without_budget_does_not_count_as_retry(caplog):
    caplog.set_level(logging.WARNING, logger="app.utils.nba_api_config")
    strategy = _retry_strategy(total=0)

    with pytest.raises(MaxRetryError):
        with telemetry.provider_call(
            telemetry.PROVIDER_NBA_STATS, "retry_total_zero"
        ):
            strategy.increment(
                method="GET",
                url="https://stats.example.test/endpoint?token=secret",
                response=_RetryableResponse(),
            )

    event = telemetry.get_recorded_provider_events()[0]
    assert event["retry_count"] == 0
    assert caplog.records == []


def test_retry_event_counts_successful_transitions_only(caplog):
    caplog.set_level(logging.WARNING, logger="app.utils.nba_api_config")
    strategy = _retry_strategy(total=3)
    url = "https://stats.example.test/endpoint?token=secret"

    with pytest.raises(MaxRetryError):
        with telemetry.provider_call(
            telemetry.PROVIDER_NBA_STATS, "retry_total_three"
        ):
            for _ in range(3):
                strategy = strategy.increment(
                    method="GET", url=url, response=_RetryableResponse()
                )
            strategy.increment(
                method="GET", url=url, response=_RetryableResponse()
            )

    event = telemetry.get_recorded_provider_events()[0]
    assert event["retry_count"] == 3
    assert len(caplog.records) == 3
    assert all("/endpoint" in record.message for record in caplog.records)
    assert all("token=secret" not in record.message for record in caplog.records)

