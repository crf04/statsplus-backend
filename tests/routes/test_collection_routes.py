"""HTTP boundary tests for the collection control plane."""

import gzip
import json
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
        "collector-1", "statsplus-collector", "testing", frozenset({"poll", "ingest"}), "jti", None
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


def test_machine_secret_exchange_is_reachable_and_never_returns_secret(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.issue_for_secret.return_value = "short-lived-token"
    response = client.post("/api/collector/token", json={"identity_id": "collector-1", "secret": "machine-secret", "scopes": ["ingest"]})
    assert response.status_code == 201
    assert response.json == {"token": "short-lived-token"}


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


def test_operator_route_matrix_is_registered_and_reasoned(client, app):
    _install_collection_services(app)
    paths = [
        ("/api/admin/collection/seasons/2025-26", "POST"),
        ("/api/admin/collection/streams/stream/rollback", "POST"),
        ("/api/admin/collection/streams/stream/activate", "POST"),
        ("/api/admin/collection/compositions/job/retry", "POST"),
        ("/api/admin/collection/cycles/start", "POST"),
        ("/api/admin/collection/repair", "POST"),
        ("/api/admin/collection/cycles/cycle/finish", "POST"),
        ("/api/admin/collection/cycles/cycle/not-applicable", "POST"),
        ("/api/admin/collection/bootstrap", "POST"),
        ("/api/admin/collection/collectors/id/revoke", "POST"),
        ("/api/admin/collection/collectors/id/rotate", "POST"),
        ("/api/admin/collection/reconciliation", "GET"),
        ("/api/admin/collection/reconciliation/item/resolve", "POST"),
    ]
    for path, method in paths:
        response = client.open(path, method=method, json={} if method == "POST" else None)
        assert response.status_code != 404, path


def test_rotation_response_does_not_expose_long_lived_secret(client, app):
    _install_collection_services(app)
    response = client.post("/api/admin/collection/collectors/id/rotate", json={"reason": "planned rotation"})
    assert response.status_code == 202
    assert "secret" not in response.json


def test_rate_limit_has_stable_retry_timing(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.record_usage.side_effect = ControlPlaneError("usage_limit")
    response = client.get("/api/collector/manifest/manifest-1", headers={"Authorization": "Bearer token"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json["error"]["details"] == {"retry_after_seconds": 60}
