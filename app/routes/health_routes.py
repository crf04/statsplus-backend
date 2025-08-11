"""Health check routes.

Provides endpoints to verify service dependencies such as the database and NBA API connectivity.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify
import requests
from sqlalchemy import text

from ..utils.db import get_engine
from ..utils.nba_api_config import get_shared_nba_session


health_bp = Blueprint("health", __name__, url_prefix="/api/health")


@health_bp.route("/db", methods=["GET"])
def database_healthcheck() -> Tuple[Any, int]:
    """Check database connectivity.

    Attempts to connect to the configured database and run a trivial query.

    Returns
    -------
    Tuple[Any, int]
        A JSON response with status information and an HTTP status code.
    """

    engine = get_engine()
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            ok = result.scalar() == 1

        payload: Dict[str, Any] = {
            "status": "ok" if ok else "error",
            "dialect": engine.dialect.name,
            "driver": getattr(engine.dialect, "driver", None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return jsonify(payload), 200 if ok else 500

    except Exception as error:
        payload = {
            "status": "error",
            "dialect": engine.dialect.name,
            "driver": getattr(engine.dialect, "driver", None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(error),
        }
        return jsonify(payload), 500


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
        'environment': os.getenv('FLASK_ENV', 'unknown'),
        'version': '1.0.0'
    }
    
    all_healthy = all(
        checks[key].get('status') == 'healthy' if isinstance(checks[key], dict) else True
        for key in ['database', 'nba_api']
    )
    
    overall_status = 'healthy' if all_healthy else 'degraded'
    status_code = 200 if all_healthy else 503
    
    return jsonify({
        'status': overall_status,
        'checks': checks
    }), status_code


@health_bp.route("/nba-api", methods=["GET"])
def nba_api_health() -> Tuple[Any, int]:
    """Test NBA API connectivity and response time.

    Returns
    -------
    Tuple[Any, int]
        A JSON response with NBA API status and HTTP status code.
    """
    result = _check_nba_api_connectivity()
    status_code = 200 if result['status'] == 'healthy' else 503
    return jsonify(result), status_code


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
        return {
            'status': 'unhealthy',
            'error': str(error),
            'dialect': engine.dialect.name,
            'driver': getattr(engine.dialect, 'driver', None)
        }


def _check_nba_api_connectivity() -> Dict[str, Any]:
    """Test NBA API connectivity and response time.
    
    Returns
    -------
    Dict[str, Any]
        NBA API connectivity status information.
    """
    try:
        start_time = time.time()
        session = get_shared_nba_session()
        
        # Test with a simple NBA API endpoint
        response = session.get(
            'https://stats.nba.com/stats/leaguestandings',
            params={
                'LeagueID': '00',
                'Season': '2024-25',
                'SeasonType': 'Regular Season'
            },
            timeout=(5, 10)
        )
        
        duration = time.time() - start_time
        
        return {
            'status': 'healthy' if response.status_code == 200 else 'unhealthy',
            'response_time_ms': round(duration * 1000, 2),
            'status_code': response.status_code,
            'endpoint': 'stats.nba.com/stats/leaguestandings',
            'using_session_pool': True
        }
        
    except requests.exceptions.Timeout as e:
        return {
            'status': 'unhealthy',
            'error': f'Timeout: {str(e)}',
            'response_time_ms': None,
            'endpoint': 'stats.nba.com/stats/leaguestandings'
        }
    except requests.exceptions.RequestException as e:
        return {
            'status': 'unhealthy',
            'error': f'Request failed: {str(e)}',
            'response_time_ms': None,
            'endpoint': 'stats.nba.com/stats/leaguestandings'
        }
    except Exception as e:
        return {
            'status': 'unhealthy',
            'error': f'Unexpected error: {str(e)}',
            'response_time_ms': None,
            'endpoint': 'stats.nba.com/stats/leaguestandings'
        }

