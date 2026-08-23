"""Pass a captured publication generation only to seams that accept it."""

from __future__ import annotations

from inspect import Parameter, signature
from typing import Any


def accepts_keyword(method: Any, name: str) -> bool:
    """Whether an injected seam takes a keyword its caller may pass."""

    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind == Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def call_with_snapshot(method, *args, publication_snapshot=None, **kwargs):
    """Keep injected legacy test seams compatible with the snapshot kwarg."""

    if publication_snapshot is None:
        return method(*args, **kwargs)
    if accepts_keyword(method, "publication_snapshot"):
        kwargs["publication_snapshot"] = publication_snapshot
    return method(*args, **kwargs)


__all__ = ["accepts_keyword", "call_with_snapshot"]
