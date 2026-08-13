from datetime import datetime, timedelta, timezone
import gzip
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from app.models.collection_control import CollectionAlert, CompositionJob, OperatorJob, PublicationStream

from app.migrations import run_migrations
from app.services.collection_control import (
    CollectorTokenService,
    CollectionControlService,
    ControlPlaneError,
    ObservationIngestionService,
    PublicationService,
    CollectionOperationsService,
    EmailAlertAdapter,
)


UTC = timezone.utc


@pytest.fixture
def control_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite3'}")
    run_migrations(engine)
    return engine


def test_collector_tokens_are_scoped_expiring_and_one_time_when_consumed(control_db):
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    def clock():
        return now[0]
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=clock)
    identity = tokens.create_identity("pc", scopes=["poll", "ingest"])
    token = tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["ingest"])
    claims = tokens.validate(token, required_scope="ingest", consume=True)
    assert claims.collector_id == identity["identity_id"]
    with pytest.raises(ControlPlaneError, match="token_replayed"):
        tokens.validate(token, required_scope="ingest", consume=True)
    with pytest.raises(ControlPlaneError, match="scope_denied"):
        tokens.validate(token, required_scope="poll")
    rotated = tokens.rotate(identity["identity_id"], overlap_seconds=60)
    assert rotated["secret"] != identity["secret"]
    delivered = tokens.retrieve_delivery(rotated["delivery_id"])
    assert delivered["secret"] == rotated["secret"]
    with pytest.raises(ControlPlaneError, match="credential_delivery_unavailable"):
        tokens.retrieve_delivery(rotated["delivery_id"])
    assert tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["poll"])
    with pytest.raises(ControlPlaneError, match="invalid_identity_secret"):
        tokens.issue_for_secret(identity["identity_id"], "wrong", scopes=["poll"])
    expiring = tokens.issue(identity["identity_id"], scopes=["poll"], ttl_seconds=1)
    now[0] += timedelta(minutes=6)
    with pytest.raises(ControlPlaneError, match="invalid_token"):
        tokens.validate(expiring, required_scope="poll")


def test_production_token_service_requires_deployment_signing_secret(control_db):
    with pytest.raises(ControlPlaneError, match="signing_secret_required"):
        CollectorTokenService(control_db, environment="production")


def test_bootstrap_requires_active_season_and_manifest_uses_exact_cutoff(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    with pytest.raises(ControlPlaneError, match="season_not_active"):
        control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    control.activate_season("2025-26", actor="operator")
    for kind in ("event", "athlete"):
        request = control.create_bootstrap_request("2025-26", kind, cutoff=cutoff)
        control.publish_catalog(request.request_id, {"kind": kind}, version="v1")
    manifest = control.create_manifest("2025-26", cutoff=cutoff, scopes=["synergy"], collect_before=now + timedelta(hours=1))
    assert manifest.cutoff == cutoff
    assert json.loads(manifest.accepted_versions) == [1, 2]


def test_manifest_cutoff_rejects_late_ingestion_and_acceptance_enqueues_job(control_db):
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    def clock():
        return now[0]
    control = CollectionControlService(control_db, clock=clock)
    control.activate_season("2025-26", actor="operator")
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    for kind in ("event", "athlete"):
        request = control.create_bootstrap_request("2025-26", kind, cutoff=cutoff)
        control.publish_catalog(request.request_id, {"kind": kind}, version="v1")
    manifest = control.create_manifest("2025-26", cutoff=cutoff, scopes=["synergy_play_types"], collect_before=now[0] + timedelta(minutes=1))
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=clock)
    identity = tokens.create_identity("pc", scopes=["ingest"])
    claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["ingest"]))
    publication = PublicationService(control_db, clock=clock)
    publication.register_stream("synergy_play_types", provider="nba", owner="collector", required_observations=["synergy_play_types"], publication_strategy="snapshot_replace", enabled=True)
    ingestion = ObservationIngestionService(control_db, publication_service=publication, clock=clock)
    payload = b'{"rows":[1]}'
    envelope = {"manifest_id": manifest.manifest_id, "client_observation_id": "obs-late", "environment": "testing", "provider": "nba", "observation_type": "synergy_play_types", "scope": {}, "season": "2025-26", "cutoff": cutoff.isoformat(), "schema_version": 2, "checksum": __import__("hashlib").sha256(payload).hexdigest(), "retrieved_at": now[0].isoformat()}
    receipt = ingestion.ingest(claims, envelope, payload)
    assert receipt.replay is False
    with publication.session() as session:
        assert session.scalar(select(CompositionJob).where(CompositionJob.stream_key == "synergy_play_types")) is not None
    now[0] += timedelta(minutes=2)
    with pytest.raises(ControlPlaneError, match="manifest_expired"):
        ingestion.ingest(claims, envelope, payload)


def test_ingestion_is_atomic_and_same_id_replay_returns_original_receipt(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    for kind in ("event", "athlete"):
        request = control.create_bootstrap_request("2025-26", kind, cutoff=cutoff)
        control.publish_catalog(request.request_id, {"kind": kind}, version="v1")
    manifest = control.create_manifest("2025-26", cutoff=cutoff, scopes=["synergy"], collect_before=now + timedelta(hours=1))
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    identity = tokens.create_identity("pc", scopes=["ingest"])
    claims = tokens.validate(tokens.issue(identity["identity_id"], scopes=["ingest"]))
    payload = json.dumps({"rows": [1, 2]}, separators=(",", ":")).encode()
    envelope = {"manifest_id": manifest.manifest_id, "client_observation_id": "obs-1", "environment": "testing",
        "provider": "nba", "observation_type": "synergy", "scope": {"season": "2025-26"}, "season": "2025-26",
        "cutoff": cutoff.isoformat(), "schema_version": 2, "checksum": __import__("hashlib").sha256(payload).hexdigest(),
        "retrieved_at": now.isoformat()}
    ingestion = ObservationIngestionService(control_db, clock=lambda: now)
    first = ingestion.ingest(claims, envelope, gzip.compress(payload), compressed=True)
    second = ingestion.ingest(claims, envelope, gzip.compress(payload), compressed=True)
    assert first.observation_id == second.observation_id
    assert second.replay is True
    envelope["checksum"] = "0" * 64
    with pytest.raises(ControlPlaneError, match="checksum_mismatch"):
        ingestion.ingest(claims, envelope, payload)
    bad_payload = b'{"value":NaN}'
    envelope["checksum"] = __import__("hashlib").sha256(bad_payload).hexdigest()
    with pytest.raises(ControlPlaneError, match="malformed_payload"):
        ingestion.ingest(claims, envelope, bad_payload)


def test_publication_pointer_fences_stale_worker_and_rolls_back(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publications = PublicationService(control_db, clock=lambda: now)
    publications.register_stream("synergy:season", provider="nba", owner="collector", required_observations=[], publication_strategy="replace", enabled=True)
    queued = publications.enqueue("synergy:season", season="2025-26", cutoff=now)
    assert publications.enqueue("synergy:season", season="2025-26", cutoff=now).job_id == queued.job_id
    first = publications.compose("synergy:season", season="2025-26", cutoff=now, payload={"v": 1})
    with pytest.raises(ControlPlaneError, match="stale_composition"):
        publications.compose("synergy:season", season="2025-26", cutoff=now, payload={"v": 2}, expected_fence=0)
    second = publications.compose("synergy:season", season="2025-26", cutoff=now, payload={"v": 2}, expected_fence=first.fence)
    assert publications.current("synergy:season").publication_id == second.publication_id
    rollback = publications.rollback("synergy:season", reason="operator repair")
    assert rollback.payload == first.payload
    publications.register_stream("event_catalog", provider="nba", owner="operator", required_observations=["event_catalog"], publication_strategy="replace", enabled=True)
    publications.register_default_streams()
    assert publications.current("event_catalog") is None
    with publications.session() as session:
        assert session.get(PublicationStream, "event_catalog").enabled is True
    assert any(row.enabled is False for row in publications.register_default_streams() if row.stream_key == "synergy:l15")


def test_cycle_no_game_and_operations_are_bounded_and_audited(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    for kind in ("event", "athlete"):
        request = control.create_bootstrap_request("2025-26", kind, cutoff=cutoff)
        control.publish_catalog(request.request_id, {"kind": kind}, version="v1")
    manifest = control.create_manifest("2025-26", cutoff=cutoff, scopes=["synergy"], collect_before=now + timedelta(hours=1))
    cycle = control.open_cycle(manifest.manifest_id, completed_game_count=0)
    assert cycle.status == "no_game"
    operations = CollectionOperationsService(control_db, clock=lambda: now)
    assert operations.validate_completeness(cycle_id=cycle.cycle_id)["complete"]
    audit = operations.audit(actor="operator", action="cycle.open", resource=cycle.cycle_id, reason="scheduled collection")
    assert audit.reason == "scheduled collection"
    item = operations.reconciliation(season="2025-26", kind="identity", reason="identity_unresolved")
    assert operations.resolve_reconciliation(item.item_id, actor="operator", reason="catalog repaired").status == "resolved"
    assert operations.alert(cycle_id=cycle.cycle_id, severity="warning", code="stale_catalog").code == "stale_catalog"


def test_alert_delivery_failure_rolls_back_durable_mutation(control_db):
    def fail(_subject, _body):
        raise RuntimeError("mailbox unavailable")

    operations = CollectionOperationsService(control_db, alert_adapter=EmailAlertAdapter(fail))
    with pytest.raises(RuntimeError, match="mailbox unavailable"):
        operations.alert(cycle_id=None, severity="critical", code="first_failure")
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionAlert)).all() == []


def test_audit_creates_durable_operator_job(control_db):
    operations = CollectionOperationsService(control_db)
    event = operations.audit(actor="admin", action="cycle.start", resource="cycle", reason="operator requested")
    with control_db.connect() as connection:
        jobs = connection.execute(select(OperatorJob)).all()
    assert jobs and event.details.find(jobs[0][0]) >= 0
