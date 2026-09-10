"""Request correlation header contract for #12.

Every app request accepts a safe inbound ``X-Request-ID`` (or generates a
fresh one), sets it on ``flask.g`` so provider events within the request carry
it, and echoes it back on the ``X-Request-ID`` response header.
"""

from __future__ import annotations

import re

import pytest
from flask import jsonify

import app.utils.telemetry as telemetry

_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def test_accepts_and_echoes_valid_inbound_request_id(client):
    response = client.get("/api/health/db", headers={"X-Request-ID": "req-abc-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-abc-123"


def test_generates_id_when_inbound_is_missing(client):
    first = client.get("/api/health/db")
    second = client.get("/api/health/db")

    assert first.headers["X-Request-ID"]
    assert _UUID_HEX.match(first.headers["X-Request-ID"])
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


def test_rejects_untrusted_inbound_id_and_generates_one(client):
    from app.utils.request_id import resolve_request_id

    unsafe = "bad value with spaces"
    response = client.get("/api/health/db", headers={"X-Request-ID": unsafe})

    assert _UUID_HEX.match(response.headers["X-Request-ID"])
    assert response.headers["X-Request-ID"] != unsafe

    injected = '">\nX-Evil: 1'
    for candidate in (unsafe, injected, '">X-Evil:1', "' OR 1=1 --"):
        assert resolve_request_id(candidate) != candidate
        assert _UUID_HEX.match(resolve_request_id(candidate))


def test_request_id_is_present_even_on_error_responses(client):
    response = client.get("/api/definitely-missing-route")

    assert response.status_code == 404
    assert "X-Request-ID" in response.headers


def test_provider_events_correlate_to_the_request_id(app):
    from app.utils.request_id import HEADER_NAME

    def probe():
        with telemetry.provider_call(
            telemetry.PROVIDER_NBA_STATS, "player_game_logs"
        ):
            pass
        return jsonify({"ok": True})

    app.add_url_rule(
        "/correlation-probe",
        "correlation_probe",
        probe,
        methods=["GET"],
    )

    client = app.test_client()
    response = client.get(
        "/correlation-probe", headers={HEADER_NAME: "correlate-me-42"}
    )

    assert response.status_code == 200
    assert response.headers[HEADER_NAME] == "correlate-me-42"
    event = telemetry.get_recorded_provider_events()[-1]
    assert event["request_id"] == "correlate-me-42"


def test_request_id_bound_to_g_flows_into_provider_events(app):
    from app.utils.request_id import generate_request_id

    def probe():
        with telemetry.provider_call(
            telemetry.PROVIDER_PBP_STATS, "get_totals"
        ):
            pass
        return jsonify({"ok": True})

    app.add_url_rule(
        "/correlation-probe-2",
        "correlation_probe_2",
        probe,
        methods=["GET"],
    )

    client = app.test_client()
    generated = generate_request_id()
    client.get("/correlation-probe-2", headers={"X-Request-ID": generated})

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["request_id"] == generated
    assert _UUID_HEX.match(event["request_id"])

def test_request_log_line_reports_duration_request_id_and_cache_state(client, caplog):
    """The lifecycle log is the source for p95 triggers, not the telemetry
    deques: it reports method, URL rule, status, duration, request id, and
    the Target backtest cache decision (a dash until that seam stamps g)."""

    import logging

    with caplog.at_level(logging.INFO):
        response = client.get("/api/health/db", headers={"X-Request-ID": "req-log-1"})

    assert response.status_code == 200
    lines = [
        record.getMessage()
        for record in caplog.records
        if "duration_ms=" in record.getMessage()
    ]
    assert len(lines) == 1
    line = lines[0]
    assert "method=GET" in line
    assert "rule=/api/health/db" in line
    assert "status=200" in line
    assert "duration_ms=" in line
    assert "request_id=req-log-1" in line
    assert "targets_cache=-" in line


def test_request_log_line_covers_a_missing_route(client, caplog):
    """Errors are routes too: the log line carries the 404 status."""

    import logging

    with caplog.at_level(logging.INFO):
        response = client.get("/api/definitely-missing-route")

    assert response.status_code == 404
    lines = [
        record.getMessage()
        for record in caplog.records
        if "duration_ms=" in record.getMessage()
    ]
    assert len(lines) == 1
    assert "status=404" in lines[0]
