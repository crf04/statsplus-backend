"""Telemetry snapshot, counter, and logging-safety contract for #12.

The module-level buffer and counters must stay thread-safe and bounded, must
distinguish provider failures (counted at the provider seams) from application
failures (counted by the central error handler), and must never carry
credentials, headers, URLs, bodies, or exception text.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
import requests
from flask import Flask

from app.providers.dfs import DeadlineExceededError
import app.utils.telemetry as telemetry


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def test_provider_event_records_all_documented_fields():
    with telemetry.provider_call(
        telemetry.PROVIDER_NBA_STATS,
        "player_game_logs",
        cache_status=telemetry.CACHE_DISABLED,
    ):
        pass

    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    event = events[0]
    assert set(event) == {
        "provider",
        "operation",
        "outcome",
        "started_at",
        "duration_ms",
        "retry_count",
        "cache_status",
        "request_id",
        "status_code",
    }
    assert event["provider"] == telemetry.PROVIDER_NBA_STATS
    assert event["operation"] == "player_game_logs"
    assert event["outcome"] == telemetry.OUTCOME_SUCCESS
    assert event["retry_count"] == 0
    assert event["cache_status"] == telemetry.CACHE_DISABLED
    assert event["status_code"] is None


def test_provider_event_includes_status_code_when_set():
    with telemetry.provider_call(telemetry.PROVIDER_PBP_STATS, "get_totals") as tracker:
        tracker.status_code = 200

    event = telemetry.get_recorded_provider_events()[0]
    assert event["status_code"] == 200


def test_provider_call_uses_explicit_request_id_and_closed_dfs_catalog():
    with telemetry.provider_call(
        telemetry.PROVIDER_UNDERDOG,
        "get_snapshot",
        request_id="retrieval-123",
    ):
        pass

    event = telemetry.get_recorded_provider_events()[0]
    assert event["request_id"] == "retrieval-123"
    assert "get_snapshot" in telemetry.PROVIDER_OPERATION_CATALOG[
        telemetry.PROVIDER_UNDERDOG
    ]
    assert "get_snapshot" in telemetry.PROVIDER_OPERATION_CATALOG[
        telemetry.PROVIDER_PRIZEPICKS
    ]

    with pytest.raises(ValueError, match="request_id"):
        telemetry.provider_call(
            telemetry.PROVIDER_UNDERDOG,
            "get_snapshot",
            request_id="unsafe request id",
        )


def test_event_outcome_distinguishes_timeout_http_and_malformed():
    with pytest.raises(requests.exceptions.ReadTimeout):
        with telemetry.provider_call(telemetry.PROVIDER_NBA_STATS, "timeout_op"):
            raise requests.exceptions.ReadTimeout("upstream timed out")
    with pytest.raises(requests.exceptions.HTTPError):
        with telemetry.provider_call(telemetry.PROVIDER_NBA_STATS, "http_op"):
            raise requests.exceptions.HTTPError("500")
    with pytest.raises(telemetry.ProviderResponseError):
        with telemetry.provider_call(telemetry.PROVIDER_NBA_STATS, "bad_op"):
            raise telemetry.ProviderResponseError("malformed")

    events = {event["operation"]: event for event in telemetry.get_recorded_provider_events()}
    assert events["timeout_op"]["outcome"] == telemetry.OUTCOME_TIMEOUT
    assert events["http_op"]["outcome"] == telemetry.OUTCOME_HTTP_ERROR
    assert events["bad_op"]["outcome"] == telemetry.OUTCOME_MALFORMED


def test_deadline_timeout_is_distinct_from_unrelated_builtin_timeout():
    with pytest.raises(DeadlineExceededError):
        with telemetry.provider_call(telemetry.PROVIDER_UNDERDOG, "get_snapshot"):
            raise DeadlineExceededError("retrieval deadline exceeded")
    with pytest.raises(TimeoutError):
        with telemetry.provider_call(telemetry.PROVIDER_UNDERDOG, "get_snapshot"):
            raise TimeoutError("unrelated implementation timeout")

    events = telemetry.get_recorded_provider_events()
    assert events[0]["outcome"] == telemetry.OUTCOME_TIMEOUT
    assert events[1]["outcome"] == telemetry.OUTCOME_ERROR


def test_retry_count_is_read_from_the_thread_counter():
    with telemetry.provider_call(telemetry.PROVIDER_NBA_STATS, "nba_retry"):
        telemetry.increment_retry_count()
        telemetry.increment_retry_count()

    event = telemetry.get_recorded_provider_events()[0]
    assert event["retry_count"] == 2


def test_deterministic_clock_injection():
    fixed_monotonic = iter([float(i) for i in range(10)])
    telemetry.set_clock(
        monotonic=lambda: next(fixed_monotonic),
        now_iso=lambda: "2025-03-04T05:00:00+00:00",
    )
    try:
        with telemetry.provider_call(
            telemetry.PROVIDER_PBP_STATS, "get_totals"
        ) as tracker:
            tracker.status_code = 200

        event = telemetry.get_recorded_provider_events()[0]
        assert event["started_at"] == "2025-03-04T05:00:00+00:00"
        assert event["duration_ms"] == 1000.0
    finally:
        import time as _time

        telemetry.set_clock(
            monotonic=_time.monotonic,
            now_iso=lambda: datetime.now(timezone.utc).isoformat(),
        )


def test_buffer_is_bounded_and_thread_safe():
    for index in range(telemetry.EVENT_BUFFER_CAPACITY + 500):
        telemetry.record_provider_event(
            telemetry.ProviderEvent(
                provider=telemetry.PROVIDER_NBA_STATS,
                operation=f"op_{index}",
                outcome=telemetry.OUTCOME_SUCCESS,
                started_at="2025-01-01T00:00:00+00:00",
                duration_ms=0.0,
                retry_count=0,
                cache_status=telemetry.CACHE_MISS,
                request_id="-",
            )
        )

    metrics = telemetry.snapshot_metrics()
    assert metrics["buffered_events"] <= telemetry.EVENT_BUFFER_CAPACITY
    assert metrics["buffered_events"] == telemetry.EVENT_BUFFER_CAPACITY
    assert metrics["provider_events_total"] == (
        telemetry.EVENT_BUFFER_CAPACITY + 500
    )
    assert len(telemetry.get_recorded_provider_events()) == telemetry.EVENT_BUFFER_CAPACITY
    assert len(telemetry.snapshot_recent_events(limit=10)) == 10


def test_provider_failure_counters_are_segregated_from_application():
    with pytest.raises(requests.exceptions.ReadTimeout):
        with telemetry.provider_call(telemetry.PROVIDER_NBA_STATS, "timeout_op"):
            raise requests.exceptions.ReadTimeout("nope")

    telemetry.record_application_failure("internal_error")
    telemetry.record_application_failure("internal_error")
    telemetry.record_application_failure("operation_failed")

    metrics = telemetry.snapshot_metrics()
    assert metrics["provider_failures"][telemetry.PROVIDER_NBA_STATS] == {
        telemetry.OUTCOME_TIMEOUT: 1,
    }
    assert metrics["application_failures"] == {
        "internal_error": 2,
        "operation_failed": 1,
    }


def test_cache_decisions_are_counted_apart_from_provider_operations():
    """A real upstream call is a provider event, not a cache decision."""

    telemetry.record_cached_provider_event(telemetry.PROVIDER_NBA_STATS, "player_game_logs")
    with pytest.raises(requests.exceptions.ReadTimeout):
        with telemetry.provider_call(
            telemetry.PROVIDER_NBA_STATS,
            "player_game_logs",
            cache_status=telemetry.CACHE_MISS,
        ):
            raise requests.exceptions.ReadTimeout("miss")

    metrics = telemetry.snapshot_metrics()
    assert metrics["cache"][telemetry.PROVIDER_NBA_STATS] == {"hit": 1}
    assert metrics["provider_events_total"] == 2
    assert [event["cache_status"] for event in telemetry.get_recorded_provider_events()] == [
        telemetry.CACHE_HIT,
        telemetry.CACHE_MISS,
    ]


def test_provider_events_never_write_secret_text(caplog):
    caplog.set_level(logging.INFO)
    telemetry.record_cached_provider_event(
        telemetry.PROVIDER_NBA_STATS, "player_game_logs"
    )

    log_text = caplog.text
    assert "Authorization" not in log_text
    assert "api-key" not in log_text
    assert "Bearer " not in log_text
    assert "https://" not in log_text
    assert "raw body" not in log_text


def test_cache_hit_ignores_retries_accrued_by_an_earlier_call():
    telemetry.clear_recorded_provider_events()

    with pytest.raises(requests.exceptions.ReadTimeout):
        with telemetry.provider_call(
            telemetry.PROVIDER_PBP_STATS, "timed_out_before_hit"
        ):
            telemetry.increment_retry_count()
            telemetry.increment_retry_count()
            raise requests.exceptions.ReadTimeout("nope")

    telemetry.record_cached_provider_event(
        telemetry.PROVIDER_NBA_STATS, "player_game_logs"
    )

    events = telemetry.get_recorded_provider_events()
    hit_event = [e for e in events if e["outcome"] == telemetry.OUTCOME_SUCCESS][-1]
    assert hit_event["provider"] == telemetry.PROVIDER_NBA_STATS
    assert hit_event["cache_status"] == telemetry.CACHE_HIT
    assert hit_event["retry_count"] == 0


@pytest.fixture
def error_app() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    from app.errors import (
        ProviderUnavailableError,
        register_error_handlers,
    )

    register_error_handlers(app)

    @app.get("/provider-outage")
    def provider_outage() -> None:
        raise ProviderUnavailableError(detail="provider token=secret-token")

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("internal app secret")

    return app


def test_provider_unavailable_is_not_counted_as_application_failure(
    error_app,
):
    client = error_app.test_client()
    client.get("/provider-outage")

    metrics = telemetry.snapshot_metrics()
    assert "provider_unavailable" not in metrics["application_failures"]


def test_app_error_is_not_double_counted_when_provider_fails_in_seam(error_app):
    client = error_app.test_client()

    # Provider seam records the provider failure (simulated here, outside a
    # request) and the route layers it into ProviderUnavailableError.  The
    # central handler must not record the same provider error as an
    # application failure again.
    with pytest.raises(requests.exceptions.ReadTimeout):
        with telemetry.provider_call(telemetry.PROVIDER_NBA_STATS, "player_game_logs"):
            raise requests.exceptions.ReadTimeout("upstream")

    response = client.get("/boom")
    assert response.status_code == 500

    metrics = telemetry.snapshot_metrics()
    assert metrics["provider_failures"][telemetry.PROVIDER_NBA_STATS][
        telemetry.OUTCOME_TIMEOUT
    ] == 1
    assert metrics["application_failures"] == {"internal_error": 1}


def test_central_handler_counts_only_application_failures(error_app):
    client = error_app.test_client()
    client.get("/boom")
    client.get("/boom")
    client.get("/provider-outage")

    metrics = telemetry.snapshot_metrics()
    assert metrics["application_failures"] == {"internal_error": 2}


def test_recent_events_include_cache_hit_and_failure_outcomes():
    telemetry.record_cached_provider_event(telemetry.PROVIDER_NBA_STATS, "player_game_logs")
    with pytest.raises(telemetry.ProviderResponseError):
        with telemetry.provider_call(telemetry.PROVIDER_PBP_STATS, "get_totals"):
            raise telemetry.ProviderResponseError("bad shape")

    recent = telemetry.snapshot_recent_events(limit=5)
    assert len(recent) == 2
    assert {e["outcome"] for e in recent} == {
        telemetry.OUTCOME_SUCCESS,
        telemetry.OUTCOME_MALFORMED,
    }


def _board_request_event(**overrides):
    values = {
        "duration_ms": 12.5,
        "outcome": "served",
        "status_code": 200,
        "comparison_availability": "available",
        "provider_status_counts": (("complete", 2),),
        "failure_reason_counts": (),
        "freshness_counts": (("fresh", 2),),
        "cache_counts": (("hit", 1), ("miss", 1)),
        "group_count": 1,
        "market_count": 2,
        "unresolved_count": 0,
        "disabled_provider_count": 1,
        "started_at": "2026-08-09T20:00:30+00:00",
        "request_id": "board-1",
    }
    values.update(overrides)
    return telemetry.BoardRequestEvent(**values)


def test_board_request_events_are_recorded_and_counted():
    telemetry.record_board_request_event(_board_request_event())

    recorded = telemetry.get_recorded_board_request_events()
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == "served"
    assert recorded[0]["cache_counts"] == {"hit": 1, "miss": 1}
    assert telemetry.snapshot_metrics()["board_request_events_total"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": "truncated"},
        {"comparison_availability": "nikola-jokic"},
        {"provider_status_counts": (("player-203999", 1),)},
        {"failure_reason_counts": (("connection refused by host", 1),)},
        {"freshness_counts": (("0022500001", 1),)},
        {"cache_counts": (("mkt_2_abc", 1),)},
        {"duration_ms": -1.0},
        {"started_at": "2026-08-09T20:00:30"},
        {"request_id": "not a valid id"},
    ],
)
def test_a_board_request_event_refuses_unbounded_labels(overrides):
    with pytest.raises(ValueError):
        _board_request_event(**overrides)


@pytest.mark.parametrize(
    "duration",
    [float("nan"), float("inf"), float("-inf"), -0.001, True, False, "12.5", None],
)
def test_a_board_request_duration_must_be_a_finite_non_negative_number(duration):
    """A duration is measured, so nothing unmeasurable may be recorded as one."""

    with pytest.raises(ValueError):
        _board_request_event(duration_ms=duration)


@pytest.mark.parametrize(
    "field",
    [
        "group_count",
        "market_count",
        "unresolved_count",
        "disabled_provider_count",
        "status_code",
    ],
)
def test_a_board_request_count_is_never_a_boolean(field):
    with pytest.raises(ValueError):
        _board_request_event(**{field: True})


def test_a_board_request_label_count_is_never_a_boolean():
    with pytest.raises(ValueError):
        _board_request_event(provider_status_counts=(("complete", True),))


@pytest.mark.parametrize(
    ("outcome", "status_code"),
    [
        ("served", 200),
        ("not_modified", 304),
        ("invalid", 400),
        ("too_large", 400),
        ("disabled", 404),
        ("error", 500),
        ("unavailable", 503),
    ],
)
def test_every_board_request_outcome_states_its_one_status(outcome, status_code):
    event = _board_request_event(outcome=outcome, status_code=status_code)

    assert event.outcome == outcome
    assert event.status_code == status_code
    assert telemetry.BOARD_REQUEST_STATUSES[outcome] == status_code
    assert telemetry.BOARD_REQUEST_OUTCOMES == set(telemetry.BOARD_REQUEST_STATUSES)


@pytest.mark.parametrize(
    ("outcome", "status_code"),
    [("served", 404), ("disabled", 200), ("unavailable", 500), ("invalid", 404)],
)
def test_a_board_request_outcome_cannot_claim_another_status(outcome, status_code):
    with pytest.raises(ValueError):
        _board_request_event(outcome=outcome, status_code=status_code)


def test_player_pool_recorder_counts_and_bounds_scalar_drop_events():
    recorder = telemetry.BoundedPlayerPoolTelemetryRecorder()
    recorder.record(telemetry.PlayerPoolTelemetryEvent(2, 1, 3, 4))

    metrics = telemetry.snapshot_metrics()

    assert metrics["player_pool_events_total"] == 1
    assert metrics["player_pool_buffered_events"] == 1
    assert telemetry.snapshot_recent_player_pool_events() == [
        {
            "unknown_stat_label_count": 2,
            "unjoined_athlete_count": 1,
            "unjoined_event_count": 3,
            "team_mismatch_count": 4,
        }
    ]


def test_every_board_request_outcome_serializes_through_the_buffer():
    for outcome, status_code in telemetry.BOARD_REQUEST_STATUSES.items():
        telemetry.record_board_request_event(
            _board_request_event(outcome=outcome, status_code=status_code)
        )

    recorded = telemetry.get_recorded_board_request_events()
    assert [event["outcome"] for event in recorded] == list(
        telemetry.BOARD_REQUEST_STATUSES
    )
    assert [event["status_code"] for event in recorded] == list(
        telemetry.BOARD_REQUEST_STATUSES.values()
    )
    assert telemetry.snapshot_metrics()["board_request_events_total"] == len(recorded)


def test_a_board_request_log_line_states_no_identity(caplog):
    caplog.set_level(logging.INFO)

    telemetry.record_board_request_event(_board_request_event())

    assert "board_request" in caplog.text
    assert "Authorization" not in caplog.text
    assert "https://" not in caplog.text
    assert "Bearer " not in caplog.text
