"""Health check routes.

Provides endpoints to verify service dependencies such as the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify
from sqlalchemy import text

from ..utils.db import get_engine


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

