"""Offline tests for PBP-specific health routes."""

from __future__ import annotations

from typing import Any

from app.errors import ProviderUnavailableError


class HealthyProvider:
    def health_check(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "provider": "PBP Stats",
            "endpoint": "https://api.pbpstats.com/get-totals/nba",
            "response_time_ms": 1.0,
            "status_code": 200,
            "test_type": "totals",
            "using_session_pool": True,
        }


def test_pbp_health_names_the_provider(client, monkeypatch) -> None:
    from app.routes import health_routes

    monkeypatch.setattr(health_routes, "_build_pbp_stats_provider", HealthyProvider)

    response = client.get("/api/health/pbp-stats")

    assert response.status_code == 200
    assert response.get_json()["provider"] == "PBP Stats"
    assert response.get_json()["test_type"] == "totals"


def test_nba_api_health_remains_a_distinct_provider_signal(client, monkeypatch) -> None:
    from app.routes import health_routes

    with client.application.app_context():
        monkeypatch.setattr(
            health_routes.health_service,
            "check_nba_api",
            lambda: {"status": "healthy", "provider": "nba_stats"},
        )

    response = client.get("/api/health/nba-api")

    assert response.status_code == 200
    assert response.get_json()["provider"] == "nba_stats"


def test_pbp_health_provider_failure_uses_central_error_contract(
    client, monkeypatch
) -> None:
    from app.routes import health_routes

    class UnavailableProvider:
        def health_check(self):
            raise ProviderUnavailableError(
                "PBP Stats timed out while fetching totals.",
                detail="provider timeout",
            )

    monkeypatch.setattr(
        health_routes,
        "_build_pbp_stats_provider",
        lambda: UnavailableProvider(),
    )

    response = client.get("/api/health/pbp-stats")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "The PBP Stats health check failed.",
        }
    }


def test_detailed_health_reports_pbp_stats_check(client, monkeypatch) -> None:
    from app.routes import health_routes

    with client.application.app_context():
        monkeypatch.setattr(health_routes.health_service, "detailed", lambda: {
            "status": "healthy",
            "checks": {
                "database": {"status": "healthy", "dialect": "sqlite", "driver": "pysqlite"},
                "nba_api": {"status": "healthy", "provider": "nba_stats"},
                "pbp_stats": HealthyProvider().health_check(),
            },
        })

    response = client.get("/api/health/detailed")

    assert response.status_code == 200
    checks = response.get_json()["checks"]
    assert checks["pbp_stats"]["provider"] == "PBP Stats"
