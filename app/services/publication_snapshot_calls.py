"""Pass a request's read scope only to seams that accept it."""

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


def call_with_read_scope(
    method, *args, publication_snapshot=None, connection=None, **kwargs
):
    """Keep injected legacy test seams compatible with the read-scope kwargs."""

    for name, value in (
        ("publication_snapshot", publication_snapshot),
        ("connection", connection),
    ):
        if value is not None and accepts_keyword(method, name):
            kwargs[name] = value
    return method(*args, **kwargs)


__all__ = ["accepts_keyword", "call_with_read_scope"]
