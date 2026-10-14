"""Optional Sentry error reporting.

Inert unless ``SENTRY_DSN`` is set: without it nothing is imported, patched,
or sent.  Events carry the ``request_id`` tag (the full ``X-Request-ID``, whose
first 8 characters the UI shows as ``(ref ...)``), the environment, and the
release (``RAILWAY_GIT_COMMIT_SHA``).  Request bodies, cookies, credentials
headers, query strings, users and breadcrumbs are never sent, and performance
tracing is off.

Gunicorn preloads the app in a master that must stay single-threaded, and the
SDK's transport runs a background thread, so ``gunicorn.conf.py`` calls
``init_sentry`` from ``post_fork``: every worker owns its own client.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

_SAFE_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "content-type",
        "content-length",
        "host",
        "user-agent",
        "x-request-id",
    }
)
_REDACTED = "[Filtered]"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_DATABASE_DETAIL_RE = re.compile(
    r"^(\s*DETAIL:).*?(?=\n\s*(?:\[SQL:|\[parameters:|\(Background|CONTEXT:|HINT:|STATEMENT:)|\Z)",
    re.MULTILINE | re.DOTALL,
)
_SQL_PARAMETERS_RE = re.compile(r"\[parameters: .*?\]\s*(?=\n|$)", re.DOTALL)
_REQUEST_FIELDS_DROPPED = ("data", "cookies", "query_string", "env")


def _current_request_id() -> str | None:
    from flask import g, has_request_context

    if not has_request_context():
        return None
    return getattr(g, "request_id", None) or None


def report_exception(error: BaseException) -> None:
    """Send a handled server failure as one exception event with its traceback.

    Marks the request so ``scrub_event`` drops the ERROR log record the error
    handler writes for the same failure; a no-op without an active client.
    """

    import sentry_sdk
    from flask import g, has_request_context

    if not sentry_sdk.get_client().is_active():
        return
    if has_request_context():
        g.sentry_exception_reported = True
    sentry_sdk.capture_exception(error)


def _log_duplicates_reported_exception(event: dict[str, Any]) -> bool:
    from flask import g, has_request_context

    return (
        event.get("logger") == "app.errors"
        and "exception" not in event
        and has_request_context()
        and bool(getattr(g, "sentry_exception_reported", False))
    )


def _scrub_text(text: Any) -> Any:
    """Redact credentials, e-mail addresses and SQL bind parameters in text."""

    if not isinstance(text, str):
        return text
    from app.errors import _sanitize_diagnostic_detail

    text = _sanitize_diagnostic_detail(text) or ""
    text = _SQL_PARAMETERS_RE.sub("[parameters: [Filtered]]", text)
    text = _DATABASE_DETAIL_RE.sub(r"\1 [Filtered]", text)
    return _EMAIL_RE.sub(_REDACTED, text)


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any] | None:
    """Tag the event with the request reference and strip request content."""

    if _log_duplicates_reported_exception(event):
        return None
    exception = event.get("exception")
    if isinstance(exception, dict):
        for value in exception.get("values") or []:
            if isinstance(value, dict) and value.get("value"):
                value["value"] = _scrub_text(value["value"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if key in logentry:
                logentry[key] = _scrub_text(logentry[key])
        if isinstance(logentry.get("params"), (list, tuple)):
            logentry["params"] = [_scrub_text(p) for p in logentry["params"]]
    if "message" in event:
        event["message"] = _scrub_text(event["message"])

    request = event.get("request")
    if isinstance(request, dict):
        for field in _REQUEST_FIELDS_DROPPED:
            request.pop(field, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() not in _SAFE_HEADERS:
                    del headers[name]
    event.pop("user", None)
    event.pop("breadcrumbs", None)

    request_id = _current_request_id()
    if request_id:
        tags = event.get("tags")
        if not isinstance(tags, dict):
            tags = {}
            event["tags"] = tags
        tags["request_id"] = request_id
    return event


def init_sentry(
    environ: Mapping[str, str] | None = None,
    *,
    transport: Any = None,
) -> bool:
    """Initialize the SDK when ``SENTRY_DSN`` is set; return whether it was."""

    environ = os.environ if environ is None else environ
    dsn = (environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration

    options: dict[str, Any] = {}
    if transport is not None:
        options["transport"] = transport
    sentry_sdk.init(
        dsn=dsn,
        environment=(
            environ.get("SENTRY_ENVIRONMENT")
            or environ.get("RAILWAY_ENVIRONMENT_NAME")
            or environ.get("FLASK_ENV")
            or "development"
        ),
        release=environ.get("RAILWAY_GIT_COMMIT_SHA") or None,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        max_breadcrumbs=0,
        traces_sample_rate=0.0,
        # Auto-enabled integrations (SQLAlchemy, Redis, requests, ...) patch
        # libraries process-wide for spans and breadcrumbs this setup turns off.
        auto_enabling_integrations=False,
        integrations=[FlaskIntegration()],
        before_send=scrub_event,
        **options,
    )
    return True
