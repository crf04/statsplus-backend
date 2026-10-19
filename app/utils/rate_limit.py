"""Per-user and per-IP request rate limits, shared across workers by Redis.

The contract is crf04/statsplus#157: a signed-in caller is limited by Firebase
uid, an unauthenticated one by client IP, and a breach answers ``429`` with the
standard error shape and a ``Retry-After`` header.  Counting is Flask-Limiter
over the same Redis the caches use; when that store is unreachable the request
is allowed and a warning is logged.  Limiter keys carry TTLs, so an
``allkeys-lru`` Redis never has to evict them.

Flask-Limiter checks limits in a ``before_request`` hook, which runs before a
route's own ``@require_auth``.  The uid is only known once the token is
verified, so :func:`init_rate_limiting` registers a hook ahead of the
limiter's that authenticates routes marked ``requires_auth`` (or
``optional_auth``) first.  Each app owns its limiter and store, so building a
second app never resets the first one's counts.
"""

from __future__ import annotations

import logging
import math
import time

from flask import Flask, current_app, jsonify, request
from flask_limiter import Limit, Limiter, RateLimitExceeded
from limits.storage import Storage, storage_from_string

from app.config.settings import RuntimeSettings
from app.utils.auth import (
    _authenticate_optionally,
    _authenticate_request,
    get_current_user,
)

logger = logging.getLogger(__name__)

RATE_LIMITED_MESSAGE = "Too many requests. Try again in a few seconds."

AUTHENTICATED_DEFAULT_LIMIT = "300/minute"
UNAUTHENTICATED_LIMIT = "60/minute"
TARGET_PREVIEW_LIMIT = "30/minute"
TARGET_BACKTEST_LIMIT = "60/minute"
NL_QUERY_LIMIT = "30/minute"
DFS_BOARD_LIMIT = "30/minute"
FEEDBACK_LIMIT = "10/hour"

#: Redis commands must not hold a request slot when the store is wedged.
_STORAGE_TIMEOUT_SECONDS = 1


def client_ip() -> str:
    """The caller's IP: the first ``X-Forwarded-For`` hop behind Railway's proxy."""

    forwarded = request.headers.get("X-Forwarded-For", "")
    first_hop = forwarded.split(",")[0].strip()
    return first_hop or request.remote_addr or "unknown"


def user_key() -> str:
    """The signed-in caller's key; callers only use it on authenticated routes."""

    user = get_current_user()
    return f"user:{user['uid']}" if user else f"ip:{client_ip()}"


def _default_limit() -> str:
    return AUTHENTICATED_DEFAULT_LIMIT if get_current_user() else UNAUTHENTICATED_LIMIT


class FailOpenLimiter(Limiter):
    """A limiter that allows the request, with a warning, when its store fails."""

    def _check_request_limit(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        try:
            super()._check_request_limit(*args, **kwargs)
        except RateLimitExceeded:
            raise
        except Exception as error:
            logger.warning("Rate limit store unavailable; allowing the request: %s", error)


def _is_exempt() -> bool:
    """CORS preflights, ``/health``, and machine-credentialed collector routes."""

    if request.method == "OPTIONS" or request.path == "/health":
        return True
    # The collector authenticates with its own signed credential, not a
    # Firebase token, and polls and ingests faster than a per-IP human limit.
    return request.blueprint == "collection" and get_current_user() is None


def storage_uri(settings: RuntimeSettings) -> str:
    """The Redis the limiter counts in, or per-process memory with no Redis."""

    cache = settings.cache
    if not cache.enabled:
        return "memory://"
    if cache.url:
        return cache.url
    if cache.host != "localhost" or cache.password:
        scheme = "rediss" if cache.tls else "redis"
        password = f":{cache.password}@" if cache.password else ""
        return f"{scheme}://{password}{cache.host}:{cache.port}/{cache.database}"
    return "memory://"


def build_storage(settings: RuntimeSettings) -> Storage:
    """A counting store for the limiter's own keys, with bounded socket waits."""

    uri = storage_uri(settings)
    if uri.startswith("memory://"):
        return storage_from_string(uri)
    return storage_from_string(
        uri,
        socket_timeout=_STORAGE_TIMEOUT_SECONDS,
        socket_connect_timeout=_STORAGE_TIMEOUT_SECONDS,
    )


#: Endpoints with a limit of their own, which replaces the shared default.
ROUTE_LIMITS = {
    "users.preview_target": TARGET_PREVIEW_LIMIT,
    "users.backtest_target": TARGET_BACKTEST_LIMIT,
    "nl.process_natural_language_query": NL_QUERY_LIMIT,
    "dfs.dfs_board": DFS_BOARD_LIMIT,
    "users.submit_feedback": FEEDBACK_LIMIT,
}


def _authenticate_before_limiting() -> None:
    if request.method == "OPTIONS" or get_current_user() is not None:
        return
    view = current_app.view_functions.get(request.endpoint or "")
    if getattr(view, "requires_auth", False):
        _authenticate_request(allow_waitlisted=getattr(view, "allows_waitlisted", False))
    elif getattr(view, "optional_auth", False):
        _authenticate_optionally()


def init_rate_limiting(app: Flask, settings: RuntimeSettings) -> None:
    """Enforce the contract's limits on ``app``, which owns its limiter and store.

    Call after the blueprints are registered: the per-route limits wrap the
    registered view functions.
    """

    uri = storage_uri(settings)
    app.config["RATELIMIT_STORAGE_URI"] = uri
    app.config["RATELIMIT_STORAGE_OPTIONS"] = (
        {}
        if uri.startswith("memory://")
        else {
            "socket_timeout": _STORAGE_TIMEOUT_SECONDS,
            "socket_connect_timeout": _STORAGE_TIMEOUT_SECONDS,
        }
    )
    limiter = FailOpenLimiter(
        key_func=user_key,
        # One bucket per caller across every route without its own limit.
        default_limits=[Limit(_default_limit, key_function=user_key, scope="default", shared=True)],
        strategy="fixed-window",
    )
    limiter.request_filter(_is_exempt)

    def rate_limited(_: RateLimitExceeded):  # type: ignore[no-untyped-def]
        response = jsonify({"error": {"code": "rate_limited", "message": RATE_LIMITED_MESSAGE}})
        response.status_code = 429
        response.headers["Retry-After"] = str(_retry_after_seconds(limiter))
        return response

    # Registered first, so the uid exists when the limiter's hook builds keys.
    app.before_request(_authenticate_before_limiting)
    limiter.init_app(app)
    for endpoint, limit in ROUTE_LIMITS.items():
        app.view_functions[endpoint] = limiter.limit(limit, key_func=user_key)(app.view_functions[endpoint])
    app.register_error_handler(RateLimitExceeded, rate_limited)
    app.extensions["rate_limiter"] = limiter


def _retry_after_seconds(limiter: Limiter) -> int:
    """Seconds until the breached window reopens, never less than one."""

    try:
        current = limiter.current_limit
        window = current.limit.get_expiry()
        return max(1, min(window, math.ceil(current.reset_at - time.time())))
    except Exception:
        return 60
