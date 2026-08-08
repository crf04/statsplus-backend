"""Application errors and the public HTTP error contract.

Routes and services raise the errors in this module when they can describe a
failure safely.  Flask converts them to one predictable JSON shape at the
application boundary.  The public message is deliberately separate from the
optional detail so provider responses, credentials, and other implementation
details can be logged without being returned to a caller.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException


logger = logging.getLogger(__name__)


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


def _error_response(code: str, message: str, status_code: int):
    """Build the one public JSON shape used for application errors."""

    return jsonify({"error": {"code": code, "message": message}}), status_code


def register_error_handlers(app: Flask) -> None:
    """Register application and fallback HTTP error handlers on ``app``."""

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):  # type: ignore[no-untyped-def]
        if error.detail:
            logger.error(
                "Application error code=%s detail=%s",
                error.code,
                error.detail,
            )
        else:
            logger.error("Application error code=%s", error.code)

        return _error_response(error.code, error.public_message, error.status_code)

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

        logger.error(
            "HTTP error status=%s code=%s detail=%s",
            error.code,
            app_error.code,
            app_error.detail,
        )
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

        logger.exception("Unhandled application exception: %s", error)
        app_error = AppError(detail=error)
        return _error_response(
            app_error.code,
            app_error.public_message,
            app_error.status_code,
        )
