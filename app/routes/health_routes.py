"""Health check routes for the database and external providers."""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify
from sqlalchemy import text

from ..dependencies import get_dependencies
from ..errors import AppError, ProviderUnavailableError
from ..providers.pbp_stats import PBPStatsAdapter, PBPStatsProvider
from app.config.settings import get_runtime_settings


health_bp = Blueprint("health", __name__, url_prefix="/api/health")
logger = logging.getLogger(__name__)


@health_bp.route("/db", methods=["GET"])
def database_healthcheck() -> Tuple[Any, int]:
    """Check database connectivity.

    Attempts to connect to the configured database and run a trivial query.

    Returns
    -------
    Tuple[Any, int]
        A JSON response with status information and an HTTP status code.
    """

    try:
        engine = get_dependencies().engine
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            ok = result.scalar() == 1

        payload: Dict[str, Any] = {
            "status": "ok" if ok else "error",
            "dialect": engine.dialect.name,
            "driver": getattr(engine.dialect, "driver", None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if not ok:
            raise AppError(
                "The database health check failed.",
                detail="Database health query returned an unexpected result",
            )
        return jsonify(payload), 200

    except Exception as error:
        if isinstance(error, AppError):
            raise
        raise AppError("The database health check failed.", detail=error) from error


@health_bp.route("/detailed", methods=["GET"])
def detailed_health() -> Tuple[Any, int]:
    """Comprehensive health check including PBP Stats connectivity.

    Checks database, PBP Stats connectivity, and environment configuration.

    Returns
    -------
    Tuple[Any, int]
        A JSON response with detailed status information and HTTP status code.
    """
    checks = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'database': _check_database_connection(),
        'pbp_stats': _check_pbp_stats_connectivity(),
        'environment': get_runtime_settings().environment,
        'version': '1.0.0'
    }
    
    all_healthy = all(
        checks[key].get('status') == 'healthy' if isinstance(checks[key], dict) else True
        for key in ['database', 'pbp_stats']
    )
    
    overall_status = 'healthy' if all_healthy else 'degraded'

    if not all_healthy:
        raise ProviderUnavailableError(
            "One or more health-check dependencies are unavailable.",
            detail=checks,
        )
    
    return jsonify({
        'status': overall_status,
        'checks': checks
    }), 200


@health_bp.route("/pbp-stats", methods=["GET"])
def pbp_stats_health() -> Tuple[Any, int]:
    """Test PBP Stats totals connectivity and response validation."""

    return _render_pbp_health()


@health_bp.route("/nba-api", methods=["GET"])
def nba_api_health() -> Tuple[Any, int]:
    """Deprecated compatibility alias for :func:`pbp_stats_health`.

    The old path said "NBA API" even though it has always called PBP Stats.
    Keep it working for existing clients while making the migration explicit.
    """

    return _render_pbp_health(deprecated=True)


def _render_pbp_health(*, deprecated: bool = False) -> Tuple[Any, int]:
    """Render the canonical PBP Stats health response or its old alias."""

    result = _check_pbp_stats_connectivity()
    if result['status'] != 'healthy':
        raise ProviderUnavailableError(
            "The PBP Stats health check failed.",
            detail=result.get("error") or result,
        )

    response = jsonify(result)
    if deprecated:
        response.headers["Deprecation"] = "true"
        response.headers[
            "Warning"
        ] = '299 - "Deprecated; use /api/health/pbp-stats"'
    return response, 200


def _build_pbp_stats_provider() -> PBPStatsProvider:
    """Resolve the app-factory-injected PBP adapter."""

    return get_dependencies().pbp_stats_provider


def _check_pbp_stats_connectivity(
    provider: PBPStatsProvider | None = None,
) -> Dict[str, Any]:
    """Check the PBP Stats totals endpoint through its adapter."""

    try:
        adapter = provider or _build_pbp_stats_provider()
        return adapter.health_check()
    except ProviderUnavailableError as error:
        logger.error("PBP Stats health check failed: %s", error)
        return {
            'status': 'unhealthy',
            'provider': 'PBP Stats',
            'error': error.public_message,
            'response_time_ms': None,
            'endpoint': PBPStatsAdapter.BASE_URL,
        }
    except Exception as error:
        logger.error("PBP Stats health check failed: %s", error)
        return {
            'status': 'unhealthy',
            'provider': 'PBP Stats',
            'error': 'PBP Stats health check failed.',
            'response_time_ms': None,
            'endpoint': PBPStatsAdapter.BASE_URL,
        }


def _check_nba_api_connectivity(
    provider: PBPStatsProvider | None = None,
) -> Dict[str, Any]:
    """Deprecated helper alias retained for integrations importing it."""

    return _check_pbp_stats_connectivity(provider)


def _check_database_connection() -> Dict[str, Any]:
    """Check database connectivity.
    
    Returns
    -------
    Dict[str, Any]
        Database connection status information.
    """
    engine = get_dependencies().engine
    try:
        start_time = time.time()
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            ok = result.scalar() == 1
        
        duration = time.time() - start_time
        
        return {
            'status': 'healthy' if ok else 'unhealthy',
            'response_time_ms': round(duration * 1000, 2),
            'dialect': engine.dialect.name,
            'driver': getattr(engine.dialect, 'driver', None)
        }
        
    except Exception as error:
        logger.error("Database health check failed: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'Database health check failed.',
            'dialect': engine.dialect.name,
            'driver': getattr(engine.dialect, 'driver', None)
        }
