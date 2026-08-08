"""Health check routes.

Provides endpoints to verify service dependencies such as the database and NBA API connectivity.
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify
import requests
from nba_api.stats import endpoints
from sqlalchemy import text

from ..errors import AppError, ProviderUnavailableError
from ..services.nba_stats_adapter import NBAStatsAdapter
from ..services.pbp_stats_adapter import PBPTotalsAdapter, PBP_TOTALS_URL
from ..utils.db import get_engine
from ..utils.nba_api_config import get_shared_nba_session
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
        engine = get_engine()
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
    """Comprehensive health check including NBA API connectivity.

    Checks database, NBA API connectivity, and environment configuration.

    Returns
    -------
    Tuple[Any, int]
        A JSON response with detailed status information and HTTP status code.
    """
    checks = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'database': _check_database_connection(),
        'nba_api': _check_nba_api_connectivity(),
        'pbp_stats': _check_pbp_stats_connectivity(),
        'environment': get_runtime_settings().environment,
        'version': '1.0.0'
    }
    
    all_healthy = all(
        checks[key].get('status') == 'healthy' if isinstance(checks[key], dict) else True
        for key in ['database', 'nba_api', 'pbp_stats']
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


@health_bp.route("/nba-api", methods=["GET"])
def nba_api_health() -> Tuple[Any, int]:
    """Test ``stats.nba.com`` connectivity through the NBA adapter seam.

    Returns
    -------
    Tuple[Any, int]
        A JSON response with NBA API status and HTTP status code.
    """
    result = _check_nba_api_connectivity()
    if result['status'] != 'healthy':
        raise ProviderUnavailableError(
            "The NBA API health check failed.",
            detail=result.get("error") or result,
        )
    return jsonify(result), 200


@health_bp.route("/pbp-api", methods=["GET"])
@health_bp.route("/pbp-stats", methods=["GET"])
def pbp_stats_health() -> Tuple[Any, int]:
    """Test ``api.pbpstats.com`` connectivity with its own signal."""
    result = _check_pbp_stats_connectivity()
    if result["status"] != "healthy":
        raise ProviderUnavailableError(
            "The PBP Stats health check failed.",
            detail=result.get("error") or result,
        )
    return jsonify(result), 200


def _check_database_connection() -> Dict[str, Any]:
    """Check database connectivity.
    
    Returns
    -------
    Dict[str, Any]
        Database connection status information.
    """
    engine = get_engine()
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


def _check_nba_api_connectivity() -> Dict[str, Any]:
    """Test ``stats.nba.com`` via :class:`NBAStatsAdapter`.
    
    Returns
    -------
    Dict[str, Any]
        NBA API connectivity status information.
    """
    try:
        start_time = time.time()
        settings = get_runtime_settings()
        adapter = NBAStatsAdapter(settings=settings)
        adapter.run_endpoint(
            "health_probe",
            lambda timeout: endpoints.LeagueDashTeamStats(
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="Per48",
                league_id_nullable="00",
                timeout=timeout,
            ),
            required_columns=("TEAM_ID", "TEAM_NAME"),
        )

        duration = time.time() - start_time

        return {
            'status': 'healthy',
            'response_time_ms': round(duration * 1000, 2),
            'status_code': adapter.last_status_code,
            'endpoint': 'stats.nba.com/stats/leaguedashteamstats',
            'provider': 'nba_stats',
            'test_type': 'team_stats_api',
        }
        
    except requests.exceptions.Timeout as error:
        logger.error("NBA API health check timed out: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'NBA API health check timed out.',
            'response_time_ms': None,
            'endpoint': 'stats.nba.com/stats/leaguedashteamstats',
            'provider': 'nba_stats',
        }
    except requests.exceptions.RequestException as error:
        logger.error("NBA API health check request failed: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'NBA API health check request failed.',
            'response_time_ms': None,
            'endpoint': 'stats.nba.com/stats/leaguedashteamstats',
            'provider': 'nba_stats',
        }
    except Exception as error:
        logger.error("NBA API health check failed: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'NBA API health check failed.',
            'response_time_ms': None,
            'endpoint': 'stats.nba.com/stats/leaguedashteamstats',
            'provider': 'nba_stats',
        }


def _check_pbp_stats_connectivity() -> Dict[str, Any]:
    """Test ``api.pbpstats.com`` through its separate PBP adapter seam."""
    endpoint = PBP_TOTALS_URL.replace("https://", "")
    try:
        start_time = time.time()
        settings = get_runtime_settings()
        adapter = PBPTotalsAdapter(
            settings=settings,
            session=get_shared_nba_session(settings),
        )
        status_code = adapter.health_probe()
        duration = time.time() - start_time
        return {
            "status": "healthy",
            "response_time_ms": round(duration * 1000, 2),
            "status_code": status_code,
            "endpoint": endpoint,
            "provider": "pbp_stats",
            "test_type": "totals_api",
        }
    except requests.exceptions.Timeout:
        logger.error("PBP Stats health check timed out")
        return {
            "status": "unhealthy",
            "error": "PBP Stats health check timed out.",
            "response_time_ms": None,
            "endpoint": endpoint,
            "provider": "pbp_stats",
        }
    except requests.exceptions.RequestException:
        logger.error("PBP Stats health check request failed")
        return {
            "status": "unhealthy",
            "error": "PBP Stats health check request failed.",
            "response_time_ms": None,
            "endpoint": endpoint,
            "provider": "pbp_stats",
        }
    except Exception:
        logger.exception("PBP Stats health check failed")
        return {
            "status": "unhealthy",
            "error": "PBP Stats health check failed.",
            "response_time_ms": None,
            "endpoint": endpoint,
            "provider": "pbp_stats",
        }
