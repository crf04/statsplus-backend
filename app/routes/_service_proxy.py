"""Compatibility proxy for app-factory-injected request services."""

from __future__ import annotations

from typing import Any

from app.dependencies import get_dependencies


class CurrentAppService:
    """Read-only handle for a service in the active application's graph."""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self._resolve(), attribute)

    def _resolve(self) -> Any:
        return getattr(get_dependencies(), f"{self._name}_service")
