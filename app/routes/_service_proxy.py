"""Resolve request services from the active Flask application's settings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import current_app, has_app_context

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.utils.db import get_engine


ServiceFactory = Callable[[Any, RuntimeSettings], Any]


def build_data_refresh_job_service(engine: Any, settings: RuntimeSettings) -> Any:
    """Build the app-scoped durable refresh coordinator used by admin routes."""
    from app.services.job_service import (
        DataRefreshJobService,
        build_default_refresh_handlers,
    )

    return DataRefreshJobService(
        engine,
        settings=settings,
        handlers=build_default_refresh_handlers(engine, settings),
    )


class CurrentAppService:
    """Lazy service handle that keeps service state scoped to one Flask app.

    Route modules are imported once per Python process, while an application
    factory may create multiple apps with different settings in that process.
    The handle defers construction until a request (or app context) is active,
    then stores the service in that app's extension registry.  Attributes set
    on the handle remain local overrides, which keeps the existing route-test
    seam for monkeypatching service methods.
    """

    def __init__(self, name: str, factory: ServiceFactory):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_fallback_service", None)
        object.__setattr__(self, "_overrides", {})

    def __getattr__(self, attribute: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if attribute in overrides:
            return overrides[attribute]
        return getattr(self._resolve(), attribute)

    def __setattr__(self, attribute: str, value: Any) -> None:
        if attribute.startswith("_"):
            object.__setattr__(self, attribute, value)
            return
        self._overrides[attribute] = value

    def __delattr__(self, attribute: str) -> None:
        if attribute in self._overrides:
            del self._overrides[attribute]
            return
        raise AttributeError(attribute)

    def _resolve(self) -> Any:
        settings = get_runtime_settings()

        if has_app_context():
            services = current_app.extensions.setdefault("request_services", {})
            service = services.get(self._name)
            if service is None or getattr(service, "settings", None) is not settings:
                service = self._factory(get_engine(settings), settings)
                services[self._name] = service
            return service

        service = self._fallback_service
        if service is None or getattr(service, "settings", None) is not settings:
            service = self._factory(get_engine(settings), settings)
            object.__setattr__(self, "_fallback_service", service)
        return service
