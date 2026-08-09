"""Correlation request IDs for the public HTTP boundary.

Every request accepts a caller-supplied ``X-Request-ID`` when it matches a
safe header pattern, and otherwise receives a fresh generated ID.  The same
value is attached to Flask's ``g`` (so the telemetry middleware can correlate
provider events) and echoed back on the ``X-Request-ID`` response header.
"""

from __future__ import annotations

import re
import uuid

HEADER_NAME = "X-Request-ID"

# Safe, printable header values only, so an inbound value can never smuggle
# headers or control characters into the log or the response.
_VALID_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def generate_request_id() -> str:
    """Return a fresh, safe correlation ID."""
    return uuid.uuid4().hex


def is_valid_request_id(value: object) -> bool:
    """Return whether ``value`` is safe for headers, logs, and telemetry."""

    return isinstance(value, str) and bool(_VALID_REQUEST_ID_PATTERN.fullmatch(value))


def resolve_request_id(incoming: str | None) -> str:
    """Return a valid ``incoming`` ID, or generate a new one."""
    if is_valid_request_id(incoming):
        return incoming
    return generate_request_id()


__all__ = [
    "HEADER_NAME",
    "generate_request_id",
    "is_valid_request_id",
    "resolve_request_id",
]
