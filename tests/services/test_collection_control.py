from datetime import datetime, timedelta, timezone
import gzip
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from app.models.collection_control import (
    ActiveSeason,
    AuditEvent,
    BootstrapRequest,
    CatalogPublication,
    CollectionAlert,
    CollectionCycle,
    CompositionJob,
    CollectionObservation,
    CollectionManifest,
    CollectorUsage,
    CollectorLease,
    OperatorJob,
    PublicationPointer,
    PublicationVersion,
    PublicationObservation,
    PublicationStream,
    ReconciliationItem,
)
from app.models.event_catalog import EventCatalogEntry
from app.models.athlete_catalog import AthleteCatalog

from app.migrations import run_migrations
from app.services.collection_control import (
    CollectorTokenService,
    CollectionControlService,
    ControlPlaneError,
    ObservationIngestionService,
    PublicationService,
    CollectionOperationsService,
    EmailAlertAdapter,
    NBA_TEAM_IDS,
)


UTC = timezone.utc


def _catalog_payload(kind, *, future=False):
    team_ids = sorted(NBA_TEAM_IDS)
    if kind == "event":
        scheduled = "2026-08-12T00:00:00+00:00" if future else "2026-08-11T00:00:00+00:00"
        return {
            "events": [
                {
                    "nba_game_id": f"game-{index}",
                    "home_team_id": team_ids[index * 2],
                    "away_team_id": team_ids[index * 2 + 1],
                    "phase": "Regular Season",
                    "status": "Scheduled" if future else "Final",
                    "scheduled_at": scheduled,
                    "athlete_ids": ["1"],
                }
                for index in range(15)
            ]
        }
    return {"identities": [{
        "player_id": "1", "team_id": team_ids[0], "status": "active",
        "event_ids": [f"game-{index}" for index in range(15)],
    }]}


@pytest.fixture
def control_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite3'}")
    run_migrations(engine)
    now = datetime(2026, 8, 12, tzinfo=UTC)
    team_ids = sorted(NBA_TEAM_IDS)
    with engine.begin() as connection:
        for index in range(15):
            connection.execute(EventCatalogEntry.__table__.insert().values(
                nba_game_id=f"game-{index}", season="2025-26",
                home_team_id=int(team_ids[index * 2]), home_team_name="Home",
                home_team_tricode="ATL", away_team_id=int(team_ids[index * 2 + 1]),
                away_team_name="Away", away_team_tricode="BOS",
                scheduled_at=datetime(2026, 8, 11, tzinfo=UTC),
                status_text="Final", status_code=3, postponed_status=None,
                postponement_evidence=None, classification="Regular Season",
                first_seen_at=now, last_seen_at=now,
            ))
        connection.execute(AthleteCatalog.__table__.insert().values(
            season="2025-26", player_id=1, display_name="Player One",
            roster_status="active", is_active=True, is_active_for_season=True,
            team_id=int(team_ids[0]), team_name="Home", team_abbreviation="ATL",
            published_at=now,
        ))
    return engine


def test_collector_tokens_are_scoped_expiring_and_one_time_when_consumed(control_db):
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    def clock():
        return now[0]
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=clock)
    identity = tokens.create_identity(
        "pc", scopes=["poll", "ingest"], owner="residential_collector",
        providers=["nba"], surfaces=["event_catalog", "athlete_catalog", "synergy_play_types"],
    )
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
    tokens.revoke(identity["identity_id"])
    with control_db.connect() as connection:
        actions = [row.action for row in connection.execute(select(AuditEvent)).all()]
    assert {"collector.token_issued", "collector.token_used", "collector.revoked"} <= set(actions)


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
        control.publish_catalog(request.request_id, _catalog_payload(kind, future=kind == "event"), version="v1")
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
        control.publish_catalog(request.request_id, _catalog_payload(kind), version="v1")
    manifest = control.create_manifest("2025-26", cutoff=cutoff, scopes=["synergy_play_types"], collect_before=now[0] + timedelta(minutes=1))
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=clock)
    identity = tokens.create_identity(
        "pc", scopes=["ingest"], owner="collector", providers=["nba"],
        surfaces=["synergy_play_types"],
    )
    claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["ingest"]))
    publication = PublicationService(control_db, clock=clock)
    publication.register_stream("synergy_play_types", provider="nba", owner="collector", required_observations=["synergy_play_types"], publication_strategy="snapshot_replace", enabled=True)
    ingestion = ObservationIngestionService(control_db, publication_service=publication, clock=clock)
    payload = json.dumps({
        "base": "play_types",
        "rows": [{"slice_key": "Transition", "category": "Transition"}],
    }, separators=(",", ":")).encode()
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
        control.publish_catalog(request.request_id, _catalog_payload(kind), version="v1")
    manifest = control.create_manifest("2025-26", cutoff=cutoff, scopes=["synergy"], collect_before=now + timedelta(hours=1))
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    identity = tokens.create_identity(
        "pc", scopes=["ingest"], owner="residential_collector", providers=["nba"],
        surfaces=["synergy_play_types"],
    )
    claims = tokens.validate(tokens.issue(identity["identity_id"], scopes=["ingest"]))
    payload = json.dumps({
        "base": "play_types",
        "rows": [{"slice_key": "Transition"}, {"slice_key": "Isolation"}],
    }, separators=(",", ":")).encode()
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
    conflict_payload = json.dumps({
        "base": "play_types", "rows": [{"slice_key": "Isolation"}],
    }, separators=(",", ":")).encode()
    envelope["checksum"] = __import__("hashlib").sha256(conflict_payload).hexdigest()
    with pytest.raises(ControlPlaneError, match="observation_id_conflict"):
        ingestion.ingest(claims, envelope, conflict_payload)
    with control_db.connect() as connection:
        conflict_audits = connection.execute(
            select(AuditEvent).where(AuditEvent.action == "observation.rejected")
        ).all()
    assert len(conflict_audits) == 1


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
        control.publish_catalog(request.request_id, _catalog_payload(kind, future=kind == "event"), version="v1")
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


def test_operator_mutation_audit_and_job_are_one_transaction(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    operations = CollectionOperationsService(control_db, collection_control=control, clock=lambda: now)

    result = operations.activate_season(
        "2025-26", actor="operator", reason="activate regular season"
    )
    assert result.job.status == "succeeded"
    with control_db.connect() as connection:
        assert connection.execute(select(ActiveSeason)).first().status == "active"
        assert connection.execute(select(OperatorJob)).first().job_id == result.job_id
        assert connection.execute(select(AuditEvent)).first().details.find(result.job_id) >= 0

    with pytest.raises(ControlPlaneError, match="reconciliation_not_found"):
        operations.resolve_reconciliation("missing", actor="operator", reason="repair record")
    with control_db.connect() as connection:
        assert len(connection.execute(select(OperatorJob)).all()) == 1
        assert len(connection.execute(select(AuditEvent)).all()) == 1


def test_cycle_game_count_comes_from_event_catalog_not_request_body(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    control.publish_catalog(event_request.request_id, _catalog_payload("event"), version="event-v1")
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    control.publish_catalog(athlete_request.request_id, _catalog_payload("athlete"), version="athlete-v1")
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes=["synergy"],
        collect_before=now + timedelta(hours=1), required_athlete_ids=["1"],
    )
    cycle = control.open_cycle(manifest.manifest_id, completed_game_count=999)
    assert cycle.completed_game_count == 15
    assert cycle.status == "collecting"


def test_publication_requires_expected_fence_after_initial_pointer(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "fenced", provider="nba", owner="collector", required_observations=[],
        publication_strategy="replace", enabled=True,
    )
    first = publication.compose("fenced", season="2025-26", cutoff=now, payload={"v": 1})
    with pytest.raises(ControlPlaneError, match="expected_fence_required"):
        publication.compose("fenced", season="2025-26", cutoff=now, payload={"v": 2})
    assert publication.current("fenced").publication_id == first.publication_id


def test_completeness_uses_same_manifest_provider_scope_and_registered_evidence(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "league", provider="nba", owner="collector", required_observations=["league_obs"],
        publication_strategy="replace", supported_windows=["season"],
        completeness_rule="league_complete", enabled=True,
    )
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs-league", client_observation_id="client-league",
            collector_id="collector", manifest_id="manifest-1", environment="testing",
            provider="nba", observation_type="league_obs", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2,
            checksum="a" * 64, payload=json.dumps(
                {"rows": [{"team_id": team_id} for team_id in sorted(NBA_TEAM_IDS)]}
            ),
            payload_bytes=2, retrieved_at=now, accepted_at=now,
        ))
    publication.compose(
        "league", season="2025-26", cutoff=now, payload={"published": True},
        manifest_id="manifest-1",
    )
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs-foreign", client_observation_id="client-foreign",
            collector_id="collector", manifest_id="manifest-2", environment="testing",
            provider="other", observation_type="league_obs", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2,
            checksum="b" * 64, payload=json.dumps({"team_ids": ["not-a-team"]}),
            payload_bytes=2, retrieved_at=now, accepted_at=now,
        ))
    assert publication.current("league").payload == '{"published":true}'


def test_base_completeness_requires_registered_base(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "synergy_play_types", provider="nba", owner="collector", required_observations=["synergy_play_types"],
        publication_strategy="replace", supported_windows=["season"],
        completeness_rule="base_complete", enabled=True,
    )
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs-base", client_observation_id="client-base",
            collector_id="collector", manifest_id="manifest-base", environment="testing",
            provider="nba", observation_type="synergy_play_types", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2,
            checksum="c" * 64, payload=json.dumps({"base": "shot_types", "rows": [{"category": "x"}]}),
            payload_bytes=2, retrieved_at=now, accepted_at=now,
        ))
    with pytest.raises(ControlPlaneError, match="base_incomplete"):
        publication.compose(
            "synergy_play_types", season="2025-26", cutoff=now, payload={"published": True},
            manifest_id="manifest-base",
        )


def test_compressed_observation_is_rejected_before_oversize_allocation(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    claims = __import__("app.services.collection_control", fromlist=["CollectorClaims"]).CollectorClaims(
        "collector-1", "statsplus-collector", "testing", frozenset({"ingest"}), "jti", now,
        "collector", frozenset({"nba"}), frozenset({"synergy_play_types"}),
    )
    ingestion = ObservationIngestionService(control_db, clock=lambda: now)
    payload = gzip.compress(json.dumps({"rows": ["x"] * 100}).encode())
    envelope = {
        "manifest_id": "missing", "client_observation_id": "obs", "environment": "testing",
        "provider": "nba", "observation_type": "synergy", "scope": {},
        "season": "2025-26", "cutoff": now.isoformat(), "schema_version": 2,
        "retrieved_at": now.isoformat(), "checksum": "0" * 64,
    }
    with pytest.raises(ControlPlaneError, match="payload_too_large"):
        ingestion.ingest(claims, envelope, payload, compressed=True, max_payload_bytes=16)


def test_catalog_bootstrap_uses_the_observation_envelope_and_is_idempotent(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    payload = _catalog_payload("event")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = {
        "manifest_id": None, "client_observation_id": "catalog-1", "environment": "testing",
        "provider": "nba", "observation_type": "event_catalog",
        "scope": {"window": "regular_season"}, "season": "2025-26",
        "cutoff": cutoff.isoformat(), "schema_version": 2,
        "retrieved_at": now.isoformat(),
        "checksum": __import__("hashlib").sha256(encoded).hexdigest(),
    }
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    identity = tokens.create_identity(
        "pc", scopes=["catalog_publish"], owner="residential_collector", providers=["nba"],
        surfaces=["event_catalog"], identity_id="collector-1",
    )
    claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["catalog_publish"]))
    ingestion = ObservationIngestionService(control_db, collection_control=control, clock=lambda: now)
    publication = ingestion.ingest_catalog(
        claims, envelope, encoded, request_id=request.request_id, catalog_version="v1"
    )
    assert publication.catalog_type == "event"
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionObservation)).first().manifest_id is None
    replay = ingestion.ingest_catalog(
        claims, envelope, encoded, request_id=request.request_id, catalog_version="v1"
    )
    assert replay.publication_id == publication.publication_id


def test_discovery_is_environment_and_scope_bound(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, environment="testing", clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    request = control.create_bootstrap_request(
        "2025-26", "event", cutoff=datetime(2026, 8, 11, tzinfo=UTC)
    )
    discovered = control.discover(
        environment="testing", scopes=["poll"], collector_id="collector",
        owner="residential_collector", providers=["nba"], surfaces=["event_catalog", "athlete_catalog"],
    )
    assert [item["request_id"] for item in discovered["bootstrap_requests"]] == [request.request_id]
    with pytest.raises(ControlPlaneError, match="scope_denied"):
        control.discover(
            environment="testing", scopes=["ingest"], collector_id="collector",
            owner="residential_collector", providers=["nba"], surfaces=["event_catalog"],
        )
    with pytest.raises(ControlPlaneError, match="environment_mismatch"):
        control.discover(
            environment="production", scopes=["poll"], collector_id="collector",
            owner="residential_collector", providers=["nba"], surfaces=["event_catalog"],
        )


def test_surface_authorization_denies_cross_collector_and_cross_provider_ingest(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    for kind in ("event", "athlete"):
        request = control.create_bootstrap_request("2025-26", kind, cutoff=cutoff)
        control.publish_catalog(request.request_id, _catalog_payload(kind), version=f"{kind}-v1")
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes=["synergy_play_types"],
        collect_before=now + timedelta(hours=1),
    )
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "synergy_play_types", provider="nba", owner="collector",
        required_observations=["synergy_play_types"], publication_strategy="snapshot_replace",
        supported_windows=["season"], enabled=True,
    )
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    foreign = tokens.create_identity(
        "other", scopes=["ingest"], owner="collector", providers=["nba"],
        surfaces=["grouped_shot_types"],
    )
    foreign_claims = tokens.validate(tokens.issue_for_secret(
        foreign["identity_id"], foreign["secret"], scopes=["ingest"]
    ))
    payload = json.dumps({
        "base": "play_types", "rows": [{"slice_key": "Transition"}],
    }, separators=(",", ":")).encode()
    envelope = {
        "manifest_id": manifest.manifest_id, "client_observation_id": "foreign-1",
        "environment": "testing", "provider": "nba", "observation_type": "synergy_play_types",
        "scope": {"window": "season"}, "season": "2025-26", "cutoff": cutoff.isoformat(),
        "schema_version": 2, "retrieved_at": now.isoformat(),
        "checksum": __import__("hashlib").sha256(payload).hexdigest(),
    }
    ingestion = ObservationIngestionService(
        control_db, publication_service=publication, clock=lambda: now,
    )
    with pytest.raises(ControlPlaneError, match="scope_denied"):
        ingestion.ingest(foreign_claims, envelope, payload)
    provider_mismatch = tokens.create_identity(
        "pbp", scopes=["ingest"], owner="collector", providers=["pbp"],
        surfaces=["synergy_play_types"],
    )
    provider_claims = tokens.validate(tokens.issue_for_secret(
        provider_mismatch["identity_id"], provider_mismatch["secret"], scopes=["ingest"]
    ))
    with pytest.raises(ControlPlaneError, match="scope_denied"):
        ingestion.ingest(provider_claims, {**envelope, "client_observation_id": "provider-1"}, payload)


def test_catalog_validation_rejects_empty_fabricated_and_incomplete_evidence(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    empty = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    with pytest.raises(ControlPlaneError, match="catalog_payload_invalid"):
        control.publish_catalog(empty.request_id, {"events": []}, version="empty")
    malformed = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    with pytest.raises(ControlPlaneError, match="catalog_payload_invalid"):
        control.publish_catalog(malformed.request_id, {
            "events": [{"id": "g1", "status": "Final", "phase": "Regular Season",
                        "scheduled_at": cutoff.isoformat()}],
        }, version="malformed")
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    control.publish_catalog(event_request.request_id, _catalog_payload("event"), version="event-v1")
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    with pytest.raises(ControlPlaneError, match="catalog_payload_invalid"):
        control.publish_catalog(athlete_request.request_id, {
            "identities": [{"player_id": "999", "team_id": sorted(NBA_TEAM_IDS)[0]}],
        }, version="athlete-fabricated")


def test_catalog_bounds_are_configurable_and_manifest_ignores_optional_identity_assertions(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(
        control_db, clock=lambda: now, min_event_catalog_games=15,
        min_event_catalog_teams=30, min_athlete_catalog_identities=1,
    )
    control.activate_season("2025-26", actor="operator")
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    control.publish_catalog(event_request.request_id, _catalog_payload("event"), version="event-v1")
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    control.publish_catalog(athlete_request.request_id, _catalog_payload("athlete"), version="athlete-v1")
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes=["synergy"],
        collect_before=now + timedelta(hours=1), required_athlete_ids=["fabricated-by-caller"],
    )
    assert manifest.status == "active"


def test_lifecycle_alerts_are_deterministic_pending_safe_and_recover(control_db):
    clock = [datetime(2026, 8, 12, tzinfo=UTC)]
    control = CollectionControlService(control_db, clock=lambda: clock[0])
    control.activate_season("2025-26", actor="operator")
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    for kind in ("event", "athlete"):
        request = control.create_bootstrap_request("2025-26", kind, cutoff=cutoff)
        control.publish_catalog(request.request_id, _catalog_payload(kind), version=f"{kind}-v1")
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes=["synergy"],
        collect_before=clock[0] + timedelta(days=1),
    )
    publication = PublicationService(control_db, clock=lambda: clock[0])
    publication.register_stream(
        "synergy_play_types", provider="nba", owner="collector", required_observations=[],
        publication_strategy="replace", supported_windows=["season"], enabled=True,
    )
    cycle = control.open_cycle(manifest.manifest_id)
    operations = CollectionOperationsService(
        control_db, collection_control=control, clock=lambda: clock[0],
    )
    with control_db.begin() as connection:
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="lifecycle-job", stream_key="synergy_play_types", manifest_id=manifest.manifest_id,
            season="2025-26", cutoff=cutoff, status="queued", attempts=0,
            created_at=clock[0], updated_at=clock[0],
        ))
    clock[0] += timedelta(hours=2)
    operations.run_maintenance(season="2025-26", cutoff=cutoff, now=clock[0])
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionAlert)).first() is None
    with control_db.begin() as connection:
        connection.execute(CompositionJob.__table__.update().where(
            CompositionJob.job_id == "lifecycle-job"
        ).values(status="failed", last_error="provider_unavailable", updated_at=clock[0]))
    operations.run_maintenance(season="2025-26", cutoff=cutoff, now=clock[0])
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionAlert).where(CollectionAlert.code == "first_failure")).first() is not None
    with control_db.begin() as connection:
        connection.execute(CompositionJob.__table__.update().where(
            CompositionJob.job_id == "lifecycle-job"
        ).values(status="succeeded", updated_at=clock[0]))
    operations.run_maintenance(season="2025-26", cutoff=cutoff, now=clock[0])
    with control_db.connect() as connection:
        stale = connection.execute(select(CollectionAlert).where(CollectionAlert.code == "stale_threshold")).all()
    assert len(stale) == 1
    clock[0] += timedelta(hours=4)
    operations.run_maintenance(season="2025-26", cutoff=cutoff, now=clock[0])
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionAlert).where(CollectionAlert.code == "cycle_attention")).first() is not None
    publication.compose("synergy_play_types", season="2025-26", cutoff=cutoff, payload={"ok": True}, manifest_id=manifest.manifest_id)
    operations.run_maintenance(season="2025-26", cutoff=cutoff, now=clock[0])
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionAlert).where(CollectionAlert.code == "recovery")).first() is not None
        assert connection.execute(select(CollectionAlert).where(
            CollectionAlert.code == "stale_threshold", CollectionAlert.status == "open"
        )).first() is None
    assert cycle.status == "collecting"


def test_database_identity_lease_contends_and_recovers_after_expiry(control_db):
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now[0])
    identity = tokens.create_identity(
        "pc", scopes=["ingest"], owner="residential_collector", providers=["nba"],
        surfaces=["synergy_play_types"], identity_id="lease-collector",
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    first = ObservationIngestionService(control_db, clock=lambda: now[0])
    second = ObservationIngestionService(control_db, clock=lambda: now[0])
    owner = first._acquire_identity_lease(claims)
    with pytest.raises(ControlPlaneError, match="collector_busy") as error:
        second._acquire_identity_lease(claims)
    assert error.value.retry_after_seconds >= 1
    with control_db.begin() as connection:
        connection.execute(
            CollectorLease.__table__.update().where(
                CollectorLease.collector_id == claims.collector_id
            ).values(lease_expires_at=now[0] - timedelta(seconds=1))
        )
    recovered = second._acquire_identity_lease(claims)
    assert recovered != owner
    second._release_identity_lease(claims, recovered)


def test_stale_lease_cannot_commit_an_observation_after_takeover(control_db):
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now[0])
    identity = tokens.create_identity(
        "pc", scopes=["ingest"], owner="residential_collector", providers=["nba"],
        surfaces=["synergy_play_types"], identity_id="stale-collector",
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    first = ObservationIngestionService(control_db, clock=lambda: now[0])
    grant = first._acquire_identity_lease(claims)
    with control_db.begin() as connection:
        connection.execute(CollectorLease.__table__.update().where(
            CollectorLease.collector_id == claims.collector_id
        ).values(lease_owner="new-worker", fence=grant.fence + 1,
                  lease_expires_at=now[0] + timedelta(seconds=30)))
    payload = json.dumps({"base": "play_types", "rows": [{"slice_key": "Transition"}]}).encode()
    envelope = {
        "manifest_id": "missing", "client_observation_id": "stale-observation",
        "environment": "testing", "provider": "nba", "observation_type": "synergy_play_types",
        "scope": {"window": "season"}, "season": "2025-26",
        "cutoff": now[0].isoformat(), "schema_version": 2,
        "retrieved_at": now[0].isoformat(),
        "checksum": __import__("hashlib").sha256(payload).hexdigest(),
    }
    with control_db.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="missing", season="2025-26", cutoff=now[0],
            collect_before=now[0] + timedelta(hours=1), accepted_versions="[2]",
            scopes="[\"synergy_play_types\"]", checksum="m" * 64,
            status="active", created_at=now[0],
        ))
    with pytest.raises(ControlPlaneError, match="stale_lease"):
        first._ingest(claims, envelope, payload, lease_grant=grant)
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionObservation)).all() == []


def test_usage_rollover_resets_locked_row_and_preserves_429_limits(control_db):
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    operations = CollectionOperationsService(control_db, clock=lambda: now[0])
    first = operations.record_usage("rollover", envelopes=2, bytes_received=20, polls=2)
    assert (first.envelope_count, first.byte_count, first.poll_count) == (2, 20, 2)
    with control_db.begin() as connection:
        connection.execute(
            CollectorUsage.__table__.update().where(CollectorUsage.collector_id == "rollover").values(
                window_started_at=now[0] - timedelta(days=1), envelope_count=999,
                byte_count=999, poll_count=999,
            )
        )
    rolled = operations.record_usage("rollover", envelopes=1, bytes_received=10, polls=1,
                                     max_envelopes=1, max_bytes=10, max_polls=1)
    assert (rolled.envelope_count, rolled.byte_count, rolled.poll_count) == (1, 10, 1)
    with pytest.raises(ControlPlaneError, match="usage_limit"):
        operations.record_usage("rollover", envelopes=1, max_envelopes=1)


def test_diagnostics_reports_bounded_stream_freshness_collector_release_and_usage(control_db):
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    publications = PublicationService(control_db, clock=lambda: now)
    publications.register_stream(
        "fresh-stream", provider="nba", owner="railway", required_observations=[],
        publication_strategy="replace", freshness_rule="daily_recheck", enabled=True,
    )
    publications.register_stream(
        "stale-stream", provider="nba", owner="railway", required_observations=[],
        publication_strategy="replace", freshness_rule="daily_recheck", enabled=True,
    )
    publications.register_stream(
        "missing-stream", provider="nba", owner="railway", required_observations=[],
        publication_strategy="replace", freshness_rule="daily_recheck", enabled=False,
    )
    publications.register_stream(
        "synergy:l15", provider="nba", owner="residential_collector",
        required_observations=["synergy"], publication_strategy="never_schedule",
        freshness_rule="unavailable", enabled=False,
    )
    with pytest.raises(ControlPlaneError, match="stream_unavailable"):
        publications.activate_stream("synergy:l15", reason="must remain unavailable")
    with control_db.begin() as connection:
        for stream_key, created_at, cutoff in (
            ("fresh-stream", now - timedelta(days=1), now - timedelta(hours=2)),
            ("stale-stream", now - timedelta(days=2), now - timedelta(days=2)),
        ):
            publication_id = f"publication-{stream_key}"
            connection.execute(PublicationVersion.__table__.insert().values(
                publication_id=publication_id, stream_key=stream_key, season="2025-26",
                cutoff=cutoff, version=1, status="active", checksum="a" * 64,
                payload="{}", created_at=created_at, reason=None, fence=3,
            ))
            connection.execute(PublicationPointer.__table__.insert().values(
                stream_key=stream_key, active_publication_id=publication_id,
                previous_publication_id=None, fence=3, updated_at=created_at,
            ))
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    created = tokens.create_identity(
        "diagnostic", scopes=["poll"], owner="residential_collector", providers=["nba"],
        surfaces=["event_catalog"], identity_id="diagnostic-collector",
    )
    claims = tokens.validate(tokens.issue_for_secret(
        created["identity_id"], created["secret"], scopes=["poll"]
    ))
    tokens.report_status(claims, release_version="collector-1.2.3", release_checksum="b" * 64)
    old = tokens.create_identity(
        "diagnostic-old", scopes=["poll"], owner="residential_collector", providers=["nba"],
        surfaces=["event_catalog"], identity_id="diagnostic-collector-old",
    )
    old_claims = tokens.validate(tokens.issue_for_secret(
        old["identity_id"], old["secret"], scopes=["poll"]
    ))
    tokens.report_status(
        old_claims, release_version="collector-1.1.0", release_checksum="c" * 64,
    )
    with control_db.begin() as connection:
        connection.execute(CollectorLease.__table__.insert().values(
            collector_id=claims.collector_id, lease_owner="worker-1",
            lease_expires_at=now + timedelta(seconds=30), fence=1, updated_at=now,
        ))
    operations = CollectionOperationsService(control_db, publication_service=publications, clock=lambda: now)
    operations.record_usage("diagnostic-collector", polls=7, envelopes=3, bytes_received=2048)
    diagnostics = operations.diagnostics(limit=50)

    streams = {row["stream_key"]: row for row in diagnostics["streams"]}
    assert streams["fresh-stream"]["freshness_status"] == "fresh"
    assert streams["fresh-stream"]["age_seconds"] == 86400
    assert streams["fresh-stream"]["coverage_cutoff"] == "2026-08-12T10:00:00+00:00"
    assert streams["fresh-stream"]["fence"] == 3
    assert streams["stale-stream"]["freshness_status"] == "stale"
    assert streams["missing-stream"]["freshness_status"] == "missing"
    assert streams["synergy:l15"]["freshness_status"] == "unavailable"
    assert streams["synergy:l15"]["available"] is False

    collector = next(row for row in diagnostics["collectors"] if row["identity_id"] == claims.collector_id)
    assert collector["release_version"] == "collector-1.2.3"
    assert collector["release_checksum"] == "b" * 64
    older = next(row for row in diagnostics["collectors"] if row["identity_id"] == old_claims.collector_id)
    assert (older["release_version"], older["release_checksum"]) == (
        "collector-1.1.0", "c" * 64,
    )
    usage = next(row for row in diagnostics["usage"] if row["collector_id"] == claims.collector_id)
    assert usage["poll_count"] == 7
    assert usage["concurrency_count"] == 1
    assert usage["concurrency_retry_after_seconds"] == 30
    assert usage["limits"] == {
        "poll_count": 100, "envelope_count": 1000,
        "byte_count": 50 * 1024 * 1024, "concurrency_count": 1,
    }
    assert usage["retry_after_seconds"] == 86400


def test_collector_status_rejects_unsafe_release_metadata(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    created = tokens.create_identity(
        "diagnostic", scopes=["poll"], owner="residential_collector", providers=["nba"],
        surfaces=["event_catalog"], identity_id="diagnostic-collector",
    )
    claims = tokens.validate(tokens.issue_for_secret(
        created["identity_id"], created["secret"], scopes=["poll"]
    ))
    with pytest.raises(ControlPlaneError, match="invalid_release_status"):
        tokens.report_status(claims, release_version="../../secret", release_checksum="not-a-checksum")


def test_collector_status_transitions_are_append_only_and_record_recovery(control_db):
    from app.models.collection_control import CollectorStatusTransition

    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now[0])
    identity = tokens.create_identity(
        "transitions", scopes=["poll"], owner="residential_collector",
        providers=["nba"], surfaces=["event_catalog"],
    )
    claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["poll"]))
    tokens.report_status(claims, release_version="collector-1", release_checksum="a" * 64,
                         state="retry", reason="railway_unavailable")
    now[0] += timedelta(minutes=1)
    tokens.report_status(claims, release_version="collector-1", release_checksum="a" * 64,
                         state="complete", reason="complete")
    with control_db.connect() as connection:
        rows = connection.execute(select(CollectorStatusTransition).order_by(
            CollectorStatusTransition.created_at.asc()
        )).all()
    assert [(row.state, row.reason) for row in rows] == [
        ("retry", "railway_unavailable"), ("complete", "recovery"),
    ]


def test_rehearsal_manifest_ingests_durable_replay_without_publication(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    identity = tokens.create_identity(
        "rehearsal", scopes=["poll", "ingest", "catalog_publish"],
        owner="residential_collector", providers=["nba"], surfaces=["event_catalog"],
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"],
        scopes=["poll", "ingest", "catalog_publish"],
    ))
    control = CollectionControlService(control_db, environment="testing", clock=lambda: now)
    manifest = control.create_rehearsal_manifest(
        claims=claims, season="2025-26", cutoff=datetime(2026, 8, 11, tzinfo=UTC),
    )
    tokens.report_status(claims, release_version="collector-1", release_checksum="a" * 64,
                         state="running", reason="rehearsal_started")
    payload = json.dumps({"observations": [{"contract_version": 1, "sanitized": True}]},
                         separators=(",", ":")).encode()
    envelope = {
        "manifest_id": manifest.manifest_id, "client_observation_id": "rehearsal-client",
        "environment": "testing", "provider": "nba", "observation_type": "rehearsal_validation",
        "scope": {"window": "rehearsal"}, "season": "2025-26",
        "cutoff": datetime(2026, 8, 11, tzinfo=UTC).isoformat(), "schema_version": 2,
        "retrieved_at": now.isoformat(),
        "checksum": __import__("hashlib").sha256(payload).hexdigest(),
    }
    ingestion = ObservationIngestionService(control_db, collection_control=control, clock=lambda: now)
    first = ingestion.ingest(claims, envelope, gzip.compress(payload), compressed=True)
    replay = ingestion.ingest(claims, envelope, gzip.compress(payload), compressed=True)
    assert replay.replay and replay.observation_id == first.observation_id
    verified = control.verify_rehearsal_receipt(
        claims=claims, manifest_id=manifest.manifest_id, observation_id=first.observation_id,
        client_observation_id="rehearsal-client", checksum=envelope["checksum"],
    )
    assert verified.observation_type == "rehearsal_validation"
    assert set(control.rehearsal_operations(
        claims=claims, manifest_id=manifest.manifest_id, observation_id=first.observation_id,
    )) == {"credential", "auth", "discovery", "status", "ingestion"}
    with control_db.connect() as connection:
        assert connection.execute(select(PublicationVersion)).all() == []


def test_catalog_completion_requires_regular_governed_schedule_and_roster_evidence(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    event_payload = _catalog_payload("event")
    event_rows = event_payload["events"]
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    event_publication = control.publish_catalog(event_request.request_id, event_payload, version="event-v1")
    assert event_publication.complete is True
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    athlete_publication = control.publish_catalog(
        athlete_request.request_id, _catalog_payload("athlete"), version="athlete-v1"
    )
    assert athlete_publication.complete is True

    partial_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    partial = {"events": event_rows[:-1]}
    partial_publication = control.publish_catalog(partial_request.request_id, partial, version="partial")
    assert partial_publication.complete is False
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes=["synergy"],
        collect_before=now + timedelta(hours=1),
    )
    assert manifest.status == "active"


def test_catalog_publication_rechecks_active_season_before_reconciliation(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    with control_db.connect() as connection:
        before = connection.execute(select(EventCatalogEntry)).all()

    control.activate_season("2026-27", actor="operator")
    with pytest.raises(ControlPlaneError, match="season_not_active"):
        control.publish_catalog(request.request_id, _catalog_payload("event"), version="stale-season")

    with control_db.connect() as connection:
        assert connection.execute(select(EventCatalogEntry)).all() == before
        assert connection.execute(select(CatalogPublication)).all() == []
        assert connection.execute(select(BootstrapRequest.status).where(
            BootstrapRequest.request_id == request.request_id
        )).scalar_one() == "pending"


def test_athlete_catalog_uses_last_good_event_when_newer_event_attempt_is_incomplete(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    assert control.publish_catalog(
        event_request.request_id, _catalog_payload("event"), version="event-complete"
    ).complete

    incomplete_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    incomplete = control.publish_catalog(
        incomplete_request.request_id,
        {"events": _catalog_payload("event")["events"][:-1]},
        version="event-incomplete",
    )
    assert incomplete.complete is False

    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    athlete = control.publish_catalog(
        athlete_request.request_id, _catalog_payload("athlete"), version="athlete-complete"
    )
    assert athlete.complete is True


def test_catalog_publication_reconciles_new_correction_and_tombstone_atomically(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    baseline = _catalog_payload("event")
    request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    assert control.publish_catalog(request.request_id, baseline, version="baseline").complete

    expanded = {"events": [*baseline["events"], {
        "nba_game_id": "game-15", "home_team_id": sorted(NBA_TEAM_IDS)[0],
        "away_team_id": sorted(NBA_TEAM_IDS)[1], "phase": "Regular Season",
        "status": "Final", "scheduled_at": cutoff.isoformat(),
        "athlete_ids": ["1"],
    }]}
    addition = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    added = control.publish_catalog(addition.request_id, expanded, version="expanded")
    assert added.complete is True
    with control_db.connect() as connection:
        assert connection.execute(select(EventCatalogEntry).where(
            EventCatalogEntry.nba_game_id == "game-15"
        )).first() is not None

    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    control.publish_catalog(athlete_request.request_id, _catalog_payload("athlete"), version="athlete")
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes=["synergy"],
        collect_before=now + timedelta(hours=1),
    )
    cycle = control.open_cycle(manifest.manifest_id)

    corrected = json.loads(json.dumps(expanded))
    corrected["events"][0]["status"] = "Scheduled"
    correction = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    assert control.publish_catalog(correction.request_id, corrected, version="correction").complete
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionManifest.status).where(
            CollectionManifest.manifest_id == manifest.manifest_id
        )).scalar_one() == "superseded"
        assert connection.execute(select(CollectionCycle.status).where(
            CollectionCycle.cycle_id == cycle.cycle_id
        )).scalar_one() == "superseded"
    with control_db.connect() as connection:
        row = connection.execute(select(EventCatalogEntry.status_text).where(
            EventCatalogEntry.nba_game_id == "game-0"
        )).scalar_one()
        assert row == "Scheduled"

    incomplete = {"events": corrected["events"][:-1]}
    incomplete_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    assert control.publish_catalog(incomplete_request.request_id, incomplete, version="incomplete").complete is False
    with control_db.connect() as connection:
        assert connection.execute(select(EventCatalogEntry.status_text).where(
            EventCatalogEntry.nba_game_id == "game-15"
        )).scalar_one() == "Final"

    tombstoned = {
        "complete_snapshot": True,
        "tombstones": ["game-15"],
        "events": corrected[:-1] if isinstance(corrected, list) else corrected["events"][:-1],
    }
    tombstone_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    tombstone = control.publish_catalog(tombstone_request.request_id, tombstoned, version="tombstone")
    assert tombstone.complete is True
    with control_db.connect() as connection:
        removed = connection.execute(select(EventCatalogEntry.classification).where(
            EventCatalogEntry.nba_game_id == "game-15"
        )).scalar_one()
        assert removed == "Tombstone"


def test_catalog_reconciliation_is_idempotent_for_roster_changes(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    control.publish_catalog(event_request.request_id, _catalog_payload("event"), version="event")
    roster = _catalog_payload("athlete")
    roster["identities"].append({
        "player_id": "2", "team_id": sorted(NBA_TEAM_IDS)[1], "status": "active",
        "event_ids": [f"game-{index}" for index in range(15)],
    })
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    assert control.publish_catalog(athlete_request.request_id, roster, version="roster").complete
    repeat_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    assert control.publish_catalog(repeat_request.request_id, roster, version="roster-repeat").complete
    incomplete = {"identities": roster["identities"][:1]}
    incomplete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    assert control.publish_catalog(incomplete_request.request_id, incomplete, version="roster-incomplete").complete is False
    with control_db.connect() as connection:
        rows = connection.execute(select(AthleteCatalog).where(
            AthleteCatalog.season == "2025-26", AthleteCatalog.player_id == 2
        )).all()
    assert len(rows) == 1


def test_catalog_rejects_playoffs_and_identity_unresolved_is_reconciled(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    playoffs = _catalog_payload("event")
    playoffs["events"][0]["phase"] = "Playoffs"
    with pytest.raises(ControlPlaneError, match="catalog_payload_invalid"):
        control.publish_catalog(request.request_id, playoffs, version="playoffs")
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=cutoff)
    control.publish_catalog(event_request.request_id, _catalog_payload("event"), version="event")
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=cutoff)
    with pytest.raises(ControlPlaneError, match="identity_unresolved"):
        control.publish_catalog(athlete_request.request_id, {
            "identities": [{"player_id": "missing", "team_id": sorted(NBA_TEAM_IDS)[0],
                             "status": "active", "event_ids": ["game-0"]}],
        }, version="unresolved")
    with control_db.connect() as connection:
        items = connection.execute(select(ReconciliationItem)).all()
    assert len(items) == 1
    assert json.loads(items[0].details)["catalog_type"] == "athlete"


def test_ingestion_identity_unresolved_is_reconciled_before_rejection(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    tokens = CollectorTokenService(control_db, environment="testing", signing_secret="test", clock=lambda: now)
    identity = tokens.create_identity(
        "pc", scopes=["ingest"], owner="residential_collector", providers=["nba"],
        surfaces=["synergy_play_types"], identity_id="unresolved-collector",
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    with control_db.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="unresolved-manifest", season="2025-26", cutoff=now,
            collect_before=now + timedelta(hours=1), accepted_versions="[2]",
            scopes="[\"synergy_play_types\"]", checksum="u" * 64,
            status="active", created_at=now,
        ))
    payload = json.dumps({
        "base": "play_types", "rows": [{"slice_key": "Transition"}],
        "unresolved_identities": ["provider-player-unknown"],
    }).encode()
    envelope = {
        "manifest_id": "unresolved-manifest", "client_observation_id": "unresolved-1",
        "environment": "testing", "provider": "nba", "observation_type": "synergy_play_types",
        "scope": {"window": "season"}, "season": "2025-26", "cutoff": now.isoformat(),
        "schema_version": 2, "retrieved_at": now.isoformat(),
        "checksum": __import__("hashlib").sha256(payload).hexdigest(),
    }
    ingestion = ObservationIngestionService(control_db, collection_control=control, clock=lambda: now)
    with pytest.raises(ControlPlaneError, match="identity_unresolved"):
        ingestion.ingest(claims, envelope, payload)
    with control_db.connect() as connection:
        items = connection.execute(select(ReconciliationItem)).all()
    assert len(items) == 1
    assert json.loads(items[0].details)["client_observation_id"] == "unresolved-1"


def test_publication_provenance_is_normalized_and_gc_protects_active_previous_only(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "provenance", provider="nba", owner="collector", required_observations=["league_obs"],
        publication_strategy="replace", supported_windows=["season"],
        completeness_rule="league_complete", enabled=True,
    )
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="prov-obs-1", client_observation_id="prov-client-1",
            collector_id="collector", manifest_id="prov-manifest", environment="testing",
            provider="nba", observation_type="league_obs", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2, checksum="p" * 64,
            payload=json.dumps({"rows": [{"team_id": team} for team in sorted(NBA_TEAM_IDS)]}),
            payload_bytes=2, retrieved_at=now, accepted_at=now - timedelta(days=31),
        ))
    first = publication.compose("provenance", season="2025-26", cutoff=now,
                                payload={"published": 1}, manifest_id="prov-manifest")
    with control_db.connect() as connection:
        refs = connection.execute(select(PublicationObservation)).all()
    assert [(row.publication_id, row.observation_id) for row in refs] == [(first.publication_id, "prov-obs-1")]
    removed_payload = {"published": 1}
    with control_db.begin() as connection:
        connection.execute(PublicationVersion.__table__.update().where(
            PublicationVersion.publication_id == first.publication_id
        ).values(payload=json.dumps(removed_payload)))
    operations = CollectionOperationsService(control_db, clock=lambda: now)
    assert operations.gc_observations(now=now, retention_days=30) == 0
    second = publication.compose("provenance", season="2025-26", cutoff=now,
                                 payload={"published": 2}, expected_fence=first.fence,
                                 manifest_id="prov-manifest")
    assert second.publication_id != first.publication_id
    assert operations.gc_observations(now=now, retention_days=30) == 0
    third = publication.compose("provenance", season="2025-26", cutoff=now,
                                payload={"published": 3}, expected_fence=second.fence,
                                manifest_id="prov-manifest")
    assert publication.prune_history(stream_key="provenance", season="2025-26") == 1
    assert third.publication_id != second.publication_id
    # The same accepted evidence backs every retained slice, so it remains
    # protected even after the oldest rendered publication is pruned.
    assert operations.gc_observations(now=now, retention_days=30) == 0


def test_rollback_copies_exact_observation_provenance_and_maintenance_prunes_history(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "rollback-provenance", provider="nba", owner="collector",
        required_observations=["league_obs"], publication_strategy="replace",
        supported_windows=["season"], completeness_rule="league_complete", enabled=True,
    )
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="rollback-obs", client_observation_id="rollback-client",
            collector_id="collector", manifest_id="rollback-manifest", environment="testing",
            provider="nba", observation_type="league_obs", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2, checksum="r" * 64,
            payload=json.dumps({"rows": [{"team_id": team} for team in sorted(NBA_TEAM_IDS)]}),
            payload_bytes=2, retrieved_at=now, accepted_at=now - timedelta(days=31),
        ))
    first = publication.compose(
        "rollback-provenance", season="2025-26", cutoff=now,
        payload={"published": "first"}, manifest_id="rollback-manifest",
    )
    second = publication.compose(
        "rollback-provenance", season="2025-26", cutoff=now,
        payload={"published": "second"}, expected_fence=first.fence,
        manifest_id="rollback-manifest",
    )
    assert second.payload == '{"published":"second"}'
    rollback = publication.rollback("rollback-provenance", reason="restore first")
    with control_db.connect() as connection:
        refs = connection.execute(select(PublicationObservation).where(
            PublicationObservation.publication_id == rollback.publication_id
        )).all()
        assert [(row.observation_id, row.role) for row in refs] == [("rollback-obs", "completeness_evidence")]
    operations = CollectionOperationsService(control_db, publication_service=publication, clock=lambda: now)
    result = operations.run_maintenance(season="2025-26", cutoff=now)
    assert result["publications_pruned"] >= 0
    with control_db.connect() as connection:
        assert connection.execute(select(CollectionObservation).where(
            CollectionObservation.observation_id == "rollback-obs"
        )).first() is not None
        assert connection.execute(select(PublicationVersion).where(
            PublicationVersion.publication_id == first.publication_id
        )).first() is None
def test_event_catalog_rejects_caller_game_count_fallback(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    control = CollectionControlService(control_db, clock=lambda: now)
    control.activate_season("2025-26", actor="operator")
    request = control.create_bootstrap_request(
        "2025-26", "event", cutoff=datetime(2026, 8, 11, tzinfo=UTC)
    )
    with pytest.raises(ControlPlaneError, match="catalog_payload_invalid"):
        control.publish_catalog(request.request_id, {"completed_game_count": 999}, version="v1")


def test_league_completeness_rejects_asserted_team_list(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "league-assertion", provider="nba", owner="collector", required_observations=["league_obs"],
        publication_strategy="replace", supported_windows=["season"],
        completeness_rule="league_complete", enabled=True,
    )
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs-assertion", client_observation_id="client-assertion",
            collector_id="collector", manifest_id="manifest-assertion", environment="testing",
            provider="nba", observation_type="league_obs", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2, checksum="d" * 64,
            payload=json.dumps({"team_ids": sorted(NBA_TEAM_IDS)}), payload_bytes=2,
            retrieved_at=now, accepted_at=now,
        ))
    with pytest.raises(ControlPlaneError, match="league_incomplete"):
        publication.compose(
            "league-assertion", season="2025-26", cutoff=now,
            payload={"published": True}, manifest_id="manifest-assertion",
        )


def test_base_completeness_requires_every_registered_slice(control_db):
    now = datetime(2026, 8, 12, tzinfo=UTC)
    publication = PublicationService(control_db, clock=lambda: now)
    publication.register_stream(
        "synergy_play_types", provider="nba", owner="collector",
        required_observations=["synergy_play_types"], publication_strategy="replace",
        supported_windows=["season"], completeness_rule="base_complete", enabled=True,
    )
    rows = [{"slice_key": "Transition"}]
    with control_db.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs-slice", client_observation_id="client-slice",
            collector_id="collector", manifest_id="manifest-slice", environment="testing",
            provider="nba", observation_type="synergy_play_types", scope=json.dumps({"window": "season"}),
            season="2025-26", cutoff=now, schema_version=2, checksum="e" * 64,
            payload=json.dumps({"base": "play_types", "rows": rows}), payload_bytes=2,
            retrieved_at=now, accepted_at=now,
        ))
    with pytest.raises(ControlPlaneError, match="base_incomplete"):
        publication.compose(
            "synergy_play_types", season="2025-26", cutoff=now,
            payload={"published": True}, manifest_id="manifest-slice",
        )
