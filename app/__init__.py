"""Application factory for the NBA stats backend."""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

logger = logging.getLogger(__name__)


def create_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    """Create and configure the Flask application."""
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    app = Flask(__name__)
    app.config.update(
        JSON_SORT_KEYS=False,
        TESTING=os.getenv("FLASK_ENV") == "testing",
    )
    if config_overrides:
        app.config.update(config_overrides)

    CORS(app)

    _initialize_dependencies(app)
    _register_error_handlers(app)
    _register_blueprints(app)

    return app


def _initialize_dependencies(app: Flask) -> None:
    """Initialize optional runtime dependencies without making imports fail."""
    if not app.config.get("SKIP_TABLE_CREATE", False):
        try:
            from app.models import create_all_tables

            create_all_tables()
        except Exception as error:
            logger.warning("Could not create database tables: %s", error)

    if not app.config.get("SKIP_FIREBASE_INIT", False):
        try:
            from app.utils.firebase_admin import initialize_firebase_admin

            initialize_firebase_admin()
        except Exception as error:
            logger.warning("Firebase Admin initialization skipped: %s", error)


def _register_blueprints(app: Flask) -> None:
    """Register the public API blueprints in one place."""
    from app.routes.data_update_routes import data_bp
    from app.routes.game_routes import game_bp
    from app.routes.health_routes import health_bp
    from app.routes.nl_routes import nl_bp
    from app.routes.player_routes import player_bp
    from app.routes.team_routes import team_bp
    from app.routes.user_routes import user_bp

    app.register_blueprint(player_bp, url_prefix="/api/players")
    app.register_blueprint(game_bp, url_prefix="/api/games")
    app.register_blueprint(team_bp, url_prefix="/api/teams")
    app.register_blueprint(data_bp, url_prefix="/api/data")
    app.register_blueprint(nl_bp, url_prefix="/api")
    app.register_blueprint(health_bp)
    app.register_blueprint(user_bp, url_prefix="/api/user")


def _register_error_handlers(app: Flask) -> None:
    """Register consistent JSON error responses."""

    @app.errorhandler(401)
    def handle_unauthorized(error):  # type: ignore[no-untyped-def]
        return jsonify({
            "error": "Unauthorized",
            "message": "Please sign in to access this resource",
        }), 401

    @app.errorhandler(403)
    def handle_forbidden(error):  # type: ignore[no-untyped-def]
        return jsonify({
            "error": "Forbidden",
            "message": "You do not have permission to access this resource",
        }), 403
