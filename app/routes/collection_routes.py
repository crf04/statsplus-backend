"""Narrow collector and operator control-plane HTTP contracts."""

from __future__ import annotations

from datetime import datetime
from flask import Blueprint, jsonify, request

from app.dependencies import get_dependencies
from app.errors import InvalidInputError, ResourceNotFoundError, route_error_boundary
from app.services.collection_control import ControlPlaneError
from app.models.collection_control import ReconciliationItem
from app.utils.auth import require_admin


collection_bp = Blueprint("collection", __name__)


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


def _control_error(error: ControlPlaneError) -> InvalidInputError:
    return InvalidInputError("The collection request could not be completed.", detail=error.reason)


@collection_bp.post("/collector/token")
@require_admin
@route_error_boundary("Failed to issue collector credentials.")
def issue_collector_token():
    body = _body()
    try:
        token = _service("collector_tokens").issue(
            str(body.get("identity_id", "")), scopes=body.get("scopes"), ttl_seconds=int(body.get("ttl_seconds", 300))
        )
    except (ControlPlaneError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify({"token": token}), 201


def _collector_claims(required_scope: str):
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise InvalidInputError("A collector bearer token is required.")
    try:
        return _service("collector_tokens").validate(header[7:].strip(), required_scope=required_scope)
    except ControlPlaneError as error:
        raise InvalidInputError("The collector token is invalid.", detail=error.reason) from error


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
    envelope = _body()
    payload = envelope.pop("payload", None)
    if payload is None:
        raise InvalidInputError("The observation payload is required.")
    try:
        receipt = _service("observation_ingestion").ingest(claims, envelope, __import__("json").dumps(payload, separators=(",", ":")))
    except (ControlPlaneError, TypeError, ValueError) as error:
        raise _control_error(error) from error
    return jsonify(receipt.to_dict()), 202


@collection_bp.post("/admin/collection/seasons/<season>")
@require_admin
@route_error_boundary("Failed to activate the collection season.")
def activate_season(season: str):
    body = _body()
    actor = str(body.get("actor", "")).strip()
    if not actor:
        raise InvalidInputError("An operator identity is required.")
    try:
        row = _service("collection_control").activate_season(season, actor=actor)
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
        _service("collection_operations").audit(actor=str(body.get("actor", "admin")), action="stream.activate", resource=stream_key, reason=str(body.get("reason", "")))
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
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": row.job_id, "status": row.status, "attempts": row.attempts}), 202


@collection_bp.post("/admin/collection/bootstrap")
@require_admin
@route_error_boundary("Failed to create the bootstrap request.")
def create_bootstrap():
    body = _body()
    try:
        cutoff = datetime.fromisoformat(str(body["cutoff"]).replace("Z", "+00:00"))
        row = _service("collection_control").create_bootstrap_request(
            str(body["season"]), str(body["catalog_type"]), cutoff=cutoff
        )
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
        _service("collection_operations").audit(actor=str(body.get("actor", "admin")), action="collector.revoke", resource=identity_id, reason=reason)
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
        _service("collection_operations").audit(actor=str(body.get("actor", "admin")), action="collector.rotate", resource=identity_id, reason=reason)
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": identity_id, "identity_id": identity_id, "secret": credentials["secret"]}), 202


@collection_bp.get("/admin/collection/reconciliation")
@require_admin
@route_error_boundary("Failed to list reconciliation items.")
def list_reconciliation():
    operations = _service("collection_operations")
    with operations.session() as session:
        rows = session.query(ReconciliationItem).filter(ReconciliationItem.status == "open").order_by(ReconciliationItem.created_at.desc()).limit(50).all()
    return jsonify({"items": [{"item_id": row.item_id, "season": row.season, "kind": row.kind, "reason": row.reason, "status": row.status} for row in rows]})


@collection_bp.post("/admin/collection/reconciliation/<item_id>/resolve")
@require_admin
@route_error_boundary("Failed to resolve reconciliation item.")
def resolve_reconciliation(item_id: str):
    body = _body()
    try:
        row = _service("collection_operations").resolve_reconciliation(item_id, actor=str(body.get("actor", "admin")), reason=str(body.get("reason", "")))
    except ControlPlaneError as error:
        raise _control_error(error) from error
    return jsonify({"job_id": row.item_id, "status": row.status}), 202


__all__ = ["collection_bp"]
