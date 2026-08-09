"""Stable primitive normalization shared by DFS wire-format adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.providers.dfs import MarketThreshold, normalize_timestamp


def optional_text(value: Any) -> str | None:
    """Trim optional provider text without manufacturing a value."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def required_identifier(value: Mapping[str, Any], key: str) -> str:
    """Read a required provider identifier as text."""

    identifier = value.get(key)
    if identifier is None or isinstance(identifier, bool) or not str(identifier).strip():
        raise ValueError(f"{key} must be present")
    return str(identifier)


def required_text(value: Mapping[str, Any], key: str) -> str:
    """Read required, non-empty provider text."""

    text = optional_text(value.get(key))
    if text is None:
        raise ValueError(f"{key} must be a non-empty string")
    return text


def required_number(value: Mapping[str, Any], key: str) -> str | int | float:
    """Read a finite provider number while retaining its source spelling."""

    raw = value.get(key)
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (str, int, float)):
        raise ValueError(f"{key} must be numeric")
    try:
        decimal = MarketThreshold(raw, unit="count").value
    except ValueError as error:
        raise ValueError(f"{key} must be numeric") from error
    if not decimal.is_finite():
        raise ValueError(f"{key} must be finite")
    return raw


def display_number(value: Any, *, field: str) -> str:
    """Return a numeric source value in its original display form."""

    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} must have a displayable numeric value")
    return str(value)


def validate_timestamp(value: Any, field: str) -> None:
    """Validate an optional ISO-8601 timestamp without changing its evidence."""

    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        normalize_timestamp(value)
    except ValueError as error:
        raise ValueError(str(error)) from error


def is_ineligible_event_status(label: str) -> bool:
    """Recognize event states that are outside the pregame snapshot."""

    normalized = label.strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized in {"live", "closed", "settled", "final", "in_play", "inplay"}


__all__ = [
    "display_number",
    "is_ineligible_event_status",
    "optional_text",
    "required_identifier",
    "required_number",
    "required_text",
    "validate_timestamp",
]
