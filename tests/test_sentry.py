"""Sentry reporting: inert without a DSN, tagged and scrubbed with one."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from app.utils.sentry_setup import (
    init_sentry,
    make_traces_sampler,
    parse_traces_sample_rate,
    scrub_event,
)

DSN = "https://publickey@o0.ingest.sentry.io/0"
SHA = "0123456789abcdef0123456789abcdef01234567"


class CapturingTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def transport():
    yield CapturingTransport()
    sentry_sdk.get_global_scope().set_client(None)


def _environ(**extra):
    return {
        "SENTRY_DSN": DSN,
        "RAILWAY_GIT_COMMIT_SHA": SHA,
        "FLASK_ENV": "testing",
        **extra,
    }


@pytest.fixture
def failing_app(app, transport):
    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.post("/boom-post")
    def boom_post():
        raise RuntimeError("kaboom")

    init_sentry(_environ(), transport=transport)
    return app


def test_without_a_dsn_nothing_is_initialized_or_imported():
    code = (
        "import sys;"
        "from app.utils.sentry_setup import init_sentry;"
        "print(init_sentry({}), 'sentry_sdk' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "SENTRY_DSN"},
        check=True,
    )

    assert result.stdout.split() == ["False", "False"]


def test_blank_dsn_is_treated_as_unset():
    assert init_sentry({"SENTRY_DSN": "  "}) is False


def test_forced_500_sends_one_event_tagged_with_the_response_request_id(failing_app, transport):
    response = failing_app.test_client().get("/boom")

    assert response.status_code == 500
    sentry_sdk.flush()
    assert len(transport.events) == 1
    event = transport.events[0]
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert event["tags"]["request_id"] == request_id
    assert event["environment"] == "testing"
    assert event["release"] == SHA
    assert event["level"] == "error"
    values = event["exception"]["values"]
    assert values[-1]["type"] == "RuntimeError"
    assert values[-1]["value"] == "kaboom"
    assert "boom" in [f["function"] for f in values[-1]["stacktrace"]["frames"]]


def test_exception_message_secrets_are_redacted(app, transport):
    @app.get("/leak")
    def leak():
        raise RuntimeError("provider said token=" + "exc" + "-secret")

    init_sentry(_environ(), transport=transport)

    app.test_client().get("/leak")

    sentry_sdk.flush()
    assert len(transport.events) == 1
    assert "exc-secret" not in json.dumps(transport.events[0])


def test_raised_app_error_5xx_is_one_exception_event(app, transport):
    from app.errors import OperationFailedError

    @app.get("/op-failed")
    def op_failed():
        raise OperationFailedError(detail="db down")

    init_sentry(_environ(), transport=transport)

    response = app.test_client().get("/op-failed")

    sentry_sdk.flush()
    assert response.status_code >= 500
    assert len(transport.events) == 1
    assert transport.events[0]["exception"]["values"][-1]["type"] == "OperationFailedError"
    assert transport.events[0]["tags"]["request_id"] == response.headers["X-Request-ID"]


def test_event_carries_a_caller_supplied_request_id(failing_app, transport):
    response = failing_app.test_client().get("/boom", headers={"X-Request-ID": "abcd1234-trace.7"})

    sentry_sdk.flush()
    assert response.headers["X-Request-ID"] == "abcd1234-trace.7"
    assert [e["tags"]["request_id"] for e in transport.events] == ["abcd1234-trace.7"]


def test_request_content_never_reaches_the_event(failing_app, transport):
    failing_app.test_client().post(
        "/boom-post?token=query-secret",
        json={"password": "body-secret"},
        headers={
            "Authorization": "Bearer bearer-secret",
            "Cookie": "session=cookie-secret",
            "X-Api-Key": "apikey-secret",
            "Referer": "https://app.example/?email=referer-secret",
            "Forwarded": "for=203.0.113.77",
        },
    )

    sentry_sdk.flush()
    assert len(transport.events) == 1
    serialized = json.dumps(transport.events[0])
    for secret in (
        "body-secret",
        "bearer-secret",
        "cookie-secret",
        "apikey-secret",
        "query-secret",
        "referer-secret",
        "203.0.113.77",
    ):
        assert secret not in serialized


def test_scrub_event_filters_credentials_and_request_content():
    event = {
        "request": {
            "url": "http://x/api",
            "data": {"a": "b"},
            "cookies": {"s": "c"},
            "query_string": "token=t",
            "headers": {
                "Authorization": "Bearer t",
                "Cookie": "s=c",
                "Referer": "https://x/?email=a@b.c",
                "Forwarded": "for=203.0.113.9",
                "Accept": "application/json",
            },
        },
        "user": {"email": "a@b.c"},
        "breadcrumbs": {"values": [{"message": "x"}]},
    }

    assert scrub_event(event) == {
        "request": {
            "url": "http://x/api",
            "headers": {"Accept": "application/json"},
        },
    }


def test_request_bodies_are_never_collected(transport):
    init_sentry(_environ(), transport=transport)

    assert sentry_sdk.get_client().options["max_request_body_size"] == "never"


def test_library_integrations_do_not_hook_the_database_engine(transport):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    init_sentry(_environ(), transport=transport)

    assert not event.contains(Engine, "before_cursor_execute", _sqlalchemy_hook())


def _sqlalchemy_hook():
    from sentry_sdk.integrations.sqlalchemy import _before_cursor_execute

    return _before_cursor_execute


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0.05),
        ("", 0.05),
        ("0.2", 0.2),
        (" 1 ", 1.0),
        ("0", 0.0),
        ("abc", 0.05),
        ("-0.1", 0.05),
        ("1.5", 0.05),
        ("nan", 0.05),
    ],
)
def test_traces_sample_rate_parsing(raw, expected):
    assert parse_traces_sample_rate(raw) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/api/collector/token",
        "/api/collector/bootstrap/abc/status",
        "/api/health/db",
        "/health",
        "/api/internal/anything",
    ],
)
def test_sampler_skips_ingestion_health_and_internal_routes(path):
    sampler = make_traces_sampler(0.5)

    assert sampler({"wsgi_environ": {"PATH_INFO": path}}) == 0.0


@pytest.mark.parametrize("path", ["/api/players/search", "/api/matchups/today", "/"])
def test_sampler_samples_user_routes_at_the_configured_rate(path):
    sampler = make_traces_sampler(0.5)

    assert sampler({"wsgi_environ": {"PATH_INFO": path}}) == 0.5
    assert sampler({}) == 0.5


def test_tracing_defaults_to_five_percent_and_skips_collector(transport):
    init_sentry(_environ(), transport=transport)
    options = sentry_sdk.get_client().options

    assert options["traces_sampler"]({"wsgi_environ": {"PATH_INFO": "/api/x"}}) == 0.05
    assert options["traces_sampler"]({"wsgi_environ": {"PATH_INFO": "/api/collector/token"}}) == 0.0
    assert options["send_default_pii"] is False


def test_tracing_rate_comes_from_the_environment(transport):
    init_sentry(_environ(SENTRY_TRACES_SAMPLE_RATE="0.25"), transport=transport)
    sampler = sentry_sdk.get_client().options["traces_sampler"]

    assert sampler({"wsgi_environ": {"PATH_INFO": "/api/x"}}) == 0.25


def test_environment_prefers_the_explicit_sentry_setting(transport):
    init_sentry(
        _environ(SENTRY_ENVIRONMENT="beta", RAILWAY_ENVIRONMENT_NAME="production"),
        transport=transport,
    )

    assert sentry_sdk.get_client().options["environment"] == "beta"


def test_a_plain_error_log_becomes_one_tagged_log_event(app, transport):
    import logging

    @app.get("/logs-error")
    def logs_error():
        logging.getLogger("app.test").error("backfill failed rows=3")
        return {"ok": True}

    init_sentry(_environ(), transport=transport)

    response = app.test_client().get("/logs-error")

    sentry_sdk.flush()
    assert len(transport.events) == 1
    assert transport.events[0]["logentry"]["formatted"] == "backfill failed rows=3"
    assert transport.events[0]["tags"]["request_id"] == response.headers["X-Request-ID"]


def test_log_events_redact_emails_and_sql_parameters(app, transport):
    import logging

    @app.get("/db-error")
    def db_error():
        logging.getLogger("app.test").error(
            "Database error for ada@example.invalid\n"
            "[SQL: INSERT INTO users (email) VALUES (?)]\n"
            "[parameters: ('ada@example.invalid', 'Ada Lovelace')]\n"
            "DETAIL:  Failing row contains (uid-7, Ada\nLovelace, null).\n"
            "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
        )
        return {"ok": True}

    init_sentry(_environ(), transport=transport)

    app.test_client().get("/db-error")

    sentry_sdk.flush()
    assert len(transport.events) == 1
    entry = transport.events[0]["logentry"]
    assert entry["formatted"] == (
        "Database error for [Filtered]\n"
        "[SQL: INSERT INTO users (email) VALUES (?)]\n"
        "[parameters: [Filtered]]\n"
        "DETAIL: [Filtered]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    assert "Ada" not in json.dumps(transport.events[0]["logentry"])
