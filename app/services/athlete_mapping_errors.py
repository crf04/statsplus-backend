"""Shared failure type for the provider athlete mapping seam.

The resolver and the repository both read durable state, so the translation
boundary needs one error both can raise without importing each other.  The DFS
Board isolates exactly this type and never catches broad exceptions.
"""

from __future__ import annotations

DEFAULT_MAPPING_FAILURE_SUMMARY = "The athlete mapping operation could not complete."


class AthleteMappingPersistenceError(RuntimeError):
    """A storage read or write failure safe to isolate from a board read."""


__all__ = [
    "AthleteMappingPersistenceError",
    "DEFAULT_MAPPING_FAILURE_SUMMARY",
]
