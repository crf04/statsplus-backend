"""Small UTC normalization authority for persisted timestamps."""

from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime) -> datetime:
    """Return one datetime in UTC, treating persisted naive values as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_utc_iso(value: str) -> datetime:
    """Parse one ISO timestamp, accepting the standard trailing-Z form."""

    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


__all__ = ["as_utc", "parse_utc_iso"]
