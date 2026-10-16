"""HTTP contract for the slate read."""

from datetime import datetime, timezone

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import InvalidInputError, ProviderUnavailableError
from app.services.slate_service import SlateService
from tests.support.slate_catalog import NoNeighbourEvents


def test_get_slate_returns_the_service_payload(client, dependencies):
    expected = {
        "slate_date": "2026-01-02",
        "freshness": {
            "schedule": {
                "status": "fresh",
                "retrieved_at": "2026-01-02T10:00:00+00:00",
            },
            "pool": {"status": "unavailable", "retrieved_at": None, "providers": {}},
        },
        "games": [],
    }
    dependencies.slate_service.get_slate.return_value = expected

    response = client.get("/api/games/slate?date=2026-01-02")

    assert response.status_code == 200
    assert response.get_json() == expected
    dependencies.slate_service.get_slate.assert_called_once_with("2026-01-02")


def test_authenticated_omitted_date_defaults_through_real_service(client, dependencies):
    class Catalog(NoNeighbourEvents):
        def count_events(self, season):
            return 1

        def get_freshness(self, season, *, now):
            return {
                "last_success_at": "2026-03-08T06:00:00+00:00",
                "event_count": 1,
            }

        def get_events_between(self, season, starts_at, ends_at):
            assert starts_at == datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
            assert ends_at == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
            return []

    dependencies.slate_service = SlateService(
        Catalog(),
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2025-26"),
        ),
        clock=lambda: datetime(2026, 3, 8, 16, tzinfo=timezone.utc),
    )

    response = client.get("/api/games/slate")

    assert response.status_code == 200
    assert response.get_json()["slate_date"] == "2026-03-08"


def test_missing_event_catalog_dependency_is_standard_503(client, dependencies):
    dependencies.slate_service = SlateService(
        None,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2025-26"),
        ),
    )

    response = client.get("/api/games/slate?date=2026-01-02")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


class _OnlyCollectedSeason(NoNeighbourEvents):
    """An Event Catalog holding the 2025-26 schedule and nothing else."""

    def count_events(self, season):
        return 1 if season == "2025-26" else 0

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2026-10-14T10:00:00+00:00", "event_count": 1}

    def get_events_between(self, season, starts_at, ends_at):
        assert season == "2025-26"
        return []


def _real_slate_service(dependencies):
    dependencies.slate_service = SlateService(
        _OnlyCollectedSeason(),
        settings=RuntimeSettings(environment="testing", nba=NBASeasonSettings()),
        clock=lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("date", "season"),
    [("2024-12-25", "2024-25"), ("2027-10-25", "2027-28")],
)
def test_slate_for_an_uncollected_season_is_a_definite_404(
    client, dependencies, date, season
):
    _real_slate_service(dependencies)

    response = client.get(f"/api/games/slate?date={date}")

    assert response.status_code == 404
    body = response.get_json()
    assert body["error"]["code"] == "resource_not_found"
    assert body["error"]["message"] == f"No NBA schedule is stored for the {season} season."
    assert body["error"]["details"] == {"season": season}


@pytest.mark.parametrize("date", ["2025-10-21", "2026-10-15"])
def test_slate_for_a_collected_or_fallback_season_is_still_200(
    client, dependencies, date
):
    _real_slate_service(dependencies)

    response = client.get(f"/api/games/slate?date={date}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["slate_date"] == date
    assert body["games"] == []
    assert body["freshness"]["schedule"]["retrieved_at"] == "2026-10-14T10:00:00+00:00"


def test_get_slate_is_anonymous(client, dependencies, monkeypatch):
    """Anonymous since crf04/statsplus#211, even with Firebase enforced."""
    from app.utils import auth

    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "false")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    dependencies.slate_service.get_slate.return_value = {"slate_date": "2026-01-02"}

    response = client.get("/api/games/slate")

    assert response.status_code == 200
    assert response.get_json() == {"slate_date": "2026-01-02"}


def test_get_slate_preserves_an_explicit_empty_date_as_invalid(client, dependencies):
    dependencies.slate_service.get_slate.side_effect = InvalidInputError("bad date")

    response = client.get("/api/games/slate?date=")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    dependencies.slate_service.get_slate.assert_called_once_with("")


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidInputError("bad date"), 400, "invalid_input"),
        (ProviderUnavailableError("missing schedule"), 503, "provider_unavailable"),
    ],
)
def test_get_slate_uses_standard_error_contract(
    client, dependencies, error, status, code
):
    dependencies.slate_service.get_slate.side_effect = error

    response = client.get("/api/games/slate?date=bad")

    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    assert response.headers["X-Request-ID"]
