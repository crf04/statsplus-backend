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


# --- independent player-shooting streams (#267) ----------------------------


def _enable(engine, *stream_keys):
    with engine.begin() as connection:
        connection.execute(PublicationStream.__table__.update().values(enabled=False))
        connection.execute(PublicationStream.__table__.update().where(
            PublicationStream.stream_key.in_(stream_keys),
        ).values(enabled=True))


def _ready_manifest(engine, control, service, *, scopes=None):
    """Drive the catalog chain until this tick issues its own manifest."""

    _manifest(engine, deadline=NOW - timedelta(minutes=1), scopes=scopes)
    assert service.tick(SEASON)["state"] == "awaiting_event"
    _publish_requested(engine, control, "event")
    assert service.tick(SEASON)["state"] == "awaiting_athlete"
    _publish_requested(engine, control, "athlete")
    assert service.tick(SEASON)["state"] == "manifest_ready"
    with Session(engine) as session:
        current = next(
            row for row in session.scalars(select(CollectionManifest)).all()
            if row.status == "active" and row.manifest_id != "prior"
        )
        return set(json.loads(current.scopes))


def test_both_player_shooting_streams_are_authorized_by_one_manifest(setup):
    engine, control, service, _ = setup
    _enable(engine, "grouped_shot_types", "exact_shot_zones")
    assert {"grouped_shot_types", "exact_shot_zones"} <= _ready_manifest(
        engine, control, service,
    )


def test_the_zone_stream_is_scheduled_without_its_sibling(setup):
    """Each stream is scheduled on its own enabled flag."""

    engine, control, service, _ = setup
    _enable(engine, "exact_shot_zones")
    scopes = _ready_manifest(engine, control, service, scopes=["exact_shot_zones"])
    assert "exact_shot_zones" in scopes
    assert "grouped_shot_types" not in scopes


def test_a_manifest_covering_only_the_sibling_is_not_authority_for_a_newly_enabled_stream(
    setup,
):
    """Enabling zones mid-window waits for the open manifest, then covers both.

    The open manifest is a shared completeness fence.  A stream enabled after
    it was issued is not already authorized by it, but that is not a licence to
    issue an overlapping manifest while its collection window is still running.
    """

    engine, control, service, _ = setup
    _enable(engine, "grouped_shot_types")
    _ready_manifest(engine, control, service)
    with Session(engine) as session:
        before = {row.manifest_id for row in session.scalars(select(CollectionManifest))}

    _enable(engine, "grouped_shot_types", "exact_shot_zones")
    # The sibling's manifest does not read as ready for the new stream, and
    # its own window is still open, so nothing overlapping is issued.
    assert service.tick(SEASON)["state"] == "collection_pending"
    with Session(engine) as session:
        assert {
            row.manifest_id for row in session.scalars(select(CollectionManifest))
        } == before

    # Once that window closes, the next manifest covers both streams.
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.update().where(
            CollectionManifest.manifest_id.in_(before),
        ).values(collect_before=NOW - timedelta(minutes=1)))
    assert service.tick(SEASON)["state"] == "manifest_ready"
    with Session(engine) as session:
        issued = [
            row for row in session.scalars(select(CollectionManifest))
            if row.manifest_id not in before
        ]
    assert len(issued) == 1
    assert {"grouped_shot_types", "exact_shot_zones"} <= set(json.loads(issued[0].scopes))


def test_a_disabled_zone_stream_leaves_shot_type_scheduling_unchanged(setup):
    """The preactivation behaviour of the sibling stream is untouched."""

    engine, control, service, _ = setup
    _enable(engine, "grouped_shot_types")
    assert _ready_manifest(engine, control, service) == {
        "grouped_shot_types", "canonical_game_ledger",
    }


def test_ready_sibling_work_still_composes_when_the_other_stream_fails(setup):
    """One stream's composition failure must not strand the other's job."""

    engine, _, _, _ = setup
    _enable(engine, "grouped_shot_types", "exact_shot_zones")
    for stream_key in ("grouped_shot_types", "exact_shot_zones"):
        with engine.begin() as connection:
            connection.execute(CompositionJob.__table__.insert().values(
                job_id=f"job-{stream_key}", season=SEASON, stream_key=stream_key,
                cutoff=NOW, manifest_id="prior", status="queued", generation=1,
                attempts=0, created_at=NOW, updated_at=NOW,
            ))

    from app.services.collection_control import ControlPlaneError
    from app.services.ledger_runtime import LedgerGovernance, LedgerRuntime
    from app.services.canonical_game_ledger import CanonicalGameLedgerRepository

    composed = []
    publications = PublicationService(engine, clock=lambda: NOW)

    def compose_from_observations(stream_key, **kwargs):
        if stream_key == "exact_shot_zones":
            raise ControlPlaneError("incomplete_publication")
        composed.append(stream_key)

    publications.compose_from_observations = compose_from_observations

    class Governance:
        def read_for_composition(self, season, cutoff, manifest_id=None):
            return LedgerGovernance(season, cutoff, frozenset(), frozenset(), {})

    runtime = LedgerRuntime(
        backfill=None, repository=CanonicalGameLedgerRepository(engine),
        materialization=None, governance=Governance(),
        publication_service=publications, clock=lambda: NOW,
    )
    runtime.compose_queued(SEASON)

    assert composed == ["grouped_shot_types"]
    with Session(engine) as session:
        statuses = {
            row.stream_key: row.status
            for row in session.scalars(select(CompositionJob))
        }
    assert statuses["grouped_shot_types"] == "succeeded"
    assert statuses["exact_shot_zones"] != "succeeded"


def test_a_stream_disabled_since_the_prior_manifest_is_not_copied_forward(setup):
    """History must not keep a turned-off stream collectible.

    A new manifest preserves sibling scopes it does not manage, but membership
    of the player-shooting streams follows the currently enabled set.  Copying
    ``exact_shot_zones`` forward from yesterday's manifest would leave it
    discoverable and executable by the collector after it was disabled --
    ``_manifest_streams`` reads the manifest, not the enabled flag.
    """

    engine, control, service, _ = setup
    _enable(engine, "grouped_shot_types")
    scopes = _ready_manifest(engine, control, service, scopes=[
        "grouped_shot_types", "exact_shot_zones", "canonical_game_ledger",
    ])
    assert "exact_shot_zones" not in scopes
    # The unrelated sibling scope this tick does not manage is still preserved.
    assert {"grouped_shot_types", "canonical_game_ledger"} <= scopes


def test_a_re_enabled_stream_is_restored_to_the_next_manifest(setup):
    """The inverse: the enabled set is what decides, in both directions."""

    engine, control, service, _ = setup
    _enable(engine, "grouped_shot_types", "exact_shot_zones")
    scopes = _ready_manifest(engine, control, service, scopes=[
        "grouped_shot_types", "canonical_game_ledger",
    ])
    assert {"grouped_shot_types", "exact_shot_zones", "canonical_game_ledger"} <= scopes
