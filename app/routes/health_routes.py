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
from sqlalchemy import text

from ..errors import AppError, ProviderUnavailableError
from ..utils.db import get_engine
from ..utils.nba_api_config import get_shared_nba_session
from ..utils.telemetry import PROVIDER_PBP_STATS, provider_call
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
        'environment': get_runtime_settings().environment,
        'version': '1.0.0'
    }
    
    all_healthy = all(
        checks[key].get('status') == 'healthy' if isinstance(checks[key], dict) else True
        for key in ['database', 'nba_api']
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
    """Test NBA API connectivity and response time.

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
    """Test NBA API connectivity and response time using game logs endpoint.
    
    Returns
    -------
    Dict[str, Any]
        NBA API connectivity status information.
    """
    try:
        start_time = time.time()
        settings = get_runtime_settings()
        session = get_shared_nba_session(settings)

        # Test with the game logs API endpoint (what you actually use)
        with provider_call(PROVIDER_PBP_STATS, "health_probe") as tracker:
            response = session.get(
                'https://api.pbpstats.com/get-totals/nba',
                params={
                    'Season': settings.nba.current_season,
                    'SeasonType': 'Regular+Season',
                    'Type': 'Player'
                },
                timeout=(
                    settings.providers.pbp_connect_timeout_seconds,
                    settings.providers.pbp_read_timeout_seconds,
                )
            )
            tracker.status_code = response.status_code
            response.raise_for_status()

        duration = time.time() - start_time

        return {
            'status': 'healthy' if response.status_code == 200 else 'unhealthy',
            'response_time_ms': round(duration * 1000, 2),
            'status_code': response.status_code,
            'endpoint': 'api.pbpstats.com/get-totals/nba',
            'test_type': 'game_logs_api',
            'using_session_pool': True
        }
        
    except requests.exceptions.Timeout as error:
        logger.error("NBA API health check timed out: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'NBA API health check timed out.',
            'response_time_ms': None,
            'endpoint': 'api.pbpstats.com/get-totals/nba'
        }
    except requests.exceptions.RequestException as error:
        logger.error("NBA API health check request failed: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'NBA API health check request failed.',
            'response_time_ms': None,
            'endpoint': 'api.pbpstats.com/get-totals/nba'
        }
    except Exception as error:
        logger.error("NBA API health check failed: %s", error)
        return {
            'status': 'unhealthy',
            'error': 'NBA API health check failed.',
            'response_time_ms': None,
            'endpoint': 'api.pbpstats.com/get-totals/nba'
        }
