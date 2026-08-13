"""Narrow collector and operator control-plane HTTP contracts."""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from flask import Blueprint, jsonify, request

from app.dependencies import get_dependencies
from app.errors import (
    AppError,
    AuthenticationRequiredError,
    AuthorizationError,
    ConflictError,
    InvalidInputError,
    InvalidTokenError,
    RateLimitedError,
    ResourceNotFoundError,
    route_error_boundary,
)
from app.services.collection_control import (
    ControlPlaneError,
    MAX_COMPRESSED_BYTES,
    MAX_ENVELOPE_BYTES,
    decompress_gzip_limited,
    OperatorActionResult,
)
from app.utils.auth import get_current_user, require_admin


collection_bp = Blueprint("collection", __name__)


def _actor() -> str:
    user = get_current_user() or {}
    return str(user.get("uid") or user.get("email") or "admin")[:128]


def _service(name: str):
    service = getattr(get_dependencies(), name, None)
    if service is None:
        raise ResourceNotFoundError("Collection control plane is not configured.")
    return service


def _body() -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise InvalidInputError("A JSON object is required.")
    return value


def _read_limited_body(limit: int) -> bytes:
    """Read a request body in bounded chunks before retaining it."""

    output = bytearray()
    while True:
        chunk = request.stream.read(min(64 * 1024, limit - len(output) + 1))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > limit:
            raise InvalidInputError("The compressed request body is too large.", detail="payload_too_large")
    return bytes(output)


def _control_error(error: Exception) -> AppError:
    reason = getattr(error, "reason", "invalid_input")
    if reason in {
        "identity_revoked", "invalid_identity_secret", "invalid_token", "token_replayed",
    }:
        return InvalidTokenError("The collector identity or token is invalid.", detail=reason)
    if reason in {"scope_denied", "provider_not_registered", "season_not_active"}:
        return AuthorizationError("The collector is not authorized for this operation.", detail=reason)
    if reason.endswith("_not_found") or reason in {
        "manifest_not_found", "bootstrap_not_found", "cycle_not_found",
        "manifest_expired", "bootstrap_expired", "stream_not_found",
        "composition_not_found", "reconciliation_not_found",
    }:
        return ResourceNotFoundError("The collection resource was not found.", detail=reason)
    if reason in {
        "stale_composition", "expected_fence_required", "cycle_immutable",
        "cycle_exists", "observation_id_conflict", "mixed_manifest", "reconciliation_already_resolved",
        "composition_not_retryable", "rollback_unavailable",
    }:
        return ConflictError(detail=reason)
    return InvalidInputError("The collection request could not be completed.", detail=reason)


@collection_bp.post("/collector/token")
@route_error_boundary("Failed to issue collector credentials.")
def issue_collector_token():
    """Exchange a machine secret for a short-lived scoped token."""
    body = _body()
    try:
        token = _service("collector_tokens").issue_for_secret(
            str(body.get("identity_id", "")), str(body.get("secret", "")),
            scopes=body.get("scopes"), ttl_seconds=int(body.get("ttl_seconds", 300))
        )
    except ControlPlaneError as error:
        raise InvalidTokenError("The collector identity secret is invalid.", detail=error.reason) from error
    except (TypeError, ValueError) as error:
        raise InvalidInputError("The collector token request is malformed.", detail=error) from error
    return jsonify({"token": token}), 201


def _bootstrap_response(row):
    return {
        "request_id": row.request_id,
        "season": row.season,
        "catalog_type": row.catalog_type,
        "cutoff": row.cutoff.isoformat(),
        "status": row.status,
        "expires_at": row.expires_at.isoformat(),
        "catalog_version": row.catalog_version,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "failure_reason": row.failure_reason,
    }


@collection_bp.get("/collector/bootstrap/<request_id>")
@collection_bp.get("/collector/bootstrap/<request_id>/status")
@route_error_boundary("Failed to retrieve the bootstrap request.")
def get_bootstrap_status(request_id: str):
    _collector_claims_any(("bootstrap", "poll"))
    try:
        row = _service("collection_control").bootstrap_status(request_id)
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify(_bootstrap_response(row))


@collection_bp.post("/collector/catalog/<request_id>")
@route_error_boundary("Failed to publish the bootstrap catalog.")
def publish_bootstrap_catalog(request_id: str):
    _collector_claims_any(("catalog_publish", "bootstrap", "ingest"))
    body = _body()
    allowed = {"version", "payload", "checksum", "expires_at"}
    if set(body) - allowed or "version" not in body or "payload" not in body:
        raise InvalidInputError("A catalog version and payload are required.", detail="malformed_catalog")
    if len(json.dumps(body["payload"], separators=(",", ":"), ensure_ascii=False).encode()) > MAX_ENVELOPE_BYTES:
        raise InvalidInputError("The catalog payload is too large.", detail="payload_too_large")
    expires_at = None
    if body.get("expires_at") is not None:
        expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
    try:
        row = _service("collection_control").publish_catalog(
            request_id, body["payload"], version=str(body["version"]),
            checksum=str(body["checksum"]) if body.get("checksum") else None,
            expires_at=expires_at,
        )
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify({
        "publication_id": row.publication_id,
        "request_id": request_id,
        "season": row.season,
        "catalog_type": row.catalog_type,
        "cutoff": row.cutoff.isoformat(),
        "version": row.version,
        "checksum": row.checksum,
        "published_at": row.published_at.isoformat(),
    }), 201


def _collector_claims(required_scope: str):
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise AuthenticationRequiredError("A collector bearer token is required.")
    try:
        claims = _service("collector_tokens").validate(header[7:].strip(), required_scope=required_scope)
        _service("collection_operations").record_usage(claims.collector_id, polls=1)
        return claims
    except ControlPlaneError as error:
        if error.reason == "usage_limit":
            raise RateLimitedError(60, detail=error.reason) from error
        raise InvalidTokenError("The collector token is invalid.", detail=error.reason) from error


def _collector_claims_any(required_scopes: tuple[str, ...]):
    """Accept compatible scope names during a rolling collector upgrade."""

    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise AuthenticationRequiredError("A collector bearer token is required.")
    try:
        claims = _service("collector_tokens").validate(header[7:].strip())
        if not set(required_scopes).intersection(claims.scopes):
            raise ControlPlaneError("scope_denied")
        _service("collection_operations").record_usage(claims.collector_id, polls=1)
        return claims
    except ControlPlaneError as error:
        if error.reason == "usage_limit":
            raise RateLimitedError(60, detail=error.reason) from error
        raise _control_error(error) from error


@collection_bp.get("/collector/manifest/<manifest_id>")
@route_error_boundary("Failed to retrieve the collection manifest.")
def get_manifest(manifest_id: str):
    _collector_claims("poll")
    try:
        manifest = _service("collection_control").get_manifest(manifest_id)
    except ControlPlaneError as error:
        raise ResourceNotFoundError("The collection manifest is unavailable.", detail=error.reason) from error
    return jsonify({
        "manifest_id": manifest.manifest_id,
        "season": manifest.season,
        "cutoff": manifest.cutoff.isoformat(),
        "collect_before": manifest.collect_before.isoformat(),
        "accepted_versions": __import__("json").loads(manifest.accepted_versions),
        "scopes": __import__("json").loads(manifest.scopes),
        "checksum": manifest.checksum,
    })


@collection_bp.post("/collector/observations")
@route_error_boundary("Failed to ingest the observation.")
def ingest_observation():
    claims = _collector_claims("ingest")
    if request.headers.get("Content-Encoding", "").lower() != "gzip":
        raise InvalidInputError("Observation envelopes must use gzip compression.", detail="compression_required")
    raw = _read_limited_body(MAX_COMPRESSED_BYTES)
    try:
        envelope = json.loads(decompress_gzip_limited(
            raw,
            max_input_bytes=MAX_COMPRESSED_BYTES,
            max_output_bytes=MAX_ENVELOPE_BYTES,
        ))
    except (ControlPlaneError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidInputError("The compressed observation envelope is malformed.", detail="malformed_envelope") from error
    if not isinstance(envelope, dict):
        raise InvalidInputError("The compressed observation envelope is malformed.", detail="malformed_envelope")
    payload = envelope.pop("payload", None)
    if payload is None:
        raise InvalidInputError("The observation payload is required.", detail="malformed_envelope")
    envelope["checksum"] = str(envelope.get("checksum") or __import__("hashlib").sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest())
    accepted = {"manifest_id", "client_observation_id", "environment", "provider", "observation_type",
                "scope", "season", "cutoff", "schema_version", "retrieved_at", "checksum"}
    if set(envelope) != accepted:
        raise InvalidInputError("The observation envelope fields are invalid.", detail="malformed_envelope")
    encoded_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    try:
        _service("collection_operations").record_usage(claims.collector_id, envelopes=1, bytes_received=len(raw))
        receipt = _service("observation_ingestion").ingest(
            claims, envelope, gzip.compress(encoded_payload), compressed=True
        )
    except ControlPlaneError as error:
        if error.reason == "usage_limit":
            raise RateLimitedError(60, detail=error.reason) from error
        raise _control_error(error) from error
    except (TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify(receipt.to_dict()), 202


@collection_bp.post("/admin/collection/seasons/<season>")
@require_admin
@route_error_boundary("Failed to activate the collection season.")
def activate_season(season: str):
    body = _body()
    actor = _actor()
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        result = _service("collection_operations").activate_season(
            season, actor=actor, reason=reason
        )
        row = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "season": row.season, "status": row.status, "activated_at": row.activated_at.isoformat()}), 202


@collection_bp.post("/admin/collection/streams/<path:stream_key>/rollback")
@require_admin
@route_error_boundary("Failed to roll back the publication.")
def rollback_publication(stream_key: str):
    body = _body()
    reason = str(body.get("reason", "")).strip()
    try:
        result = _service("collection_operations").rollback_publication(
            stream_key, actor=_actor(), reason=reason,
            expected_fence=body.get("expected_fence"),
        )
        row = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "publication_id": row.publication_id, "stream_key": row.stream_key, "status": row.status}), 202


@collection_bp.post("/admin/collection/streams/<path:stream_key>/activate")
@require_admin
@route_error_boundary("Failed to activate the publication stream.")
def activate_stream(stream_key: str):
    body = _body()
    try:
        result = _service("collection_operations").activate_stream(
            stream_key, actor=_actor(), reason=str(body.get("reason", ""))
        )
        row = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "stream_key": row.stream_key, "enabled": row.enabled}), 202


@collection_bp.post("/admin/collection/compositions/<job_id>/retry")
@require_admin
@route_error_boundary("Failed to retry composition.")
def retry_composition(job_id: str):
    body = _body()
    try:
        result = _service("collection_operations").retry_composition(
            job_id, actor=_actor(), reason=str(body.get("reason", ""))
        )
        row = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "composition_job_id": row.job_id, "status": row.status, "attempts": row.attempts}), 202


@collection_bp.post("/admin/collection/cycles/start")
@require_admin
@route_error_boundary("Failed to start collection cycle.")
def start_cycle():
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        result = _service("collection_operations").start_cycle(
            str(body["manifest_id"]), actor=actor, reason=reason
        )
        cycle = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "cycle_id": cycle.cycle_id, "status": cycle.status}), 202


@collection_bp.post("/admin/collection/repair")
@require_admin
@route_error_boundary("Failed to schedule scoped repair.")
def scoped_repair():
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        cutoff = datetime.fromisoformat(str(body["cutoff"]).replace("Z", "+00:00"))
        result = _service("collection_operations").scoped_repair(
            str(body["stream_key"]), season=str(body["season"]), cutoff=cutoff,
            actor=actor, reason=reason,
        )
        job = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "composition_job_id": job.job_id, "status": job.status}), 202


@collection_bp.post("/admin/collection/cycles/<cycle_id>/finish")
@require_admin
@route_error_boundary("Failed to finish collection cycle.")
def finish_cycle(cycle_id: str):
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        result = _service("collection_operations").finish_cycle(
            cycle_id, status=str(body["status"]), actor=actor, reason=reason
        )
        cycle = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "cycle_id": cycle.cycle_id, "status": cycle.status}), 202


@collection_bp.post("/admin/collection/cycles/<cycle_id>/not-applicable")
@require_admin
@route_error_boundary("Failed to govern stream applicability.")
def govern_not_applicable(cycle_id: str):
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        result = _service("collection_operations").govern_not_applicable(
            cycle_id, str(body["stream_key"]), actor=actor, reason=reason
        )
        row = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "cycle_id": cycle_id, "stream_key": row.stream_key, "status": "governed"}), 202


@collection_bp.post("/admin/collection/bootstrap")
@require_admin
@route_error_boundary("Failed to create the bootstrap request.")
def create_bootstrap():
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        cutoff = datetime.fromisoformat(str(body["cutoff"]).replace("Z", "+00:00"))
        result = _service("collection_operations").bootstrap(
            str(body["season"]), str(body["catalog_type"]), cutoff=cutoff,
            actor=actor, reason=reason,
        )
        row = result.resource
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (KeyError, ValueError, TypeError) as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "request_id": row.request_id, "status": row.status}), 202


@collection_bp.post("/admin/collection/collectors/<identity_id>/revoke")
@require_admin
@route_error_boundary("Failed to revoke collector credentials.")
def revoke_collector(identity_id: str):
    body = _body()
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        result = _service("collection_operations").revoke_collector(
            identity_id, actor=_actor(), reason=reason
        )
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "identity_id": identity_id, "status": "revoked"}), 202


@collection_bp.post("/admin/collection/collectors/<identity_id>/rotate")
@require_admin
@route_error_boundary("Failed to rotate collector credentials.")
def rotate_collector(identity_id: str):
    body = _body()
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        result = _service("collection_operations").rotate_collector(
            identity_id, actor=_actor(), reason=reason,
            overlap_seconds=int(body.get("overlap_seconds", 3600)),
        )
    except ControlPlaneError as error:
        raise _control_error(error) from error
    except (TypeError, ValueError) as error:
        raise _control_error(error) from error
    # A legacy injected dependency graph may not yet expose the transactional
    # wrapper; preserve its status-only response while production always uses
    # the durable result above.
    job_id = result.job_id if isinstance(result, OperatorActionResult) else identity_id
    return jsonify({"job_id": job_id, "identity_id": identity_id, "status": "rotated"}), 202


@collection_bp.get("/admin/collection/credential-deliveries/<delivery_id>")
@require_admin
@route_error_boundary("Failed to retrieve collector credentials.")
def retrieve_credential_delivery(delivery_id: str):
    try:
        delivery = _service("collector_tokens").delivery_metadata(delivery_id)
    except ControlPlaneError as error:
        raise ResourceNotFoundError("The one-time credential delivery is unavailable.", detail=error.reason) from error
    return jsonify(delivery)


@collection_bp.post("/collector/credential-deliveries/<delivery_id>/claim")
@route_error_boundary("Failed to deliver the rotated collector credential.")
def claim_credential_delivery(delivery_id: str):
    claims = _collector_claims_any(("credential", "ingest"))
    body = _body()
    presented = body.get("secret")
    if not isinstance(presented, str) or not presented:
        raise InvalidInputError("The previous machine secret is required.", detail="secret_required")
    try:
        delivery = _service("collector_tokens").claim_delivery(
            delivery_id, collector_id=claims.collector_id, presented_secret=presented
        )
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify(delivery)


@collection_bp.get("/admin/collection/reconciliation")
@require_admin
@route_error_boundary("Failed to list reconciliation items.")
def list_reconciliation():
    rows = _service("collection_operations").list_reconciliation(limit=50)
    return jsonify({"items": [{"item_id": row.item_id, "season": row.season, "kind": row.kind, "reason": row.reason, "status": row.status} for row in rows]})


@collection_bp.get("/admin/collection/diagnostics")
@require_admin
@route_error_boundary("Failed to retrieve collection diagnostics.")
def collection_diagnostics():
    return jsonify(_service("collection_operations").diagnostics(limit=50))


@collection_bp.post("/admin/collection/reconciliation/<item_id>/resolve")
@require_admin
@route_error_boundary("Failed to resolve reconciliation item.")
def resolve_reconciliation(item_id: str):
    body = _body()
    try:
        result = _service("collection_operations").resolve_reconciliation(
            item_id, actor=_actor(), reason=str(body.get("reason", ""))
        )
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": result.job_id, "item_id": result.resource.item_id, "status": result.resource.status}), 202


__all__ = ["collection_bp"]
