"""Thin-route tests for the authenticated DFS Board endpoint.

The route is an adapter, so these tests assert what an adapter is responsible
for: authentication, feature gating, typed filters, the statuses and headers of
each outcome, and that the bytes it publishes are exactly the recorded response
fixtures.  Board semantics themselves are asserted at the service seam.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.config.settings import (
    AuthenticationSettings,
    FeatureSettings,
    ProviderSettings,
    RuntimeSettings,
)
from app.providers.dfs import CoverageEvidence, ProviderSnapshot, SnapshotStatus
import app.routes.dfs_routes as dfs_routes
from app.services.dfs_board_response import DFSBoardResponseService
import app.utils.telemetry as telemetry

from tests.services.test_comparison_board import (
    GENERATED_AT,
    RETRIEVED_AT,
    _market,
    _service,
    _snapshot,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dfs_board"


def load_fixture(name: str) -> dict:
    """Read one recorded response fixture; the docs cite these files."""

    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def assert_fixture(name: str, payload) -> None:
    """Assert one response is exactly its recorded fixture.

    Run the suite with ``RECORD_BOARD_FIXTURES=1`` to rewrite the fixtures after
    an intended contract change; review the diff before committing it.
    """

    path = FIXTURE_DIR / f"{name}.json"
    if os.environ.get("RECORD_BOARD_FIXTURES") == "1":
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
        return
    assert payload == json.loads(path.read_text())


def board_settings(*, enabled=True, providers=("dabble", "prizepicks")):
    return RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=False),
        features=FeatureSettings(dfs_board_enabled=enabled),
        providers=ProviderSettings(dfs_enabled_providers=providers),
    )


def complete_snapshots():
    return [
        _snapshot("dabble", (_market(market_id="m-1", threshold="25.5"),)),
        _snapshot(
            "prizepicks",
            (_market(provider="prizepicks", market_id="m-2", threshold="26.0"),),
        ),
    ]


@pytest.fixture
def make_board_client(monkeypatch):
    """Build a client whose board is assembled from offline snapshots."""

    def _make(snapshots=None, *, settings=None, max_markets=None, failures=None):
        from app import create_app
        import app.utils.auth as auth

        runtime_settings = settings or board_settings()
        comparison_service, providers = _service(
            snapshots if snapshots is not None else complete_snapshots(),
            max_markets=max_markets,
        )
        for name, failure in (failures or {}).items():
            providers[name].failure = failure
        response_service = DFSBoardResponseService(
            comparison_service,
            settings=runtime_settings,
        )
        dependencies = SimpleNamespace(
            settings=runtime_settings,
            dfs_board_response_service=response_service,
            user_service=SimpleNamespace(create_or_update_user=lambda data: None),
        )
        # Firebase is present but never contacted: a missing token must be a 401
        # rather than the unavailable-provider response.
        monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
        monkeypatch.setattr(
            auth,
            "verify_firebase_token",
            lambda token: {"uid": "test-uid", "email": "user@example.com"},
        )
        monkeypatch.setattr(auth, "get_dependencies", lambda: dependencies)

        app = create_app(
            {
                "TESTING": True,
                "RUNTIME_SETTINGS": runtime_settings,
                "DEPENDENCIES": dependencies,
                "SKIP_FIREBASE_INIT": True,
                "SKIP_TABLE_CREATE": True,
            }
        )
        return app.test_client(), providers

    return _make


AUTH = {"Authorization": "Bearer test-token"}


# -- authentication and gating ---------------------------------------------


def test_the_board_requires_authentication(make_board_client):
    client, providers = make_board_client()

    response = client.get("/api/dfs/board")

    assert response.status_code == 401
    assert_fixture("unauthenticated", response.get_json())
    assert response.headers["X-Request-ID"]
    assert all(provider.calls == 0 for provider in providers.values())


def test_a_disabled_board_is_absent(make_board_client):
    client, providers = make_board_client(settings=board_settings(enabled=False))

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 404
    assert_fixture("disabled", response.get_json())
    assert all(provider.calls == 0 for provider in providers.values())


@pytest.mark.parametrize(
    "query",
    ["", "?athlete_name=jokic", "?providers=", "?providers=fanduel", "?season="],
)
def test_a_disabled_board_is_absent_whatever_the_query_says(
    make_board_client, monkeypatch, query
):
    """The gate runs before the parser, so a disabled board reads nothing.

    A caller must not be able to learn which filters exist -- or reach the
    parser, a provider, a database, or Redis -- on a deployment that publishes
    no board at all.
    """

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a disabled board parsed a query string")

    monkeypatch.setattr(
        "app.services.dfs_board_response.parse_board_request", explode
    )
    client, providers = make_board_client(settings=board_settings(enabled=False))

    response = client.get(f"/api/dfs/board{query}", headers=AUTH)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "dfs_board_disabled"
    assert all(provider.calls == 0 for provider in providers.values())


def test_a_disabled_board_still_authenticates_first(make_board_client):
    client, _ = make_board_client(settings=board_settings(enabled=False))

    response = client.get("/api/dfs/board?athlete_name=jokic")

    assert response.status_code == 401
    assert_fixture("unauthenticated", response.get_json())


def test_no_provider_specific_public_route_exists(make_board_client):
    client, _ = make_board_client()

    rules = {str(rule) for rule in client.application.url_map.iter_rules()}

    assert "/api/dfs/board" in rules
    assert not any(
        provider in rule
        for rule in rules
        for provider in ("dabble", "prizepicks", "underdog")
    )


def test_importing_the_route_creates_no_client(monkeypatch):
    import importlib

    import app.routes.dfs_routes as dfs_routes

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("route import created a connection")

    monkeypatch.setattr("app.utils.cache_config.get_redis_client", explode)
    monkeypatch.setattr("app.utils.db.get_engine", explode)

    importlib.reload(dfs_routes)


# -- filters ---------------------------------------------------------------


def test_supported_filters_narrow_the_board(make_board_client):
    client, providers = make_board_client()

    response = client.get(
        "/api/dfs/board?providers=dabble&market_statuses=available"
        "&canonical_athlete_ids=203999&canonical_event_ids=0022500001"
        "&canonical_statistic_ids=points",
        headers=AUTH,
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["filters"] == {
        "providers": ["dabble"],
        "canonical_athlete_ids": [203999],
        "canonical_event_ids": ["0022500001"],
        "canonical_statistic_ids": ["points"],
        "market_statuses": ["available"],
    }
    assert [market["provider"] for market in payload["markets"]] == ["dabble"]
    assert providers["prizepicks"].calls == 0


@pytest.mark.parametrize(
    "query",
    [
        "providers=fanduel",
        "market_statuses=settled",
        "canonical_athlete_ids=jokic",
        "season=not-a-season",
        "athlete_name=jokic",
        "providers=",
        "providers=,",
        "providers=dabble,",
        "market_statuses=",
        "canonical_athlete_ids=",
        "canonical_event_ids=",
        "canonical_statistic_ids=",
        "season=",
        "season=%20",
    ],
)
def test_an_invalid_filter_uses_the_shared_400_contract(make_board_client, query):
    client, providers = make_board_client()

    response = client.get(f"/api/dfs/board?{query}", headers=AUTH)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    # An unparsable request never becomes an unfiltered board read.
    assert all(provider.calls == 0 for provider in providers.values())


# -- outcomes --------------------------------------------------------------


def test_a_complete_board_is_served_exactly(make_board_client):
    client, _ = make_board_client()

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 200
    assert_fixture("complete", response.get_json())


def test_a_mixed_partial_and_stale_board_is_served(make_board_client):
    partial = ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.PARTIAL,
        markets=(_market(provider="prizepicks", market_id="m-2", threshold="26.0"),),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            pagination_complete=False,
        ),
        retrieved_at=RETRIEVED_AT,
    )
    stale = _snapshot(
        "dabble",
        (_market(market_id="m-1", threshold="25.5"),),
        retrieved_at=RETRIEVED_AT.replace(hour=19, minute=45),
    )
    client, _ = make_board_client([stale, partial])

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 200
    assert_fixture("mixed_partial_stale", response.get_json())


def test_an_empty_complete_board_is_not_an_outage(make_board_client):
    empty = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(),
        coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
        retrieved_at=RETRIEVED_AT,
    )
    client, _ = make_board_client([empty])

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 200
    assert_fixture("empty", response.get_json())


def test_an_oversized_board_is_refused_without_truncation(make_board_client):
    client, _ = make_board_client(max_markets=1)

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 400
    assert_fixture("oversized", response.get_json())


def test_a_board_no_provider_could_be_read_from_is_a_sanitized_503(make_board_client):
    """A snapshot past its stale ceiling publishes no board, only an outage."""

    beyond = _snapshot(
        "dabble",
        (_market(market_id="m-1", threshold="25.5"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    client, _ = make_board_client([beyond])

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 503
    assert_fixture("unusable_snapshot", response.get_json())


def test_a_total_provider_failure_is_a_sanitized_503(make_board_client):
    client, _ = make_board_client(
        failures={
            "dabble": requests.exceptions.Timeout("https://secret.example?key=abc"),
            "prizepicks": requests.exceptions.Timeout("token=secret"),
        }
    )

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 503
    assert_fixture("total_failure", response.get_json())
    assert "secret" not in response.get_data(as_text=True)


# -- telemetry -------------------------------------------------------------


@pytest.fixture
def board_events():
    """Read the process-wide board request aggregates for one test."""

    telemetry.clear_recorded_provider_events()
    yield lambda: [
        (event["outcome"], event["status_code"])
        for event in telemetry.get_recorded_board_request_events()
    ]
    telemetry.clear_recorded_provider_events()


def test_an_unauthenticated_request_records_no_board_event(
    make_board_client, board_events
):
    """Telemetry begins where the caller's identity does."""

    client, _ = make_board_client()

    assert client.get("/api/dfs/board").status_code == 401
    assert board_events() == []


@pytest.mark.parametrize(
    ("query", "settings_kwargs", "expected"),
    [
        ("", {}, ("served", 200)),
        ("?providers=", {}, ("invalid", 400)),
        ("?athlete_name=jokic", {}, ("invalid", 400)),
        ("", {"enabled": False}, ("disabled", 404)),
    ],
)
def test_every_authenticated_request_records_exactly_one_event(
    make_board_client, board_events, query, settings_kwargs, expected
):
    client, _ = make_board_client(settings=board_settings(**settings_kwargs))

    client.get(f"/api/dfs/board{query}", headers=AUTH)

    assert board_events() == [expected]


def test_an_unavailable_and_an_oversized_read_are_each_one_event(
    make_board_client, board_events
):
    unavailable, _ = make_board_client(
        failures={"dabble": requests.exceptions.Timeout("x")}
    )
    unavailable.get("/api/dfs/board?providers=dabble", headers=AUTH)
    oversized, _ = make_board_client(max_markets=1)
    oversized.get("/api/dfs/board", headers=AUTH)

    assert board_events() == [("unavailable", 503), ("too_large", 400)]


def test_a_revalidated_board_is_one_not_modified_event(
    make_board_client, board_events
):
    client, _ = make_board_client()
    served = client.get("/api/dfs/board", headers=AUTH)

    client.get(
        "/api/dfs/board",
        headers={**AUTH, "If-None-Match": served.headers["ETag"]},
    )

    assert board_events() == [("served", 200), ("not_modified", 304)]


def test_a_dependency_lookup_failure_is_still_one_error_event(
    make_board_client, board_events, monkeypatch
):
    """An observation begins before the graph is read, so a lookup that fails is seen."""

    client, _ = make_board_client()

    def explode():
        raise RuntimeError("host=secret.example")

    monkeypatch.setattr(dfs_routes, "get_dependencies", explode)

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 500
    assert "secret" not in response.get_data(as_text=True)
    assert board_events() == [("error", 500)]


def test_a_rendering_failure_is_an_error_event_that_keeps_what_it_read(
    make_board_client, board_events, monkeypatch
):
    """The status the caller received decides the outcome, not the service's intent.

    The board was assembled and the service meant to serve it, so the facts it
    established are recorded; the response was a 500, so the outcome is the
    error it was.
    """

    client, _ = make_board_client()

    def explode(representation):
        raise RuntimeError("rendering failed")

    monkeypatch.setattr(dfs_routes, "_board_response", explode)

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 500
    assert board_events() == [("error", 500)]
    event = telemetry.get_recorded_board_request_events()[0]
    assert event["market_count"] == 2
    assert event["provider_status_counts"] == {"complete": 2}


def test_a_refused_oversized_read_is_observed_with_the_evidence_it_gathered(
    make_board_client, board_events
):
    """A refusal counts the markets it observed, not the board it never built."""

    client, _ = make_board_client(max_markets=1)

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["observed_market_count"] == 2
    assert board_events() == [("too_large", 400)]
    event = telemetry.get_recorded_board_request_events()[0]
    assert event["market_count"] == 2
    assert event["comparison_availability"] == "available"
    assert event["provider_status_counts"] == {"complete": 2}
    assert event["freshness_counts"] == {"fresh": 2}
    assert event["cache_counts"] == {"unset": 2}
    assert event["disabled_provider_count"] == 1
    assert event["group_count"] == 1
    assert event["unresolved_count"] == 0


def test_a_board_event_carries_no_identity_or_upstream_text(
    make_board_client, board_events
):
    client, _ = make_board_client()

    client.get("/api/dfs/board?canonical_athlete_ids=203999", headers=AUTH)

    event = telemetry.get_recorded_board_request_events()[0]
    labels = {
        label
        for field in (
            "provider_status_counts",
            "failure_reason_counts",
            "freshness_counts",
            "cache_counts",
        )
        for label in event[field]
    }
    assert labels <= {"complete", "partial", "failed", "fresh", "stale", "unset"}
    assert "203999" not in json.dumps(event)


# -- conditional requests and headers --------------------------------------


def assert_private(response) -> None:
    """Every board response belongs to one caller and no shared cache."""

    assert response.headers["Cache-Control"] == (
        "private, no-cache, max-age=0, must-revalidate"
    )
    assert "public" not in response.headers["Cache-Control"]
    assert "Authorization" in response.headers["Vary"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("query", "headers", "settings_kwargs", "status"),
    [
        ("", AUTH, {}, 200),
        ("?providers=", AUTH, {}, 400),
        ("?athlete_name=jokic", AUTH, {}, 400),
        ("", AUTH, {"enabled": False}, 404),
        ("", {}, {}, 401),
    ],
)
def test_every_board_response_is_private(
    make_board_client, query, headers, settings_kwargs, status
):
    client, _ = make_board_client(settings=board_settings(**settings_kwargs))

    response = client.get(f"/api/dfs/board{query}", headers=headers)

    assert response.status_code == status
    assert_private(response)


def test_an_unavailable_and_an_oversized_board_are_private(make_board_client):
    unavailable, _ = make_board_client(
        failures={"dabble": requests.exceptions.Timeout("x")}
    )
    oversized, _ = make_board_client(max_markets=1)

    outage = unavailable.get("/api/dfs/board?providers=dabble", headers=AUTH)
    refused = oversized.get("/api/dfs/board", headers=AUTH)

    assert (outage.status_code, refused.status_code) == (503, 400)
    assert_private(outage)
    assert_private(refused)


def test_an_unexpected_failure_is_private_too(make_board_client, monkeypatch):
    """A centrally handled 500 is still one caller's response, not a cacheable one."""

    def explode(*args, **kwargs):
        raise RuntimeError("host=secret.example")

    monkeypatch.setattr(DFSBoardResponseService, "respond_to_query", explode)
    client, _ = make_board_client()

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.status_code == 500
    assert "secret" not in response.get_data(as_text=True)
    assert_private(response)


@pytest.mark.parametrize(
    ("query", "headers", "settings_kwargs"),
    [
        ("?providers=", AUTH, {}),
        ("", AUTH, {"enabled": False}),
        ("", {}, {}),
    ],
)
def test_only_a_published_board_carries_an_entity_tag(
    make_board_client, query, headers, settings_kwargs
):
    """A tag identifies a board; a failure is not one to revalidate."""

    client, _ = make_board_client(settings=board_settings(**settings_kwargs))

    response = client.get(f"/api/dfs/board{query}", headers=headers)

    assert "ETag" not in response.headers


def test_a_served_board_is_private_and_revalidatable(make_board_client):
    client, _ = make_board_client()

    response = client.get("/api/dfs/board", headers=AUTH)

    assert response.headers["Cache-Control"] == (
        "private, no-cache, max-age=0, must-revalidate"
    )
    assert "public" not in response.headers["Cache-Control"]
    assert response.headers["Vary"] == "Authorization"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["ETag"].startswith('W/"')
    assert response.headers["X-Request-ID"]


def test_an_unchanged_board_revalidates_as_304(make_board_client):
    client, _ = make_board_client()
    served = client.get("/api/dfs/board", headers=AUTH)

    response = client.get(
        "/api/dfs/board",
        headers={**AUTH, "If-None-Match": served.headers["ETag"]},
    )

    assert response.status_code == 304
    assert response.get_data() == b""
    assert response.headers["ETag"] == served.headers["ETag"]
    assert response.headers["Cache-Control"] == served.headers["Cache-Control"]
    assert response.headers["Vary"] == "Authorization"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Request-ID"]


def test_a_304_echoes_the_callers_request_id(make_board_client):
    client, _ = make_board_client()
    served = client.get("/api/dfs/board", headers=AUTH)

    response = client.get(
        "/api/dfs/board",
        headers={
            **AUTH,
            "If-None-Match": served.headers["ETag"],
            "X-Request-ID": "board-revalidation-1",
        },
    )

    assert response.status_code == 304
    assert response.headers["X-Request-ID"] == "board-revalidation-1"


def test_a_different_board_does_not_revalidate(make_board_client):
    client, _ = make_board_client()
    served = client.get("/api/dfs/board", headers=AUTH)

    response = client.get(
        "/api/dfs/board?providers=dabble",
        headers={**AUTH, "If-None-Match": served.headers["ETag"]},
    )

    assert response.status_code == 200
