"""HTTP boundary tests for the collection control plane."""

import gzip
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from app.services.collection_control import CollectorClaims, ControlPlaneError


def _install_collection_services(app):
    dependencies = app.extensions["dependencies"]
    dependencies.collector_tokens = Mock()
    dependencies.collection_control = Mock()
    dependencies.observation_ingestion = Mock()
    dependencies.publication_service = Mock()
    dependencies.collection_operations = Mock()
    dependencies.collection_operations.list_reconciliation.return_value = []
    dependencies.collection_operations.record_usage.return_value = None
    dependencies.collector_tokens.validate.return_value = CollectorClaims(
        "collector-1", "statsplus-collector", "testing", frozenset({"poll", "ingest"}), "jti", None,
        "residential_collector", frozenset({"nba"}),
        frozenset({"event_catalog", "athlete_catalog", "synergy_play_types"}),
    )
    dependencies.observation_ingestion.ingest.return_value = SimpleNamespace(
        to_dict=lambda: {"observation_id": "obs", "replay": False}
    )
    dependencies.publication_service.rollback.return_value = SimpleNamespace(
        publication_id="pub", stream_key="stream", status="rollback"
    )
    dependencies.publication_service.activate_stream.return_value = SimpleNamespace(
        stream_key="stream", enabled=True
    )
    dependencies.publication_service.retry.return_value = SimpleNamespace(
        job_id="job", status="queued", attempts=2
    )
    dependencies.collector_tokens.rotate.return_value = {"identity_id": "collector-1", "secret": "must-not-leak"}
    dependencies.collection_operations.rotate_collector.return_value = SimpleNamespace(job_id="job-rotate")
    return dependencies


def test_collector_routes_require_401_bearer_auth(client, app):
    _install_collection_services(app)
    response = client.get("/api/collector/manifest/manifest-1")
    assert response.status_code == 401
    assert response.json["error"]["code"] == "authentication_required"


def test_invalid_collector_token_is_401(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.validate.side_effect = ControlPlaneError("invalid_token")
    response = client.get("/api/collector/manifest/manifest-1", headers={"Authorization": "Bearer bad"})
    assert response.status_code == 401
    assert response.json["error"]["code"] == "invalid_token"


def test_collector_status_report_is_machine_authenticated_and_bounded(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.report_status.return_value = SimpleNamespace(
        identity_id="collector-1", last_seen_at=datetime(2026, 8, 12),
        release_version="collector-1.2.3", release_checksum="a" * 64,
    )
    response = client.post(
        "/api/collector/status",
        headers={"Authorization": "Bearer token"},
        json={"release_version": "collector-1.2.3", "release_checksum": "a" * 64},
    )
    assert response.status_code == 200
    assert response.json == {
        "identity_id": "collector-1",
        "last_seen_at": "2026-08-12T00:00:00",
        "release_version": "collector-1.2.3",
        "release_checksum": "a" * 64,
    }
    dependencies.collector_tokens.report_status.assert_called_once()

    transition = client.post(
        "/api/collector/status", headers={"Authorization": "Bearer token"},
        json={"release_version": "collector-1.2.3", "release_checksum": "a" * 64,
              "state": "retry", "reason": "railway_unavailable"},
    )
    assert transition.status_code == 200

    unsafe = client.post(
        "/api/collector/status", headers={"Authorization": "Bearer token"},
        json={
            "release_version": "collector-1.2.3", "release_checksum": "a" * 64,
            "secret": "must-not-be-accepted",
        },
    )
    assert unsafe.status_code == 400

    unauthenticated = client.post("/api/collector/status", json={
        "release_version": "collector-1.2.3", "release_checksum": "a" * 64,
    })
    assert unauthenticated.status_code == 401


def test_admin_diagnostics_returns_bounded_operational_contract(client, app):
    dependencies = _install_collection_services(app)
    contract = {
        "cycles": [], "alerts": [], "reconciliation": [], "validation": [], "jobs": [],
        "streams": [{
            "stream_key": "synergy_play_types", "provider": "nba",
            "owner": "residential_collector", "enabled": True, "available": True,
            "activation_status": "active", "freshness_rule": "cutoff_current",
            "publication_id": "publication-1", "coverage_cutoff": "2026-08-12T00:00:00+00:00",
            "fence": 4, "freshness_status": "fresh", "age_seconds": 30,
        }],
        "collectors": [{
            "identity_id": "collector-1", "environment": "production", "revoked": False,
            "last_seen_at": "2026-08-12T00:00:00+00:00",
            "release_version": "collector-1.2.3", "release_checksum": "a" * 64,
        }],
        "usage": [{
            "collector_id": "collector-1", "poll_count": 2, "envelope_count": 1,
            "byte_count": 1024, "concurrency_count": 0,
            "limits": {"poll_count": 100, "envelope_count": 1000,
                       "byte_count": 50 * 1024 * 1024, "concurrency_count": 1},
            "window_started_at": "2026-08-12T00:00:00+00:00",
            "window_resets_at": "2026-08-13T00:00:00+00:00",
            "retry_after_seconds": 3600, "concurrency_retry_after_seconds": 0,
        }],
    }
    dependencies.collection_operations.diagnostics.return_value = contract
    response = client.get("/api/admin/collection/diagnostics")
    assert response.status_code == 200
    assert response.json == contract


def test_bootstrap_poll_and_catalog_publication_contracts(client, app):
    dependencies = _install_collection_services(app)
    row = SimpleNamespace(
        request_id="request-1", season="2025-26", catalog_type="event",
        cutoff=datetime(2026, 8, 11),
        status="pending", expires_at=datetime(2026, 8, 13),
        catalog_version=None, completed_at=None, failure_reason=None,
    )
    dependencies.collection_control.bootstrap_status.return_value = row
    poll = client.get(
        "/api/collector/bootstrap/request-1",
        headers={"Authorization": "Bearer token"},
    )
    assert poll.status_code == 200
    assert poll.json["status"] == "pending"

    publication = SimpleNamespace(
        publication_id="publication-1", season="2025-26", catalog_type="event",
        cutoff=row.cutoff, version="v1", checksum="a" * 64,
        published_at=row.expires_at,
    )
    dependencies.observation_ingestion.ingest_catalog.return_value = publication
    payload = {"events": [{"id": "game-1", "status": "Final"}]}
    envelope = {
        "payload": payload,
        "manifest_id": None,
        "client_observation_id": "catalog-1",
        "environment": "testing",
        "provider": "nba",
        "observation_type": "event_catalog",
        "scope": {"window": "regular_season"},
        "season": "2025-26",
        "cutoff": row.cutoff.isoformat(),
        "schema_version": 2,
        "retrieved_at": row.expires_at.isoformat(),
        "checksum": __import__("hashlib").sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "catalog_version": "v1",
    }
    published = client.post(
        "/api/collector/catalog/request-1",
        data=gzip.compress(json.dumps(envelope).encode()),
        headers={"Authorization": "Bearer token", "Content-Encoding": "gzip"},
    )
    assert published.status_code == 201
    assert published.json["publication_id"] == "publication-1"
    dependencies.observation_ingestion.ingest_catalog.assert_called_once()


def test_machine_discovery_returns_bounded_authorized_work(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_control.discover.return_value = {
        "environment": "testing",
        "bootstrap_requests": [{"request_id": "request-1", "status": "pending"}],
        "manifests": [{"manifest_id": "manifest-1", "status": "active", "scopes": ["synergy"]}],
    }
    response = client.get(
        "/api/collector/discovery?limit=10",
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 200
    assert response.json["bootstrap_requests"][0]["request_id"] == "request-1"
    assert response.json["manifests"][0]["manifest_id"] == "manifest-1"
    dependencies.collection_control.discover.assert_called_once_with(
        claims=dependencies.collector_tokens.validate.return_value, limit=10
    )


def test_machine_secret_exchange_is_reachable_and_never_returns_secret(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.issue_for_secret.return_value = "short-lived-token"
    response = client.post("/api/collector/token", json={"identity_id": "collector-1", "secret": "machine-secret", "scopes": ["ingest"]})
    assert response.status_code == 201
    assert response.json == {"token": "short-lived-token"}


def test_invalid_machine_secret_is_401_not_400(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.issue_for_secret.side_effect = ControlPlaneError(
        "invalid_identity_secret"
    )
    response = client.post(
        "/api/collector/token",
        json={"identity_id": "collector-1", "secret": "wrong"},
    )
    assert response.status_code == 401
    assert response.json["error"]["code"] == "invalid_token"


def test_malformed_machine_token_input_is_400(client, app):
    dependencies = _install_collection_services(app)
    response = client.post(
        "/api/collector/token",
        json={"identity_id": "collector-1", "secret": "machine-secret", "ttl_seconds": "300"},
    )
    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_input"
    dependencies.collector_tokens.issue_for_secret.assert_not_called()


def test_observation_route_requires_atomic_compressed_envelope(client, app):
    dependencies = _install_collection_services(app)
    body = {
        "manifest_id": "manifest-1", "client_observation_id": "obs-1", "environment": "testing",
        "provider": "nba", "observation_type": "synergy_play_types", "scope": {},
        "season": "2025-26", "cutoff": "2026-08-11T00:00:00+00:00", "schema_version": 2,
        "retrieved_at": "2026-08-12T00:00:00+00:00", "payload": {"rows": []},
    }
    response = client.post(
        "/api/collector/observations", data=gzip.compress(json.dumps(body).encode()),
        headers={"Authorization": "Bearer token", "Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert response.status_code == 202
    assert dependencies.observation_ingestion.ingest.call_args.kwargs["compressed"] is True

    uncompressed = client.post("/api/collector/observations", json=body, headers={"Authorization": "Bearer token"})
    assert uncompressed.status_code == 400


def test_malformed_json_body_is_stable_400(client, app):
    _install_collection_services(app)
    response = client.post("/api/collector/token", json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_input"


def test_admin_mutations_return_durable_jobs_and_invoke_their_services(client, app):
    dependencies = _install_collection_services(app)
    now = datetime(2026, 8, 12)
    cases = [
        ("season", "/api/admin/collection/seasons/2025-26", {"reason": "activate"}, "activate_season",
         SimpleNamespace(season="2025-26", status="active", activated_at=now)),
        ("rollback", "/api/admin/collection/streams/stream/rollback", {"reason": "restore", "expected_fence": 2}, "rollback_publication",
         SimpleNamespace(publication_id="pub", stream_key="stream", status="rollback")),
        ("activate", "/api/admin/collection/streams/stream/activate", {"reason": "enable"}, "activate_stream",
         SimpleNamespace(stream_key="stream", enabled=True)),
        ("retry", "/api/admin/collection/compositions/job/retry", {"reason": "retry"}, "retry_composition",
         SimpleNamespace(job_id="composition", status="queued", attempts=2)),
        ("start", "/api/admin/collection/cycles/start", {"manifest_id": "manifest", "reason": "start"}, "start_cycle",
         SimpleNamespace(cycle_id="cycle", status="collecting")),
        ("repair", "/api/admin/collection/repair", {"stream_key": "stream", "season": "2025-26", "cutoff": now.isoformat(), "reason": "repair"}, "scoped_repair",
         SimpleNamespace(job_id="composition", status="queued")),
        ("finish", "/api/admin/collection/cycles/cycle/finish", {"status": "complete", "reason": "finish"}, "finish_cycle",
         SimpleNamespace(cycle_id="cycle", status="complete")),
        ("not_applicable", "/api/admin/collection/cycles/cycle/not-applicable", {"stream_key": "stream", "reason": "not applicable"}, "govern_not_applicable",
         SimpleNamespace(stream_key="stream")),
        ("bootstrap", "/api/admin/collection/bootstrap", {"season": "2025-26", "catalog_type": "event", "cutoff": now.isoformat(), "reason": "bootstrap"}, "bootstrap",
         SimpleNamespace(request_id="request", status="pending")),
        ("revoke", "/api/admin/collection/collectors/collector/revoke", {"reason": "revoke"}, "revoke_collector", None),
        ("rotate", "/api/admin/collection/collectors/collector/rotate", {"reason": "rotate"}, "rotate_collector", None),
        ("resolve", "/api/admin/collection/reconciliation/item/resolve", {"reason": "resolve"}, "resolve_reconciliation",
         SimpleNamespace(item_id="item", status="resolved")),
    ]
    for key, path, body, method, resource in cases:
        result = SimpleNamespace(job_id=f"operator-{key}", resource=resource)
        getattr(dependencies.collection_operations, method).return_value = result
        response = client.post(path, json=body)
        assert response.status_code == 202, (key, response.json)
        assert response.json["job_id"] == f"operator-{key}"
        getattr(dependencies.collection_operations, method).assert_called_once()
        getattr(dependencies.collection_operations, method).reset_mock()


def test_admin_and_collector_security_boundaries_have_stable_errors(client, app, monkeypatch, authenticate):
    dependencies = _install_collection_services(app)
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    missing = client.post("/api/admin/collection/bootstrap", json={})
    assert missing.status_code == 401
    assert missing.json["error"]["code"] == "authentication_required"

    headers = authenticate({"admin": False})
    forbidden = client.post("/api/admin/collection/bootstrap", json={"reason": "start"}, headers=headers)
    assert forbidden.status_code == 403
    assert forbidden.json["error"]["code"] == "forbidden"

    dependencies.collector_tokens.validate.return_value = CollectorClaims(
        "collector-1", "statsplus-collector", "testing", frozenset({"ingest"}), "jti", None,
        "residential_collector", frozenset({"nba"}), frozenset({"synergy_play_types"}),
    )
    insufficient = client.get(
        "/api/collector/manifest/manifest-1", headers={"Authorization": "Bearer token"}
    )
    assert insufficient.status_code == 403
    assert insufficient.json["error"]["code"] == "forbidden"

    dependencies.collector_tokens.validate.return_value = CollectorClaims(
        "collector-1", "statsplus-collector", "testing", frozenset({"poll"}), "jti", None,
        "residential_collector", frozenset({"nba"}), frozenset({"grouped_shot_types"}),
    )
    dependencies.collection_control.get_manifest.side_effect = ControlPlaneError("scope_denied")
    cross_surface = client.get(
        "/api/collector/manifest/manifest-1", headers={"Authorization": "Bearer token"}
    )
    assert cross_surface.status_code == 403
    assert cross_surface.json["error"]["code"] == "forbidden"

    monkeypatch.setattr(auth, "get_firebase_app", lambda: None)
    dependencies.collection_operations.rollback_publication.side_effect = ControlPlaneError("stale_composition")
    conflict = client.post(
        "/api/admin/collection/streams/stream/rollback",
        json={"reason": "restore"},
    )
    assert conflict.status_code == 409
    assert conflict.json["error"]["code"] == "operation_conflict"


def test_admin_domain_errors_and_credential_claim_contract(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.retry_composition.side_effect = ControlPlaneError("composition_not_found")
    missing = client.post(
        "/api/admin/collection/compositions/job/retry", json={"reason": "retry"}
    )
    assert missing.status_code == 404
    assert missing.json["error"]["code"] == "resource_not_found"

    dependencies.collector_tokens.validate.return_value = CollectorClaims(
        "collector-1", "statsplus-collector", "testing", frozenset({"ingest"}), "jti", None,
        "residential_collector", frozenset({"nba"}), frozenset({"event_catalog"}),
    )
    dependencies.collector_tokens.claim_delivery.return_value = {
        "delivery_id": "delivery-1", "identity_id": "collector-1", "secret": "replacement"
    }
    claimed = client.post(
        "/api/collector/credential-deliveries/delivery-1/claim",
        json={"secret": "old-secret"}, headers={"Authorization": "Bearer token"},
    )
    assert claimed.status_code == 200
    assert claimed.json["secret"] == "replacement"
    dependencies.collector_tokens.claim_delivery.assert_called_once_with(
        "delivery-1", collector_id="collector-1", presented_secret="old-secret"
    )

    dependencies.collector_tokens.claim_delivery.side_effect = ControlPlaneError("invalid_identity_secret")
    invalid_claim = client.post(
        "/api/collector/credential-deliveries/delivery-1/claim",
        json={"secret": "wrong"}, headers={"Authorization": "Bearer token"},
    )
    assert invalid_claim.status_code == 401
    assert invalid_claim.json["error"]["code"] == "invalid_token"


def test_rotation_response_does_not_expose_long_lived_secret(client, app):
    _install_collection_services(app)
    response = client.post("/api/admin/collection/collectors/id/rotate", json={"reason": "planned rotation"})
    assert response.status_code == 202
    assert "secret" not in response.json


def test_admin_credential_get_returns_metadata_only(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.delivery_metadata.return_value = {
        "delivery_id": "delivery-1", "identity_id": "collector-1",
        "expires_at": "2026-08-13T00:00:00+00:00", "retrieved": False,
    }
    response = client.get("/api/admin/collection/credential-deliveries/delivery-1")
    assert response.status_code == 200
    assert "secret" not in response.json
    dependencies.collector_tokens.retrieve_delivery.assert_not_called()


def test_rate_limit_has_stable_retry_timing(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.record_usage.side_effect = ControlPlaneError("usage_limit")
    response = client.get("/api/collector/manifest/manifest-1", headers={"Authorization": "Bearer token"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json["error"]["details"] == {"retry_after_seconds": 60}
