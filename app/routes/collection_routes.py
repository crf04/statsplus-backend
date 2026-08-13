"""Narrow collector and operator control-plane HTTP contracts."""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from flask import Blueprint, jsonify, request

from app.dependencies import get_dependencies
from app.errors import (
    AuthenticationRequiredError,
    InvalidInputError,
    InvalidTokenError,
    RateLimitedError,
    ResourceNotFoundError,
    route_error_boundary,
)
from app.services.collection_control import ControlPlaneError
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


def _control_error(error: Exception) -> InvalidInputError:
    reason = getattr(error, "reason", "invalid_input")
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
    except (TypeError, ValueError) as error:
        raise InvalidInputError("The collector token request is malformed.", detail=error) from error
    except ControlPlaneError as error:
        raise InvalidTokenError("The collector identity secret is invalid.", detail=error.reason) from error
    return jsonify({"token": token}), 201


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
    raw = request.get_data(cache=False)
    try:
        envelope = json.loads(gzip.decompress(raw))
    except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError) as error:
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
    except (ControlPlaneError, TypeError, ValueError) as error:
        if isinstance(error, ControlPlaneError) and error.reason == "usage_limit":
            raise RateLimitedError(60, detail=error.reason) from error
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
        row = _service("collection_control").activate_season(season, actor=actor)
        _service("collection_operations").audit(actor=actor, action="season.activate", resource=season, reason=reason)
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"season": row.season, "status": row.status, "activated_at": row.activated_at.isoformat()}), 201


@collection_bp.post("/admin/collection/streams/<path:stream_key>/rollback")
@require_admin
@route_error_boundary("Failed to roll back the publication.")
def rollback_publication(stream_key: str):
    body = _body()
    reason = str(body.get("reason", "")).strip()
    try:
        row = _service("publication_service").rollback(stream_key, reason=reason)
        _service("collection_operations").audit(actor=_actor(), action="publication.rollback", resource=stream_key, reason=reason)
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": row.publication_id, "stream_key": row.stream_key, "status": row.status}), 202


@collection_bp.post("/admin/collection/streams/<path:stream_key>/activate")
@require_admin
@route_error_boundary("Failed to activate the publication stream.")
def activate_stream(stream_key: str):
    body = _body()
    try:
        row = _service("publication_service").activate_stream(stream_key, reason=str(body.get("reason", "")))
        _service("collection_operations").audit(actor=_actor(), action="stream.activate", resource=stream_key, reason=str(body.get("reason", "")))
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": stream_key, "stream_key": row.stream_key, "enabled": row.enabled}), 202


@collection_bp.post("/admin/collection/compositions/<job_id>/retry")
@require_admin
@route_error_boundary("Failed to retry composition.")
def retry_composition(job_id: str):
    body = _body()
    try:
        row = _service("publication_service").retry(job_id, reason=str(body.get("reason", "")))
        _service("collection_operations").audit(actor=_actor(), action="composition.retry", resource=job_id, reason=str(body.get("reason", "")))
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": row.job_id, "status": row.status, "attempts": row.attempts}), 202


@collection_bp.post("/admin/collection/cycles/start")
@require_admin
@route_error_boundary("Failed to start collection cycle.")
def start_cycle():
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        cycle = _service("collection_control").open_cycle(str(body["manifest_id"]), completed_game_count=int(body.get("completed_game_count", 0)))
        _service("collection_operations").audit(actor=actor, action="cycle.start", resource=cycle.cycle_id, reason=reason)
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": cycle.cycle_id, "cycle_id": cycle.cycle_id, "status": cycle.status}), 202


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
        job = _service("publication_service").enqueue(str(body["stream_key"]), season=str(body["season"]), cutoff=cutoff)
        _service("collection_operations").audit(actor=actor, action="scoped_repair.start", resource=job.job_id, reason=reason, details={"stream_key": str(body["stream_key"])})
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": job.job_id, "status": job.status}), 202


@collection_bp.post("/admin/collection/cycles/<cycle_id>/finish")
@require_admin
@route_error_boundary("Failed to finish collection cycle.")
def finish_cycle(cycle_id: str):
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        cycle = _service("collection_control").finish_cycle(cycle_id, status=str(body["status"]), reason=reason)
        _service("collection_operations").audit(actor=actor, action="cycle.finish", resource=cycle_id, reason=reason)
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": cycle.cycle_id, "cycle_id": cycle.cycle_id, "status": cycle.status}), 202


@collection_bp.post("/admin/collection/cycles/<cycle_id>/not-applicable")
@require_admin
@route_error_boundary("Failed to govern stream applicability.")
def govern_not_applicable(cycle_id: str):
    body = _body()
    actor, reason = _actor(), str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        row = _service("collection_control").govern_not_applicable(
            cycle_id, str(body["stream_key"]), actor=actor, reason=reason
        )
        _service("collection_operations").audit(actor=actor, action="cycle.not_applicable", resource=cycle_id, reason=reason,
                                                details={"stream_key": row.stream_key})
    except (KeyError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": cycle_id, "cycle_id": cycle_id, "stream_key": row.stream_key, "status": "governed"}), 202


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
        row = _service("collection_control").create_bootstrap_request(
            str(body["season"]), str(body["catalog_type"]), cutoff=cutoff
        )
        _service("collection_operations").audit(actor=actor, action="bootstrap.start", resource=row.request_id, reason=reason)
    except (KeyError, ValueError, TypeError, ControlPlaneError) as error:
        raise _control_error(error) from error
    return jsonify({"job_id": row.request_id, "request_id": row.request_id, "status": row.status}), 202


@collection_bp.post("/admin/collection/collectors/<identity_id>/revoke")
@require_admin
@route_error_boundary("Failed to revoke collector credentials.")
def revoke_collector(identity_id: str):
    body = _body()
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        _service("collector_tokens").revoke(identity_id)
        _service("collection_operations").audit(actor=_actor(), action="collector.revoke", resource=identity_id, reason=reason)
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": identity_id, "status": "revoked"}), 202


@collection_bp.post("/admin/collection/collectors/<identity_id>/rotate")
@require_admin
@route_error_boundary("Failed to rotate collector credentials.")
def rotate_collector(identity_id: str):
    body = _body()
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise InvalidInputError("A human-readable reason is required.")
    try:
        credentials = _service("collector_tokens").rotate(identity_id)
        _service("collection_operations").audit(actor=_actor(), action="collector.rotate", resource=identity_id, reason=reason)
    except ControlPlaneError as error:
        raise _control_error(error) from error
    del credentials
    return jsonify({"job_id": identity_id, "identity_id": identity_id, "status": "rotated"}), 202


@collection_bp.get("/admin/collection/credential-deliveries/<delivery_id>")
@require_admin
@route_error_boundary("Failed to retrieve collector credentials.")
def retrieve_credential_delivery(delivery_id: str):
    try:
        delivery = _service("collector_tokens").retrieve_delivery(delivery_id)
    except ControlPlaneError as error:
        raise ResourceNotFoundError("The one-time credential delivery is unavailable.", detail=error.reason) from error
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
        row = _service("collection_operations").resolve_reconciliation(item_id, actor=_actor(), reason=str(body.get("reason", "")))
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": row.item_id, "status": row.status}), 202


__all__ = ["collection_bp"]
