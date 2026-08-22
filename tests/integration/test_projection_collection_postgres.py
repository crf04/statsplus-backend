"""PostgreSQL lease-fence coverage for projection collection (#106)."""

from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from types import SimpleNamespace
import os

import pytest
from sqlalchemy import create_engine

from app.models import Base
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
    Base.metadata.create_all(engine)
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
        kwargs = {
            "engine": engine,
            "board_service": board,
            "recording_service": _Recorder(),
            "event_reader": lambda season: events,
            "season": "2025-26",
            "providers": ("dabble",),
            "settings": ProjectionCollectionSettings(),
            "clock": lambda: NOW,
        }
        first = ProjectionCollectionCoordinator(owner="postgres-first", **kwargs)
        second = ProjectionCollectionCoordinator(owner="postgres-second", **kwargs)
        results = []
        worker = Thread(target=lambda: results.append(first.run()))
        worker.start()
        assert entered.wait(timeout=5)
        assert second.run().status == "busy"
        release.set()
        worker.join(timeout=5)
        assert results[0].status == "complete"
        assert board.calls == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
