from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.collection_control import (
    ActiveSeason, AuditEvent, BootstrapRequest, CollectionManifest, CompositionJob,
    PublicationRepairGroup, PublicationStream,
)
from app.services.collection_control import CollectionControlService, PublicationService
from app.services.player_shooting_refresh import PlayerShootingRefresh, daily_window
from tests.services.test_collection_control import _catalog_payload, _seed_governed_catalog_evidence

UTC = timezone.utc
NOW = datetime(2026, 8, 12, 9, tzinfo=UTC)
SEASON = "2025-26"


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'daily.sqlite3'}")
    run_migrations(engine)
    _seed_governed_catalog_evidence(engine, now=NOW)
    control = CollectionControlService(engine, clock=lambda: NOW)
    control.activate_season(SEASON, actor="test")
    PublicationService(engine, clock=lambda: NOW).register_default_streams()
    with engine.begin() as connection:
        connection.execute(PublicationStream.__table__.update().where(
            PublicationStream.stream_key == "grouped_shot_types",
        ).values(enabled=True))
    factory = Mock(side_effect=AssertionError("idle tick constructed provider graph"))
    service = PlayerShootingRefresh(engine, composer_factory=factory, clock=lambda: NOW)
    return engine, control, service, factory


def _publish_requested(engine, control, kind):
    with Session(engine) as session:
        request = session.scalar(select(BootstrapRequest).where(
            BootstrapRequest.catalog_type == kind, BootstrapRequest.status == "pending",
        ))
        request_id = request.request_id
    control.publish_catalog(request_id, _catalog_payload(kind), version="test")


def _manifest(engine, *, cutoff=NOW - timedelta(days=1), deadline=NOW + timedelta(hours=1), scopes=None):
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="prior", season=SEASON, cutoff=cutoff, collect_before=deadline,
            scopes=json.dumps(scopes or ["grouped_shot_types", "canonical_game_ledger"]),
            accepted_versions="[1,2]", checksum="prior", status="active", created_at=cutoff,
        ))


@pytest.mark.parametrize("field,state", [("stream", "disabled"), ("season", "season_inactive")])
def test_disabled_or_inactive_authorizes_nothing(setup, field, state):
    engine, _, service, factory = setup
    with engine.begin() as connection:
        if field == "stream":
            connection.execute(PublicationStream.__table__.update().values(enabled=False))
        else:
            connection.execute(ActiveSeason.__table__.update().values(status="inactive"))
    assert service.tick(SEASON)["state"] == state
    with Session(engine) as session:
        assert session.scalars(select(BootstrapRequest)).all() == []
    factory.assert_not_called()


@pytest.mark.parametrize("date,start,end", [
    ((2026, 1, 12), 9, 17), ((2026, 8, 12), 8, 16),
    ((2026, 3, 8), 8, 16), ((2026, 11, 1), 9, 17),
])
def test_window_uses_central_time_including_dst_transitions(date, start, end):
    cutoff, deadline = daily_window(datetime(*date, 12, tzinfo=UTC))
    assert cutoff == datetime(*date, start, 45, tzinfo=UTC)
    assert deadline == datetime(*date, end, tzinfo=UTC)


@pytest.mark.parametrize("now", [NOW.replace(hour=8, minute=44), NOW.replace(hour=16)])
def test_outside_window_creates_no_bootstrap(setup, now):
    engine, _, service, factory = setup
    service.clock = lambda: now
    assert service.tick(SEASON)["state"] == "outside_window"
    with Session(engine) as session:
        assert session.scalars(select(BootstrapRequest)).all() == []
    factory.assert_not_called()


def test_catalog_chain_is_deduplicated_and_manifest_preserves_sibling_scopes(setup):
    engine, control, service, factory = setup
    _manifest(engine, deadline=NOW - timedelta(minutes=1))
    assert service.tick(SEASON)["state"] == "awaiting_event"
    assert service.tick(SEASON)["state"] == "awaiting_event"
    with Session(engine) as session:
        assert len(session.scalars(select(BootstrapRequest)).all()) == 1
        assert session.get(CollectionManifest, "prior").status == "active"
    _publish_requested(engine, control, "event")
    assert service.tick(SEASON)["state"] == "awaiting_athlete"
    assert service.tick(SEASON)["state"] == "awaiting_athlete"
    _publish_requested(engine, control, "athlete")
    assert service.tick(SEASON)["state"] == "manifest_ready"
    assert service.tick(SEASON)["state"] == "manifest_ready"
    with Session(engine) as session:
        manifests = session.scalars(select(CollectionManifest)).all()
        assert len(manifests) == 2
        current = next(row for row in manifests if row.status == "active")
        assert set(json.loads(current.scopes)) == {"grouped_shot_types", "canonical_game_ledger"}
        assert current.event_catalog_publication_id
        assert current.collect_before == datetime(2026, 8, 12, 16)
        assert session.get(ActiveSeason, SEASON).cutoff is None
        assert len(session.scalars(select(BootstrapRequest)).all()) == 2
        audits = session.scalars(select(AuditEvent)).all()
        assert len(audits) == 3
        assert all(row.actor == "player_shooting_refresh" and row.reason for row in audits)
    factory.assert_not_called()


@pytest.mark.parametrize("kind,state", [("newer", "newer_authority"), ("pending", "collection_pending"), ("repair", "repair_pending")])
def test_existing_authority_is_not_replaced(setup, kind, state):
    engine, _, service, _ = setup
    _manifest(engine, cutoff=NOW + timedelta(days=1) if kind == "newer" else NOW - timedelta(days=1))
    if kind == "repair":
        with engine.begin() as connection:
            connection.execute(PublicationRepairGroup.__table__.insert().values(
                group_id="repair", manifest_id="prior", season=SEASON, cutoff=NOW,
                reason="operator repair", checksum="repair", created_at=NOW,
            ))
    assert service.tick(SEASON)["state"] == state
    with Session(engine) as session:
        assert session.get(CollectionManifest, "prior").status == "active"
        assert session.scalars(select(BootstrapRequest)).all() == []


def test_accepted_jobs_compose_outside_window_without_provider_refresh(setup):
    engine, _, service, _ = setup
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="accepted", season=SEASON, stream_key="grouped_shot_types",
            cutoff=NOW, status="queued", generation=1, attempts=0,
            created_at=NOW, updated_at=NOW,
        ))
    runtime = Mock(spec=["compose_queued"])
    runtime.compose_queued.return_value = 1
    service.composer_factory = Mock(return_value=runtime)
    service.clock = lambda: NOW.replace(hour=18)
    assert service.tick(SEASON) == {"state": "outside_window", "composed_jobs": 1}
    runtime.compose_queued.assert_called_once_with(SEASON)


def test_request_and_audit_rollback_together_on_failure(setup, monkeypatch):
    engine, _, service, _ = setup
    def fail(*args):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(service, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.tick(SEASON)
    with Session(engine) as session:
        assert session.scalars(select(BootstrapRequest)).all() == []
        assert session.scalars(select(AuditEvent)).all() == []


def test_newer_active_season_cutoff_does_not_get_rewritten(setup):
    engine, _, service, _ = setup
    future = NOW + timedelta(days=1)
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.update().values(cutoff=future))
    assert service.tick(SEASON)["state"] == "newer_authority"
    with Session(engine) as session:
        assert session.get(ActiveSeason, SEASON).cutoff == future.replace(tzinfo=None)
        assert session.scalars(select(BootstrapRequest)).all() == []


def test_expired_request_is_replaced_once_with_deadline_bound_retry(setup):
    engine, _, service, _ = setup
    assert service.tick(SEASON)["state"] == "awaiting_event"
    with engine.begin() as connection:
        connection.execute(BootstrapRequest.__table__.update().values(expires_at=NOW - timedelta(minutes=1)))
    assert service.tick(SEASON)["state"] == "awaiting_event"
    assert service.tick(SEASON)["state"] == "awaiting_event"
    with Session(engine) as session:
        requests = session.scalars(select(BootstrapRequest)).all()
        assert len(requests) == 2
        assert max(row.expires_at for row in requests) == datetime(2026, 8, 12, 16)


def test_completed_prior_work_can_advance_without_touching_last_good_publication(setup):
    from app.models.collection_control import PublicationPointer, PublicationVersion
    from tests.services.test_collection_control import _seed_active_publication
    engine, _, service, _ = setup
    _manifest(engine, scopes=["grouped_shot_types"])
    _seed_active_publication(engine, stream_key="grouped_shot_types", cutoff=NOW - timedelta(days=1))
    with Session(engine) as session:
        before = [(row.publication_id, row.payload, row.status) for row in session.scalars(select(PublicationVersion))]
        pointer = session.get(PublicationPointer, "grouped_shot_types").active_publication_id
    assert service.tick(SEASON)["state"] == "awaiting_event"
    with Session(engine) as session:
        assert [(row.publication_id, row.payload, row.status) for row in session.scalars(select(PublicationVersion))] == before
        assert session.get(PublicationPointer, "grouped_shot_types").active_publication_id == pointer
