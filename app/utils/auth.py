"""Authentication decorators for Firebase-backed Flask routes."""

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any, Callable

from flask import current_app, g, request

from app.errors import (
    AppError,
    AuthenticationRequiredError,
    AuthorizationError,
    InvalidTokenError,
    ProviderUnavailableError,
)
from app.services.user_service import UserService
from app.config.settings import RuntimeSettings

from .firebase_admin import get_firebase_app, verify_firebase_token

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: Any) -> bool:
    """Return whether a configuration value explicitly enables a feature."""

    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUE_VALUES


def _is_production_environment() -> bool:
    """Return whether the process is configured as production.

    A production marker in either the process environment or Flask config is
    enough to disable the bypass. This prevents a deployment marker from being
    accidentally shadowed by a stale value in the other configuration source.
    """

    environments = [os.getenv("FLASK_ENV")]
    try:
        environments.extend(
            [current_app.config.get("FLASK_ENV"), current_app.config.get("ENV")]
        )
        runtime_settings = current_app.extensions.get("runtime_settings")
        if isinstance(runtime_settings, RuntimeSettings):
            environments.append(runtime_settings.environment)
    except RuntimeError:
        pass

    return any(
        str(environment or "").strip().lower() == "production"
        for environment in environments
    )


def _is_local_environment() -> bool:
    """Return whether the process is explicitly running locally or in tests."""

    environments = [os.getenv("FLASK_ENV")]
    try:
        environments.extend(
            [current_app.config.get("FLASK_ENV"), current_app.config.get("ENV")]
        )
        runtime_settings = current_app.extensions.get("runtime_settings")
        if isinstance(runtime_settings, RuntimeSettings):
            environments.append(runtime_settings.environment)
        if current_app.testing:
            return True
    except RuntimeError:
        pass

    return any(
        str(environment or "").strip().lower()
        in {"development", "testing", "test"}
        for environment in environments
    )


def _auth_bypass_enabled() -> bool:
    """Return whether an explicitly approved local auth bypass is enabled.

    The bypass is deliberately fail-closed in production, even if both an
    environment variable and an app config value are accidentally left on.
    """

    if _is_production_environment() or not _is_local_environment():
        return False

    try:
        runtime_settings = current_app.extensions.get("runtime_settings")
    except RuntimeError:
        runtime_settings = None

    if isinstance(runtime_settings, RuntimeSettings):
        return runtime_settings.auth.firebase_admin_disabled
    return _is_truthy(os.getenv("FIREBASE_ADMIN_DISABLED"))


def _set_local_bypass_user() -> None:
    """Attach the explicitly enabled synthetic administrator to the request."""

    # Keep an internal marker separate from the claims so a local-only bypass
    # can be distinguished from a Firebase token in authorization checks.
    g.current_user = {
        "uid": "dev-user",
        "email": "dev@example.com",
        "name": "Development User",
        "email_verified": False,
        "firebase_claims": {
            "uid": "dev-user",
            "admin": True,
            "role": "admin",
            "roles": ["admin"],
        },
        "auth_method": "local_bypass",
        "is_dev_bypass": True,
        "db_user": None,
    }


def _firebase_unavailable_error(detail: Any = None) -> ProviderUnavailableError:
    """Build the safe application error for an unavailable auth provider."""

    return ProviderUnavailableError(
        (
            "Firebase authentication is unavailable. Configure Firebase Admin "
            "credentials or enable the local development bypass."
        ),
        detail=detail,
    )


def _is_firebase_unavailable_error(error: Exception) -> bool:
    """Identify the initialization failure raised by the Firebase adapter."""

    message = str(error).lower()
    return (
        "firebase admin not initialized" in message
        or "firebase not initialized" in message
    )


def _sync_firebase_user(
    decoded_token: dict[str, Any], *, tolerate_sync_errors: bool = False
) -> dict[str, Any]:
    """Sync a verified token and attach its normalized user to the request.

    Required authentication propagates database sync failures to its caller.
    Optional authentication passes ``tolerate_sync_errors=True`` so a database
    outage cannot make an otherwise public route unavailable.
    """

    firebase_user_data = {
        "uid": decoded_token.get("uid"),
        "email": decoded_token.get("email"),
        "name": decoded_token.get("name"),
        "picture": decoded_token.get("picture"),
        "email_verified": decoded_token.get("email_verified", False),
    }

    db_user = None
    try:
        db_user = UserService().create_or_update_user(firebase_user_data)
    except Exception as error:
        if not tolerate_sync_errors:
            raise
        logger.warning("Failed to sync user to database: %s", error)

    current_user = {
        **firebase_user_data,
        "firebase_claims": decoded_token,
        "db_user": db_user,
    }
    g.current_user = current_user
    return current_user


def _authenticate_request() -> None:
    """Authenticate the current request or raise a safe application error."""

    try:
        firebase_app = get_firebase_app()
    except Exception as error:  # Firebase SDK can raise varied init errors.
        logger.error("Firebase Admin initialization failed during authentication: %s", error)
        firebase_app = None

    if firebase_app is None:
        if _auth_bypass_enabled():
            logger.warning("Firebase unavailable; using explicitly enabled local auth bypass")
            _set_local_bypass_user()
            return

        logger.error("Firebase unavailable and no local auth bypass is enabled")
        raise _firebase_unavailable_error()

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise AuthenticationRequiredError(
            "Please provide a valid Firebase token.",
            detail="Authorization header was not provided",
        )

    # Split once so token strings containing spaces are passed unchanged to
    # Firebase, while still rejecting malformed or empty bearer credentials.
    try:
        scheme, token = auth_header.split(" ", 1)
        if scheme.lower() != "bearer":
            raise AuthenticationRequiredError(
                "Authorization header must use Bearer scheme.",
                detail="Authorization header used an unsupported scheme",
            )
        if not token.strip():
            raise ValueError("Authorization header must include a token")
    except ValueError as error:
        raise AuthenticationRequiredError(
            "Authorization header must be: Bearer <token>.",
            detail=error,
        ) from error

    try:
        decoded_token = verify_firebase_token(token)

        current_user = _sync_firebase_user(decoded_token)

        logger.info(
            "Authenticated user: %s (%s)",
            current_user["email"],
            current_user["uid"],
        )
        return
    except ValueError as error:
        logger.warning("Invalid token: %s", error)
        raise InvalidTokenError(detail=error) from error
    except AppError:
        raise
    except Exception as error:
        if _is_firebase_unavailable_error(error):
            logger.error("Firebase became unavailable while verifying token: %s", error)
            raise _firebase_unavailable_error(error) from error

        logger.error("Authentication error: %s", error)
        raise AppError("Authentication failed while verifying the token.", detail=error) from error


def require_auth(f: Callable[..., Any]) -> Callable[..., Any]:
    """Require a verified Firebase token for a Flask route.

    With no Firebase Admin app, requests fail closed unless the caller has
    explicitly enabled the local bypass using ``FIREBASE_ADMIN_DISABLED=true``.
    The bypass is never accepted while ``FLASK_ENV=production``.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        # Reuse a user authenticated by an outer auth/authorization decorator
        # so either decorator ordering remains safe and single-pass.
        if get_current_user() is None:
            _authenticate_request()
        return f(*args, **kwargs)

    return decorated_function


def _claims_indicate_admin(claims: Any) -> bool:
    """Return whether Firebase custom claims grant administrator access."""

    if not isinstance(claims, dict):
        return False

    if claims.get("admin") is True:
        return True

    role = claims.get("role")
    if isinstance(role, str) and role.strip().lower() == "admin":
        return True

    roles = claims.get("roles", ())
    if isinstance(roles, str):
        roles = (roles,)
    if isinstance(roles, (list, tuple, set, frozenset)):
        return any(
            isinstance(candidate, str) and candidate.strip().lower() == "admin"
            for candidate in roles
        )

    return False


def _user_is_admin(user: Any) -> bool:
    """Return whether an authenticated user has administrator privileges."""

    if not isinstance(user, dict):
        return False

    if user.get("is_dev_bypass") is True:
        return True

    return _claims_indicate_admin(user.get("firebase_claims"))


def require_admin(f: Callable[..., Any]) -> Callable[..., Any]:
    """Require authentication and an administrator Firebase claim.

    The decorator can be used by itself or inside ``@require_auth``. In the
    latter case it reuses the user already attached to Flask's ``g`` object.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if get_current_user() is None:
            _authenticate_request()

        if not _user_is_admin(get_current_user()):
            raise AuthorizationError(
                "Administrator privileges are required.",
                detail="Authenticated user did not satisfy an admin claim",
            )

        return f(*args, **kwargs)

    return decorated_function


def get_current_user() -> dict[str, Any] | None:
    """Return the authenticated user attached to the current Flask request."""

    return getattr(g, "current_user", None)


def require_auth_optional(f: Callable[..., Any]) -> Callable[..., Any]:
    """Optionally authenticate a request when a bearer token is provided."""

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        # Optional auth remains optional: an absent token never causes a
        # Firebase-unavailable response. If a token is supplied, preserve the
        # historical behavior of ignoring invalid optional credentials.
        auth_header = request.headers.get("Authorization")
        try:
            firebase_app = get_firebase_app() if auth_header else None
        except Exception as error:
            logger.warning("Optional Firebase authentication is unavailable: %s", error)
            firebase_app = None

        if auth_header and firebase_app is not None:
            try:
                scheme, token = auth_header.split(" ", 1)
                if scheme.lower() != "bearer" or not token.strip():
                    return f(*args, **kwargs)

                decoded_token = verify_firebase_token(token)
                _sync_firebase_user(decoded_token, tolerate_sync_errors=True)
            except Exception:
                # Optional auth must not turn a public route into an outage.
                pass

        return f(*args, **kwargs)

    return decorated_function
