"""Application factory for the NBA stats backend."""

from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from flask import Flask, g, request
from flask_cors import CORS

from app.config.settings import (
    RuntimeSettings,
    load_settings,
    set_runtime_settings,
)
from app.utils.request_id import HEADER_NAME, resolve_request_id

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

    _register_request_headers(app)
    _initialize_dependencies(app)
    _register_error_handlers(app)
    _register_blueprints(app)

    return app


def _register_request_headers(app: Flask) -> None:
    """Correlate every request with one safe ID and echo it to callers."""

    @app.before_request
    def bind_request_id() -> None:
        g.request_id = resolve_request_id(request.headers.get(HEADER_NAME))

    @app.after_request
    def attach_request_id(response):  # type: ignore[no-untyped-def]
        request_id = getattr(g, "request_id", None)
        if not request_id:
            request_id = resolve_request_id(request.headers.get(HEADER_NAME))
        response.headers.setdefault(HEADER_NAME, request_id)
        return response


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

    # Construct one app-scoped queue coordinator with the complete registered
    # operation set.  The route proxies use this same extension, while the
    # defensive guard keeps read-only/demo and explicitly schema-less test apps
    # bootable.
    if not app.config.get("SKIP_TABLE_CREATE", False):
        try:
            from app.services.job_service import (
                DataRefreshJobService,
                build_default_refresh_handlers,
            )
            from app.utils.db import get_engine, is_demo_database_url

            settings = app.extensions["runtime_settings"]
            if not is_demo_database_url(settings.database.url):
                engine = get_engine(settings)
                app.extensions.setdefault("request_services", {})[
                    "data_refresh_jobs"
                ] = DataRefreshJobService(
                    engine,
                    settings=settings,
                    handlers=build_default_refresh_handlers(engine, settings),
                )
        except Exception as error:
            logger.warning("Data refresh queue initialization skipped: %s", error)


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
