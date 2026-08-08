"""Application factory for the NBA stats backend."""

from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from flask import Flask
from flask_cors import CORS

from app.config.settings import (
    RuntimeSettings,
    load_settings,
    set_runtime_settings,
)

logger = logging.getLogger(__name__)


def create_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    """Create and configure the Flask application."""
    load_dotenv()

    config_overrides = config_overrides or {}
    supplied_settings = config_overrides.get("RUNTIME_SETTINGS")
    if isinstance(supplied_settings, RuntimeSettings):
        settings = supplied_settings
    else:
        settings = load_settings(overrides=config_overrides)
    set_runtime_settings(settings)
    logging.basicConfig(level=settings.log_level)

    app = Flask(__name__)
    app.config.update(
        JSON_SORT_KEYS=False,
        TESTING=settings.environment == "testing",
        FLASK_ENV=settings.environment,
        LOG_LEVEL=settings.log_level,
        PORT=settings.port,
        RUNTIME_SETTINGS=settings,
    )
    app.extensions["runtime_settings"] = settings
    if config_overrides:
        app.config.update(config_overrides)

    CORS(app)

    _initialize_dependencies(app)
    _assemble_dependencies(app)
    _register_error_handlers(app)
    _register_blueprints(app)

    return app


def _assemble_dependencies(app: Flask) -> None:
    """Construct or accept the one dependency graph used by all routes."""

    supplied_dependencies = app.config.get("DEPENDENCIES")
    if supplied_dependencies is not None:
        app.extensions["dependencies"] = supplied_dependencies
        return

    from app.dependencies import build_dependencies

    app.extensions["dependencies"] = build_dependencies(
        app.extensions["runtime_settings"]
    )


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

            initialize_firebase_admin(app.extensions["runtime_settings"])
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
    from app.errors import register_error_handlers

    register_error_handlers(app)
