"""HTTP boundary tests for the collection control plane."""

import gzip
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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
        "collector-1", "statsplus-collector", "testing", frozenset({"poll", "ingest", "catalog_publish"}), "jti", None,
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


def test_rehearsal_evidence_is_machine_authenticated_and_release_bound(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collector_tokens.report_status.return_value = SimpleNamespace(
        identity_id="collector-1", last_seen_at=datetime(2026, 8, 12),
        release_version="collector-1.2.3", release_checksum="a" * 64,
    )
    dependencies.collection_control.verify_rehearsal_receipt.return_value = SimpleNamespace(
        manifest_id="rehearsal-manifest", observation_id="observation-1",
        client_observation_id="rehearsal-client", checksum="b" * 64,
    )
    dependencies.collection_control.rehearsal_operations.return_value = [
        "credential", "auth", "discovery", "status", "ingestion",
    ]
    response = client.post(
        "/api/collector/rehearsal-evidence", headers={"Authorization": "Bearer token"},
        json={"release_version": "collector-1.2.3", "release_checksum": "a" * 64,
              "season": "2025-26", "cutoff": "2026-08-11T00:00:00Z",
              "manifest_id": "rehearsal-manifest", "observation_id": "observation-1",
              "replay_observation_id": "observation-1", "client_observation_id": "rehearsal-client",
              "checksum": "b" * 64},
    )
    assert response.status_code == 200
    assert response.json["identity_id"] == "collector-1"
    assert response.json["environment"] == "testing"
    assert response.json["contract_version"] == 1
    assert response.json["audience"] == "statsplus-collector"
    assert response.json["endpoint"]
    assert set(response.json["operations"]) == {"credential", "auth", "discovery", "status", "ingestion"}
    assert response.json["expires_at"] > response.json["issued_at"]
    assert response.json["replay_verified"] is True


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


#: The admin start request: a family, an operator reason, and the exact active
#: pair and fences being approved.  It deliberately names no rendered format.
_REBUILD_BODY = {
    "family": "traditional_opponent",
    "reason": "publish the opponent rebound split",
    "expected": {
        "season": {"publication_id": "season-pub", "fence": 3},
        "l15": {"publication_id": "l15-pub", "fence": 4},
    },
}


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
        ("rebuild", "/api/admin/collection/publication-rebuilds", _REBUILD_BODY, "start_publication_rebuild",
         SimpleNamespace(rebuild_id="rebuild-1", state="queued", target_format="traditional_opponent.v2")),
        ("family_rollback", "/api/admin/collection/publication-rebuilds/traditional_opponent/rollback",
         {"reason": "restore the previous format"}, "rollback_publication_family",
         (SimpleNamespace(publication_id="pub-a"), SimpleNamespace(publication_id="pub-b"))),
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


def test_activation_route_forwards_candidate_bound_parity_evidence(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.activate_stream.return_value = SimpleNamespace(
        job_id="activation", resource=SimpleNamespace(stream_key="player_per36", enabled=True)
    )

    response = client.post("/api/admin/collection/streams/player_per36/activate", json={
        "reason": "reviewed exact candidate",
        "season": "2025-26",
        "cutoff": "2026-08-12T00:00:00Z",
        "artifact_id": "artifact-1",
        "candidate_publication_id": "publication-1",
    })

    assert response.status_code == 202
    assert dependencies.collection_operations.activate_stream.call_args.args == (
        "player_per36",
    )
    assert dependencies.collection_operations.activate_stream.call_args.kwargs == {
        "actor": "dev-user",
        "reason": "reviewed exact candidate",
        "season": "2025-26",
        "cutoff": datetime.fromisoformat("2026-08-12T00:00:00+00:00"),
        "parity_artifact_id": "artifact-1",
        "candidate_publication_id": "publication-1",
    }

    invalid = client.post("/api/admin/collection/streams/player_per36/activate", json={
        "reason": "reviewed exact candidate",
        "cutoff": "2026-08-12T00:00:00",
    })
    assert invalid.status_code == 400
    assert dependencies.collection_operations.activate_stream.call_count == 1


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


def _install_real_collection_operations(app, tmp_path):
    """Bind the admin/collector routes to real usage and publication state."""

    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.services.collection_control import (
        CollectionOperationsService,
        PublicationService,
    )

    dependencies = _install_collection_services(app)
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine)
    publications.register_default_streams()
    dependencies.collection_operations = CollectionOperationsService(
        engine, publication_service=publications,
    )
    return dependencies, engine


def test_activation_route_enables_an_nba_stream_for_its_first_collection(client, app, tmp_path):
    _install_real_collection_operations(app, tmp_path)

    response = client.post(
        "/api/admin/collection/streams/synergy_play_types_opponent_season/activate",
        json={"reason": "enable for first collection"},
    )

    assert response.status_code == 202, response.json
    assert response.json["stream_key"] == "synergy_play_types_opponent_season"
    assert response.json["enabled"] is True


def test_activation_route_still_requires_a_candidate_for_a_ledger_stream(client, app, tmp_path):
    from sqlalchemy import select as sa_select

    from app.models.collection_control import PublicationStream

    _, engine = _install_real_collection_operations(app, tmp_path)

    response = client.post(
        "/api/admin/collection/streams/traditional_opponent_season/activate",
        json={"reason": "enable for first collection"},
    )

    assert response.status_code == 400
    with engine.connect() as connection:
        assert connection.execute(sa_select(PublicationStream.enabled).where(
            PublicationStream.stream_key == "traditional_opponent_season"
        )).scalar_one() is False


def test_observation_uploads_do_not_consume_the_collector_poll_budget(client, app, tmp_path):
    from sqlalchemy import select as sa_select

    from app.models.collection_control import CollectorUsage

    dependencies, engine = _install_real_collection_operations(app, tmp_path)
    dependencies.collection_control.discover.return_value = {
        "environment": "testing", "bootstrap_requests": [], "manifests": [],
    }
    envelope = {
        "manifest_id": "manifest-1", "client_observation_id": "obs-1",
        "environment": "testing", "provider": "nba",
        "observation_type": "synergy_play_types", "scope": {},
        "season": "2025-26", "cutoff": "2026-08-11T00:00:00+00:00",
        "schema_version": 2, "retrieved_at": "2026-08-12T00:00:00+00:00",
        "payload": {"rows": []},
    }

    for index in range(3):
        upload = client.post(
            "/api/collector/observations",
            data=gzip.compress(json.dumps({
                **envelope, "client_observation_id": f"obs-{index}",
            }).encode()),
            headers={"Authorization": "Bearer token", "Content-Encoding": "gzip"},
        )
        assert upload.status_code == 202

    with engine.connect() as connection:
        uploaded = connection.execute(sa_select(CollectorUsage)).one()
    assert (uploaded.envelope_count, uploaded.poll_count) == (3, 0)

    discovery = client.get(
        "/api/collector/discovery", headers={"Authorization": "Bearer token"}
    )
    assert discovery.status_code == 200
    with engine.connect() as connection:
        polled = connection.execute(sa_select(CollectorUsage)).one()
    assert (polled.envelope_count, polled.poll_count) == (3, 1)


# --- Publication rebuild admin surface -------------------------------------


def test_publication_rebuild_start_returns_202_with_the_durable_identity(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.start_publication_rebuild.return_value = (
        SimpleNamespace(
            job_id="operator-1",
            resource=SimpleNamespace(
                rebuild_id="rebuild-1",
                state="queued",
                target_format="traditional_opponent.v2",
            ),
        )
    )

    response = client.post(
        "/api/admin/collection/publication-rebuilds", json=_REBUILD_BODY
    )

    assert response.status_code == 202
    assert response.json == {
        "job_id": "operator-1",
        "rebuild_id": "rebuild-1",
        "state": "queued",
        "target_format": "traditional_opponent.v2",
    }


def test_publication_rebuild_status_returns_the_bounded_projection(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.publication_rebuild_status.return_value = {
        "rebuild_id": "rebuild-1", "state": "succeeded", "error_code": None
    }

    response = client.get(
        "/api/admin/collection/publication-rebuilds/traditional_opponent/rebuild-1"
    )

    assert response.status_code == 200
    assert response.json["state"] == "succeeded"
    dependencies.collection_operations.publication_rebuild_status.assert_called_once_with(
        "traditional_opponent", "rebuild-1"
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"reason": "publish the split"}, id="no-family"),
        pytest.param({**_REBUILD_BODY, "reason": "x"}, id="short-reason"),
        pytest.param(
            {**_REBUILD_BODY, "expected": {"season": {"publication_id": "p"}}},
            id="incomplete-expectation",
        ),
        pytest.param(
            {**_REBUILD_BODY, "cutoff": "not-a-timestamp"}, id="bad-cutoff"
        ),
    ],
)
def test_publication_rebuild_start_validates_its_request(client, app, body):
    _install_collection_services(app)

    assert client.post(
        "/api/admin/collection/publication-rebuilds", json=body
    ).status_code == 400


def test_a_conflicting_rebuild_request_is_a_stable_409(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.start_publication_rebuild.side_effect = (
        ControlPlaneError("duplicate_active_operation")
    )

    response = client.post(
        "/api/admin/collection/publication-rebuilds", json=_REBUILD_BODY
    )

    assert response.status_code == 409
    # The published code for this exact meaning, not a generic conflict.
    assert response.json["error"]["code"] == "duplicate_active_operation"


def test_an_unknown_rebuild_is_a_stable_404(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.publication_rebuild_status.side_effect = (
        ControlPlaneError("rebuild_not_found")
    )

    response = client.get(
        "/api/admin/collection/publication-rebuilds/traditional_opponent/missing"
    )

    assert response.status_code == 404


def test_an_unsupported_rollback_target_is_a_stable_400(client, app):
    dependencies = _install_collection_services(app)
    dependencies.collection_operations.rollback_publication_family.side_effect = (
        ControlPlaneError("publication_format_unsupported")
    )

    response = client.post(
        "/api/admin/collection/publication-rebuilds/traditional_opponent/rollback",
        json={"reason": "restore the previous format"},
    )

    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_input"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/admin/collection/publication-rebuilds", "post"),
        (
            "/api/admin/collection/publication-rebuilds/traditional_opponent/rebuild-1",
            "get",
        ),
        (
            "/api/admin/collection/publication-rebuilds/traditional_opponent/rollback",
            "post",
        ),
    ],
)
def test_publication_rebuild_routes_are_admin_only(
    client, app, monkeypatch, authenticate, path, method
):
    _install_collection_services(app)
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    unauthenticated = getattr(client, method)(path, json=_REBUILD_BODY)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json["error"]["code"] == "authentication_required"

    headers = authenticate({"admin": False})
    forbidden = getattr(client, method)(path, json=_REBUILD_BODY, headers=headers)
    assert forbidden.status_code == 403
    assert forbidden.json["error"]["code"] == "forbidden"
