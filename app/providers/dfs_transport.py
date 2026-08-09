"""Shared transport seam for deadline-bounded DFS snapshot requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, TypeVar

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    DeadlineExceededError,
    MalformedProviderResponseError,
    RetrievalContext,
)
from app.utils.telemetry import (
    CACHE_DISABLED,
    ProviderResponseError,
    provider_call,
)

_Result = TypeVar("_Result")
_FailureFactory = Callable[[str, Exception], Exception]


def bounded_timeout(
    context: RetrievalContext,
    timeout: tuple[float, float],
    *,
    now: datetime,
    provider: str,
) -> tuple[float, float]:
    """Cap connect/read timeouts by the remaining absolute retrieval budget."""

    context.ensure_active(now=now)
    remaining = context.remaining_seconds(now=now)
    if remaining <= 0:
        raise DeadlineExceededError(f"{provider} retrieval deadline exceeded")
    connect, read = timeout
    return min(float(connect), remaining), min(float(read), remaining)


def request_json(
    *,
    context: RetrievalContext,
    session: requests.Session | Any,
    url: str,
    params: Mapping[str, Any] | None,
    timeout: tuple[float, float],
    now: Callable[[], datetime],
    provider: str,
    operation: str,
    parse: Callable[[Any], _Result],
    deadline_message: str,
    timeout_message: str,
    unavailable_message: str,
    invalid_json_message: str,
    failure_factory: _FailureFactory | None = None,
) -> _Result:
    """Execute one instrumented JSON request under an absolute deadline."""

    try:
        current = now()
        bounded = bounded_timeout(
            context,
            timeout,
            now=current,
            provider=provider,
        )
        with provider_call(
            provider,
            operation,
            cache_status=CACHE_DISABLED,
            request_id=context.request_id,
        ) as tracker:
            if params is None:
                response = session.get(url, timeout=bounded)
            else:
                response = session.get(url, params=params, timeout=bounded)
            tracker.status_code = getattr(response, "status_code", None)
            context.ensure_active(now=now())
            response.raise_for_status()
            try:
                payload = response.json()
            except (TypeError, ValueError) as error:
                raise ProviderResponseError(invalid_json_message) from error
            try:
                result = parse(payload)
            except MalformedProviderResponseError as error:
                raise ProviderResponseError(str(error)) from error
            context.ensure_active(now=now())
            return result
    except DeadlineExceededError as error:
        if failure_factory is not None:
            raise failure_factory("deadline_exceeded", error) from error
        raise ProviderUnavailableError(deadline_message, detail=error) from error
    except requests.exceptions.Timeout as error:
        if failure_factory is not None:
            raise failure_factory("timeout", error) from error
        raise ProviderUnavailableError(timeout_message, detail=error) from error
    except requests.exceptions.HTTPError as error:
        if failure_factory is not None:
            raise failure_factory("http_error", error) from error
        raise ProviderUnavailableError(unavailable_message, detail=error) from error
    except requests.exceptions.RequestException as error:
        if failure_factory is not None:
            raise failure_factory("request_error", error) from error
        raise ProviderUnavailableError(unavailable_message, detail=error) from error


__all__ = ["bounded_timeout", "request_json"]
