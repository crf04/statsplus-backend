"""HTTP adapters for dependency health checks.

Provider I/O, timing, probe construction, and failure classification live in
``ProviderHealthService``.  These routes only invoke the app-scoped service,
translate its result into the existing public status contract, and jsonify it.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from app.dependencies import get_dependencies
from app.errors import AppError, ProviderUnavailableError
from ._service_proxy import CurrentAppService


health_bp = Blueprint("health", __name__, url_prefix="/api/health")
health_service = CurrentAppService("provider_health")


@health_bp.route("/db", methods=["GET"])
def database_healthcheck():
    """Return the small database health payload used by readiness checks."""

    result = health_service.check_database()
    if result.get("status") != "healthy":
        raise AppError(
            "The database health check failed.",
            detail=result.get("error") or result,
        )
    payload = dict(result)
    payload["status"] = "ok"
    return jsonify(payload), 200


@health_bp.route("/detailed", methods=["GET"])
def detailed_health():
    """Return all dependency checks or the safe provider-unavailable error."""

    payload = health_service.detailed()
    if payload.get("status") != "healthy":
        raise ProviderUnavailableError(
            "One or more health-check dependencies are unavailable.",
            detail=payload.get("checks"),
        )
    return jsonify(payload), 200


@health_bp.route("/nba-api", methods=["GET"])
def nba_api_health():
    """Return the NBA Stats provider health signal."""

    result = health_service.check_nba_api()
    if result.get("status") != "healthy":
        raise ProviderUnavailableError(
            "The NBA API health check failed.",
            detail=result.get("error") or result,
        )
    return jsonify(result), 200


@health_bp.route("/pbp-api", methods=["GET"])
@health_bp.route("/pbp-stats", methods=["GET"])
def pbp_stats_health():
    """Return the PBP Stats provider health signal."""

    result = _check_pbp_stats_connectivity()
    if result.get("status") != "healthy":
        raise ProviderUnavailableError(
            "The PBP Stats health check failed.",
            detail=result.get("error") or result,
        )
    return jsonify(result), 200


def _build_pbp_stats_provider():
    """Resolve the app-owned PBP provider for focused health tests/integrations."""

    return get_dependencies().pbp_stats_provider


def _check_pbp_stats_connectivity(provider=None):
    """Run the PBP totals health probe through the app-scoped health seam."""

    if provider is None:
        provider = _build_pbp_stats_provider()
    if hasattr(provider, "health_check"):
        try:
            result = provider.health_check()
            if isinstance(result, dict):
                return result
        except ProviderUnavailableError as error:
            return {"status": "unhealthy", "error": error.public_message}
    return health_service.check_pbp_api()


def _check_database_connection():
    """Compatibility helper delegating to the injected health service."""

    return health_service.check_database()


__all__ = [
    "database_healthcheck",
    "detailed_health",
    "health_bp",
    "health_service",
    "nba_api_health",
    "pbp_stats_health",
]
