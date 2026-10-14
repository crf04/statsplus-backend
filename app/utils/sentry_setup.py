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
from collections.abc import Mapping
from typing import Any

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-forwarded-for",
        "x-real-ip",
    }
)
_REDACTED = "[Filtered]"
_REQUEST_FIELDS_DROPPED = ("data", "cookies", "query_string", "env")


def _current_request_id() -> str | None:
    from flask import g, has_request_context

    if not has_request_context():
        return None
    return getattr(g, "request_id", None) or None


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """Tag the event with the request reference and strip request content."""

    request = event.get("request")
    if isinstance(request, dict):
        for field in _REQUEST_FIELDS_DROPPED:
            request.pop(field, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in _SENSITIVE_HEADERS:
                    headers[name] = _REDACTED
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
        integrations=[FlaskIntegration()],
        before_send=scrub_event,
        **options,
    )
    return True
