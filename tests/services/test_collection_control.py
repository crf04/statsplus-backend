from datetime import datetime, timedelta, timezone
import gzip
import json

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.collection_control import (
    CollectorTokenService,
    CollectionControlService,
    ControlPlaneError,
    ObservationIngestionService,
    PublicationService,
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
    assert tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["poll"])
    with pytest.raises(ControlPlaneError, match="invalid_identity_secret"):
        tokens.issue_for_secret(identity["identity_id"], "wrong", scopes=["poll"])
    expiring = tokens.issue(identity["identity_id"], scopes=["poll"], ttl_seconds=1)
    now[0] += timedelta(minutes=6)
    with pytest.raises(ControlPlaneError, match="invalid_token"):
        tokens.validate(expiring, required_scope="poll")


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


def test_publication_pointer_fences_stale_worker_and_rolls_back(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publications = PublicationService(control_db, clock=lambda: now)
    publications.register_stream("synergy:season", provider="nba", owner="collector", required_observations=["synergy"], publication_strategy="replace", enabled=True)
    first = publications.compose("synergy:season", season="2025-26", cutoff=now, payload={"v": 1})
    with pytest.raises(ControlPlaneError, match="stale_composition"):
        publications.compose("synergy:season", season="2025-26", cutoff=now, payload={"v": 2}, expected_fence=0)
    second = publications.compose("synergy:season", season="2025-26", cutoff=now, payload={"v": 2}, expected_fence=first.fence)
    assert publications.current("synergy:season").publication_id == second.publication_id
    rollback = publications.rollback("synergy:season", reason="operator repair")
    assert rollback.payload == first.payload
