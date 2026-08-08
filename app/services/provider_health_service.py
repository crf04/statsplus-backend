"""Dependency health checks behind one app-scoped service seam.

The HTTP routes intentionally do not construct provider endpoints, perform
network calls, measure durations, or classify exceptions.  This service owns
those details so the NBA Stats and PBP Stats checks retain distinct timeout,
telemetry, and failure behavior while remaining straightforward to test
without a live provider.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests
from sqlalchemy import text

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.services.nba_stats_adapter import NBAStatsAdapter
from app.services.pbp_stats_adapter import PBP_TOTALS_URL, PBPTotalsAdapter
from app.utils.db import get_engine
from app.utils.nba_api_config import get_shared_nba_session

logger = logging.getLogger(__name__)


class ProviderHealthService:
    """Run database and external-provider health checks."""

    NBA_ENDPOINT = "stats.nba.com/stats/leaguedashteamstats"
    PBP_ENDPOINT = PBP_TOTALS_URL.replace("https://", "")

    def __init__(
        self,
        engine: Any | None = None,
        settings: RuntimeSettings | None = None,
        *,
        nba_stats: NBAStatsAdapter | None = None,
        pbp_stats: PBPTotalsAdapter | None = None,
    ) -> None:
        self.settings = settings or get_runtime_settings()
        self.engine = engine if engine is not None else get_engine(self.settings)
        self.nba_stats = (
            nba_stats if nba_stats is not None else NBAStatsAdapter(settings=self.settings)
        )
        self.pbp_stats = pbp_stats if pbp_stats is not None else PBPTotalsAdapter(
            settings=self.settings,
            session=get_shared_nba_session(self.settings),
        )

    def check_database(self) -> dict[str, Any]:
        """Return a safe database connectivity result."""

        started = time.monotonic()
        dialect = getattr(getattr(self.engine, "dialect", None), "name", None)
        driver = getattr(getattr(self.engine, "dialect", None), "driver", None)
        try:
            with self.engine.connect() as connection:
                ok = connection.execute(text("SELECT 1")).scalar() == 1
            return {
                "status": "healthy" if ok else "unhealthy",
                "response_time_ms": self._duration_ms(started),
                "dialect": dialect,
                "driver": driver,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as error:
            del error
            logger.error("Database health check failed")
            return {
                "status": "unhealthy",
                "error": "Database health check failed.",
                "response_time_ms": self._duration_ms(started),
                "dialect": dialect,
                "driver": driver,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def check_nba_api(self) -> dict[str, Any]:
        """Probe NBA Stats through its typed adapter."""

        started = time.monotonic()
        try:
            self.nba_stats.health_probe()
            return {
                "status": "healthy",
                "response_time_ms": self._duration_ms(started),
                "status_code": self.nba_stats.last_status_code,
                "endpoint": self.NBA_ENDPOINT,
                "provider": "nba_stats",
                "test_type": "team_stats_api",
            }
        except requests.exceptions.Timeout:
            logger.error("NBA API health check timed out")
            return self._unhealthy_provider(
                "NBA API health check timed out.",
                self.NBA_ENDPOINT,
                "nba_stats",
                started,
                status_code=self.nba_stats.last_status_code,
            )
        except requests.exceptions.RequestException:
            logger.error("NBA API health check request failed")
            return self._unhealthy_provider(
                "NBA API health check request failed.",
                self.NBA_ENDPOINT,
                "nba_stats",
                started,
                status_code=self.nba_stats.last_status_code,
            )
        except Exception:
            logger.error("NBA API health check failed")
            return self._unhealthy_provider(
                "NBA API health check failed.",
                self.NBA_ENDPOINT,
                "nba_stats",
                started,
                status_code=self.nba_stats.last_status_code,
            )

    def check_pbp_api(self) -> dict[str, Any]:
        """Probe PBP Stats through its separate adapter and timeout signal."""

        started = time.monotonic()
        try:
            status_code = self.pbp_stats.health_probe()
            return {
                "status": "healthy",
                "response_time_ms": self._duration_ms(started),
                "status_code": status_code,
                "endpoint": self.PBP_ENDPOINT,
                "provider": "pbp_stats",
                "test_type": "totals_api",
            }
        except requests.exceptions.Timeout:
            logger.error("PBP Stats health check timed out")
            return self._unhealthy_provider(
                "PBP Stats health check timed out.",
                self.PBP_ENDPOINT,
                "pbp_stats",
                started,
            )
        except requests.exceptions.RequestException:
            logger.error("PBP Stats health check request failed")
            return self._unhealthy_provider(
                "PBP Stats health check request failed.",
                self.PBP_ENDPOINT,
                "pbp_stats",
                started,
            )
        except Exception:
            logger.error("PBP Stats health check failed")
            return self._unhealthy_provider(
                "PBP Stats health check failed.",
                self.PBP_ENDPOINT,
                "pbp_stats",
                started,
            )

    def detailed(self) -> dict[str, Any]:
        """Return the complete detailed-health payload before HTTP translation."""

        checks = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": self.check_database(),
            "nba_api": self.check_nba_api(),
            "pbp_stats": self.check_pbp_api(),
            "environment": self.settings.environment,
            "version": "1.0.0",
        }
        dependencies = ("database", "nba_api", "pbp_stats")
        healthy = all(checks[name].get("status") == "healthy" for name in dependencies)
        return {
            "status": "healthy" if healthy else "degraded",
            "checks": checks,
        }

    @staticmethod
    def _duration_ms(started: float) -> float:
        return round((time.monotonic() - started) * 1000, 2)

    @staticmethod
    def _unhealthy_provider(
        message: str,
        endpoint: str,
        provider: str,
        started: float,
        *,
        status_code: int | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "unhealthy",
            "error": message,
            "response_time_ms": ProviderHealthService._duration_ms(started),
            "status_code": status_code,
            "endpoint": endpoint,
            "provider": provider,
        }


__all__ = ["ProviderHealthService"]
