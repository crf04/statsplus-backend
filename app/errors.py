"""Application errors and the public HTTP error contract.

Routes and services raise the errors in this module when they can describe a
failure safely.  Flask converts them to one predictable JSON shape at the
application boundary.  The public message is deliberately separate from the
optional detail so provider responses, credentials, and other implementation
details can be logged without being returned to a caller.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import wraps
from typing import Any, ClassVar, ParamSpec, TypeVar

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from app.utils.telemetry import ProviderResponseError, record_application_failure


logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_REDACTED_VALUE = "\x00redacted-value\x00"
_REDACTED_PEM = "\x00redacted-pem\x00"


_PEM_MATERIAL_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.IGNORECASE | re.DOTALL,
)
_DATABASE_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)"
    r"(?P<username>[^/?#\s@]*):(?P<password>[^/?#\s@]+)(?P<delimiter>@)",
    re.IGNORECASE,
)
_SENSITIVE_KEY_NAME_ALTERNATIVES = (
    r"api[-_ ]*key|openai[-_ ]*key|access[-_ ]*token|refresh[-_ ]*token|"
    r"id[-_ ]*token|auth[-_ ]*token|firebase[-_ ]*token|oauth[-_ ]*token|"
    r"token|password|passwd|pwd|db[-_ ]*password|secret|private[-_ ]*key|"
    r"service[-_ ]*account|credential(?:s)?|jwt"
)
_NON_AUTH_SENSITIVE_KEY_NAMES = rf"(?:{_SENSITIVE_KEY_NAME_ALTERNATIVES})"
_SENSITIVE_KEY_NAMES = (
    rf"(?:{_SENSITIVE_KEY_NAME_ALTERNATIVES}|authorization|"
    r"proxy[-_ ]*authorization)"
)
_SENSITIVE_QUOTED_VALUE_RE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9])['\"]?{_SENSITIVE_KEY_NAMES}"
    r"['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>(?!\x00redacted-pem\x00).*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_AUTHORIZATION_BEARER_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9])['\"]?(?:authorization|"
    r"proxy[-_ ]*authorization)['\"]?\s*[:=]\s*)"
    r"(?:(?P<scheme>Bearer|Basic|Token)\s+)?"
    r"(?P<value>[^\s,'\";}\]\)]+)",
    re.IGNORECASE,
)
_SENSITIVE_UNQUOTED_VALUE_RE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9])['\"]?{_NON_AUTH_SENSITIVE_KEY_NAMES}"
    r"['\"]?\s*[:=]\s*)"
    r"(?P<value>[^\s,'\";{}\]\)]+)",
    re.IGNORECASE,
)


def _sanitize_diagnostic_detail(detail: Any) -> str | None:
    """Redact credentials and key material before writing diagnostic detail."""

    if detail is None:
        return None

    sanitized = str(detail)
    sanitized = _PEM_MATERIAL_RE.sub(_REDACTED_PEM, sanitized)
    sanitized = _DATABASE_URL_CREDENTIALS_RE.sub(
        lambda match: (
            f"{match.group('scheme')}{match.group('username')}:{_REDACTED_VALUE}"
            f"{match.group('delimiter')}"
        ),
        sanitized,
    )
    sanitized = _SENSITIVE_QUOTED_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED_VALUE}",
        sanitized,
    )
    sanitized = _AUTHORIZATION_BEARER_VALUE_RE.sub(
        lambda match: (
            f"{match.group('prefix')}"
            f"{match.group('scheme') + ' ' if match.group('scheme') else ''}"
            f"{_REDACTED_VALUE}"
        ),
        sanitized,
    )
    sanitized = _SENSITIVE_UNQUOTED_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED_VALUE}",
        sanitized,
    )
    return sanitized.replace(_REDACTED_VALUE, "[REDACTED]").replace(
        _REDACTED_PEM,
        "[REDACTED PEM]",
    )


def _log_application_error(
    error: AppError,
    *,
    http_status: int | None = None,
) -> None:
    """Emit one sanitized diagnostic event for an application error."""

    detail = _sanitize_diagnostic_detail(error.detail)
    if http_status is not None:
        if detail:
            logger.error(
                "HTTP error status=%s code=%s detail=%s",
                http_status,
                error.code,
                detail,
            )
        else:
            logger.error(
                "HTTP error status=%s code=%s",
                http_status,
                error.code,
            )
        return

    if detail:
        logger.error(
            "Application error code=%s detail=%s",
            error.code,
            detail,
        )
    else:
        logger.error("Application error code=%s", error.code)


class AppError(Exception):
    """Base class for expected application failures.

    ``message`` is the safe, client-facing message.  Use ``detail`` for the
    exception or provider response that should only be written to logs.
    """

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    default_message: ClassVar[str] = "An unexpected server error occurred."

    def __init__(self, message: str | None = None, *, detail: Any = None) -> None:
        self.public_message = message or self.default_message
        self.detail = str(detail) if detail is not None else None
        super().__init__(self.public_message)

    @property
    def public_details(self) -> dict[str, Any] | None:
        """Safe, structured facts a caller needs to act on this failure.

        Almost every failure is fully described by its code and message.  A
        subclass overrides this only when a caller cannot act without one --
        the counts and filters that would narrow a refused board, say -- and
        what it returns is bounded, closed-vocabulary data the client contract
        documents, never provider text, configuration, or ``detail``.
        """

        return None


class InvalidInputError(AppError):
    """The request could not be parsed or fails input validation."""

    status_code = 400
    code = "invalid_input"
    default_message = "The request contains invalid input."


class ResourceNotFoundError(AppError):
    """The requested resource does not exist."""

    status_code = 404
    code = "resource_not_found"
    default_message = "The requested resource was not found."


class ProviderUnavailableError(AppError):
    """An upstream provider required to serve the request is unavailable."""

    status_code = 503
    code = "provider_unavailable"
    default_message = (
        "An upstream provider is currently unavailable. Please try again later."
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: Any = None,
        provider_reason: str | None = None,
    ) -> None:
        self.provider_reason = provider_reason
        super().__init__(message, detail=detail)


class InvalidConfigurationError(AppError):
    """The server cannot safely operate with its current configuration."""

    status_code = 500
    code = "invalid_configuration"
    default_message = "The server configuration is invalid."


class AuthenticationError(AppError):
    """The request could not be authenticated."""

    status_code = 401
    code = "authentication_failed"
    default_message = "The request could not be authenticated."


class AuthenticationRequiredError(AuthenticationError):
    """The request did not provide usable authentication credentials."""

    code = "authentication_required"
    default_message = "Authentication is required to access this resource."


class InvalidTokenError(AuthenticationError):
    """The supplied authentication token is invalid or expired."""

    code = "invalid_token"
    default_message = "The provided Firebase token is invalid."


class RateLimitedError(AppError):
    """A bounded machine client limit was exceeded."""

    status_code = 429
    code = "rate_limited"
    default_message = "The client has exceeded its collection limit."

    def __init__(self, retry_after: int = 60, *, detail: Any = None):
        self.retry_after = max(1, int(retry_after))
        super().__init__(detail=detail)

    @property
    def public_details(self) -> dict[str, Any]:
        return {"retry_after_seconds": self.retry_after}


class AuthorizationError(AppError):
    """An authenticated user is not allowed to perform the operation."""

    status_code = 403
    code = "forbidden"
    default_message = "You do not have permission to access this resource."


class OperationFailedError(AppError):
    """A requested application operation could not be completed safely."""

    status_code = 500
    code = "operation_failed"
    default_message = "The requested operation could not be completed."


class DuplicateOperationError(AppError):
    """A requested operation conflicts with one that is already active."""

    status_code = 409
    code = "duplicate_active_operation"
    default_message = "An identical operation is already running or queued."


class ConflictError(AppError):
    """The request is valid but conflicts with durable current state."""

    status_code = 409
    code = "operation_conflict"
    default_message = "The operation conflicts with the current collection state."


def route_error_boundary(
    safe_message: str,
    *,
    error_type: type[AppError] = OperationFailedError,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Translate unexpected failures from one route into a safe app error.

    Routes should raise the most specific :class:`AppError` they can describe.
    This boundary preserves those expected failures and only translates
    otherwise-unhandled exceptions.  ``error_type`` supplies the public error
    category and status code; ``safe_message`` is the client-facing message.
    The original exception is retained as ``detail`` for the central logger.
    """

    def decorator(handler: Callable[P, R]) -> Callable[P, R]:
        @wraps(handler)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return handler(*args, **kwargs)
            except ProviderResponseError as error:
                # A provider seam has already recorded the malformed event.
                # Translate it at the HTTP boundary so the public contract is
                # the same safe 503 used for provider timeouts and HTTP
                # failures, without incrementing application-failure metrics.
                raise ProviderUnavailableError(detail=error) from error
            except AppError:
                raise
            except Exception as error:
                raise error_type(safe_message, detail=error) from error

        return wrapped

    return decorator


def _error_response(
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
):
    """Build the one public JSON shape used for application errors."""

    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return jsonify({"error": error}), status_code


def register_error_handlers(app: Flask) -> None:
    """Register application and fallback HTTP error handlers on ``app``."""

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):  # type: ignore[no-untyped-def]
        _log_application_error(error)

        # Provider failures are counted at the provider seams; the central
        # handler must not double count them as application failures.
        if error.code != "provider_unavailable":
            record_application_failure(error.code)

        response = _error_response(
            error.code,
            error.public_message,
            error.status_code,
            error.public_details,
        )
        if isinstance(error, RateLimitedError):
            response[0].headers["Retry-After"] = str(error.retry_after)
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):  # type: ignore[no-untyped-def]
        """Keep Werkzeug-generated errors inside the same contract."""

        if error.code == 400:
            app_error = InvalidInputError(detail=error.description)
        elif error.code == 401:
            app_error = AuthenticationRequiredError(detail=error.description)
        elif error.code == 403:
            app_error = AuthorizationError(detail=error.description)
        elif error.code == 404:
            app_error = ResourceNotFoundError(detail=error.description)
        else:
            app_error = AppError(detail=error.description)

        _log_application_error(app_error, http_status=error.code)
        if (error.code or 0) >= 500:
            record_application_failure(f"http_{error.code or 0}")
        return _error_response(
            app_error.code,
            app_error.public_message,
            error.code or app_error.status_code,
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):  # type: ignore[no-untyped-def]
        """Log unexpected details while returning a safe internal error."""

        if isinstance(error, HTTPException):
            return handle_http_error(error)
        if isinstance(error, ProviderResponseError):
            return handle_app_error(ProviderUnavailableError(detail=error))

        app_error = AppError(detail=error)
        return handle_app_error(app_error)
