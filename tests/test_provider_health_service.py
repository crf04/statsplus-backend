"""Unit tests for the app-scoped provider health service."""

from __future__ import annotations

from types import SimpleNamespace

import requests

from app.config.settings import RuntimeSettings
from app.services.provider_health_service import ProviderHealthService


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, statement):
        return SimpleNamespace(scalar=lambda: 1)


class _Engine:
    dialect = SimpleNamespace(name="sqlite", driver="pysqlite")

    def connect(self):
        return _Connection()


class _HealthyNBA:
    last_status_code = 200

    def health_probe(self):
        return object()


class _HealthyPBP:
    def health_probe(self):
        return 200


def test_provider_health_service_keeps_dependency_details_and_statuses():
    service = ProviderHealthService(
        _Engine(),
        settings=RuntimeSettings(environment="testing"),
        nba_stats=_HealthyNBA(),
        pbp_stats=_HealthyPBP(),
    )

    payload = service.detailed()

    assert payload["status"] == "healthy"
    assert payload["checks"]["database"]["dialect"] == "sqlite"
    assert payload["checks"]["nba_api"]["status_code"] == 200
    assert payload["checks"]["pbp_stats"]["status_code"] == 200


def test_provider_health_service_classifies_timeout_without_raw_detail():
    class TimeoutNBA(_HealthyNBA):
        def health_probe(self):
            raise requests.exceptions.ReadTimeout("provider-secret")

    service = ProviderHealthService(
        _Engine(),
        settings=RuntimeSettings(environment="testing"),
        nba_stats=TimeoutNBA(),
        pbp_stats=_HealthyPBP(),
    )

    result = service.check_nba_api()

    assert result["status"] == "unhealthy"
    assert result["error"] == "NBA API health check timed out."
    assert "provider-secret" not in str(result)
