"""PostgreSQL lease-fence coverage for projection collection (#106)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from types import SimpleNamespace
import os

import pytest
from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models import Base
from app.models.projection_collection import ProjectionCollectionLease
from app.providers.dfs import CoverageEvidence, ProviderSnapshot, SnapshotStatus
from app.services.projection_collection import (
    ProjectionCollectionCoordinator,
    ProjectionCollectionSettings,
)


pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc)


class _Board:
    def __init__(self, entered: Event, release: Event):
        self.entered = entered
        self.release = release
        self.calls = 0

    def get_board(self, query, *, providers):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        snapshot = ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(expected_total=0),
            retrieved_at=NOW,
        )
        return SimpleNamespace(
            provider_outcomes=(
                SimpleNamespace(provider="dabble", status="complete", snapshot=snapshot, reason=None),
            )
        )


class _Recorder:
    def record_snapshot(self, snapshot, **kwargs):
        return SimpleNamespace(changed=True)

    def record_failed_poll(self, **kwargs):
        return SimpleNamespace(outcome="failed")


def test_postgres_projection_lease_has_one_winner():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TABLE projection_collection_provider_states"
        )
        connection.exec_driver_sql("DROP TABLE projection_collection_leases")
        connection.exec_driver_sql(
            "ALTER TABLE projection_provider_polls DROP COLUMN duration_ms"
        )
        connection.exec_driver_sql(
            "DELETE FROM schema_migrations WHERE version = 43"
        )
    upgraded = run_migrations(engine)
    assert upgraded.applied == ("043_projection_collection_control",)
    try:
        entered = Event()
        release = Event()
        board = _Board(entered, release)
        events = [
            {
                "nba_game_id": "0022500001",
                "scheduled_at": NOW + timedelta(hours=1),
                "status_text": "Scheduled",
                "status_code": 1,
            }
        ]
        start = Barrier(2)
        database_url = engine.url.render_as_string(hide_password=False)

        def collect(owner):
            worker_engine = create_engine(database_url)
            try:
                coordinator = ProjectionCollectionCoordinator(
                    worker_engine,
                    board_service=board,
                    recording_service=_Recorder(),
                    event_reader=lambda _season: events,
                    season="2025-26",
                    providers=("dabble",),
                    settings=ProjectionCollectionSettings(),
                    clock=lambda: NOW,
                    owner=owner,
                )
                start.wait(timeout=5)
                result = coordinator.run()
                if result.status == "busy":
                    release.set()
                return result
            finally:
                worker_engine.dispose()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(collect, ("postgres-first", "postgres-second"))
            )

        assert sorted(result.status for result in results) == ["busy", "complete"]
        assert board.calls == 1
        with engine.connect() as connection:
            fence = connection.execute(
                select(ProjectionCollectionLease.fence).where(
                    ProjectionCollectionLease.lease_key == "projection"
                )
            ).scalar_one()
        assert fence == 1
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")
        engine.dispose()
