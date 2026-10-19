"""Concurrent lifecycle evaluations against real Postgres row locks (#480)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.models.collection_control import CollectionCycle
from app.services.collection_control import CollectionOperationsService
from tests.services.test_collection_control import (
    LEDGER_MANIFEST_PUBLISHING_STREAMS,
    RecordingAlertAdapter,
    _alerts,
    _cycle_state,
    _ledger_cycle,
    _point_at_active_publication,
    _seed_governed_catalog_evidence,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=8)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    _seed_governed_catalog_evidence(engine)
    yield engine
    engine.dispose()


class _SupersedesBeforeRecovery(CollectionOperationsService):
    """Another session supersedes the cycle after this evaluation has read it."""

    def _recover_lifecycle_alerts(self, session, *, cycle_id, now):
        with self.engine.begin() as other:
            other.execute(
                CollectionCycle.__table__.update()
                .where(CollectionCycle.cycle_id == cycle_id)
                .values(status="superseded", superseded_at=now)
            )
        return super()._recover_lifecycle_alerts(session, cycle_id=cycle_id, now=now)


def test_recovery_does_not_overwrite_a_cycle_superseded_by_another_session(engine):
    clock = [datetime(2026, 8, 12, tzinfo=UTC)]
    operations, cycle, manifest, cutoff = _ledger_cycle(engine, clock)
    clock[0] += timedelta(hours=7)
    operations.evaluate_cycle_alerts()
    assert _cycle_state(engine, cycle) == ("attention", "cycle_window_expired")
    for stream_key in LEDGER_MANIFEST_PUBLISHING_STREAMS:
        _point_at_active_publication(engine, stream_key, manifest=manifest, cutoff=cutoff, at=clock[0])

    racing = _SupersedesBeforeRecovery(
        engine, collection_control=operations.collection_control, clock=lambda: clock[0]
    )
    result = racing.evaluate_cycle_alerts()

    assert result["cycles_completed"] == 0
    assert _cycle_state(engine, cycle) == ("superseded", "cycle_window_expired")


class _HoldsFirstAttentionSend(RecordingAlertAdapter):
    """Blocks the first cycle_attention send until released, like a slow SMTP call."""

    def __init__(self):
        super().__init__()
        self.in_send = threading.Event()
        self.release = threading.Event()
        self._blocked = False

    def send(self, *, code, severity, cycle_id=None):
        if code == "cycle_attention" and not self._blocked:
            self._blocked = True
            self.in_send.set()
            assert self.release.wait(30)
        super().send(code=code, severity=severity, cycle_id=cycle_id)


def test_overlapping_evaluations_deliver_each_notification_once(engine):
    clock = [datetime(2026, 8, 12, tzinfo=UTC)]
    adapter = _HoldsFirstAttentionSend()
    operations, cycle, _, _ = _ledger_cycle(engine, clock, adapter)
    clock[0] += timedelta(hours=7)
    # Open both alerts with delivery failing, so both stay owed.
    failing = RecordingAlertAdapter(fail_on={1, 2})
    operations.alert_adapter = failing
    with pytest.raises(RuntimeError):
        operations.evaluate_cycle_alerts()
    assert failing.sent == []
    operations.alert_adapter = adapter

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(operations.evaluate_cycle_alerts)
        assert adapter.in_send.wait(30)
        # The first run holds the claim on cycle_attention mid-send and has
        # already listed stale_threshold as owed.  The second run must skip the
        # claimed alert, deliver stale_threshold, and the first run must then
        # find that one already notified instead of sending it again.
        second = pool.submit(operations.evaluate_cycle_alerts)
        second.result(timeout=30)
        adapter.release.set()
        first.result(timeout=30)

    assert sorted(adapter.sent) == ["cycle_attention", "stale_threshold"]
    assert _alerts(engine, cycle) == [("cycle_attention", "open"), ("stale_threshold", "open")]
