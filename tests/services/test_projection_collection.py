"""Offline behavioral tests for scheduled projection collection."""

from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from types import SimpleNamespace

from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.projection_collection import ProjectionCollectionProviderState
from app.providers.dfs import CoverageEvidence, ProviderSnapshot, SnapshotStatus
from app.services.projection_collection import (
    ProjectionCollectionCoordinator,
    ProjectionCollectionSettings,
)


SEASON = "2025-26"
NOW = datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc)


def _settings(**overrides):
    values = {
        "slow_interval": timedelta(minutes=30),
        "fast_interval": timedelta(minutes=5),
        "pregame_horizon": timedelta(hours=24),
        "fast_window": timedelta(hours=2),
        "lease_duration": timedelta(minutes=1),
        "backoff_base": timedelta(minutes=1),
        "backoff_max": timedelta(hours=1),
    }
    values.update(overrides)
    return ProjectionCollectionSettings(**values)


def _event(*, scheduled_at, status_text="Scheduled", status_code=1, postponed_status=None):
    return {
        "nba_game_id": "0022500001",
        "scheduled_at": scheduled_at,
        "status_text": status_text,
        "status_code": status_code,
        "postponed_status": postponed_status,
        "postponement_evidence": None,
    }


def _snapshot(provider: str, retrieved_at: datetime = NOW):
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=(),
        coverage=CoverageEvidence(expected_total=0),
        retrieved_at=retrieved_at,
    )


class FakeBoard:
    def __init__(self, outcomes_by_provider=None, *, entered=None, release=None):
        self.outcomes_by_provider = outcomes_by_provider or {}
        self.entered = entered
        self.release = release
        self.calls = []

    def get_board(self, query, *, providers):
        self.calls.append(tuple(providers))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=2)
        return SimpleNamespace(
            provider_outcomes=tuple(
                self.outcomes_by_provider[name]
                for name in sorted(providers)
                if name in self.outcomes_by_provider
            )
        )


class FakeRecorder:
    def __init__(self):
        self.snapshots = []
        self.failures = []

    def record_snapshot(self, snapshot, **kwargs):
        self.snapshots.append((snapshot.provider, kwargs))
        return SimpleNamespace(changed=True, snapshot_id="internal", materialization_outcome="advanced")

    def record_failed_poll(self, **kwargs):
        self.failures.append(kwargs)
        return SimpleNamespace(poll_id="internal", outcome="failed")


def _coordinator(tmp_path, *, events, board, recorder=None, now=NOW, settings=None):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-collection.sqlite3'}")
    run_migrations(engine)
    return (
        ProjectionCollectionCoordinator(
            engine,
            board_service=board,
            recording_service=recorder or FakeRecorder(),
            event_reader=lambda season: events,
            season=SEASON,
            settings=settings or _settings(),
            clock=lambda: now,
            owner="test-collector",
        ),
        engine,
    )


def test_no_work_never_calls_a_provider_for_offseason_or_terminal_events(tmp_path):
    board = FakeBoard()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(days=3))],
        board=board,
    )

    result = coordinator.run()

    assert result.status == "no_work"
    assert board.calls == []

    terminal_board = FakeBoard()
    terminal, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW - timedelta(hours=1), status_text="Final", status_code=3)],
        board=terminal_board,
    )
    assert terminal.run().status == "no_work"
    assert terminal_board.calls == []


def test_past_scheduled_time_does_not_stop_polling_until_governed_status_starts(tmp_path):
    board = FakeBoard(
        {"dabble": SimpleNamespace(provider="dabble", status="complete", snapshot=_snapshot("dabble"), reason=None)}
    )
    recorder = FakeRecorder()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW - timedelta(minutes=15), status_text="Scheduled", status_code=1)],
        board=board,
        recorder=recorder,
    )

    result = coordinator.run(providers=("dabble",))

    assert result.status == "complete"
    assert board.calls == [("dabble",)]
    assert [provider for provider, _ in recorder.snapshots] == ["dabble"]


def test_adaptive_policy_switches_from_slow_to_fast_board_wide(tmp_path):
    board = FakeBoard()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=23))],
        board=board,
    )

    slow = coordinator._schedule(NOW, ("dabble", "prizepicks"))
    fast = coordinator._schedule(NOW + timedelta(hours=21), ("dabble", "prizepicks"))

    assert slow is not None
    assert slow.interval == timedelta(minutes=30)
    assert slow.providers == ("dabble", "prizepicks")
    assert fast is not None
    assert fast.interval == timedelta(minutes=5)
    assert fast.providers == ("dabble", "prizepicks")
    assert board.calls == []


def test_provider_failure_is_recorded_independently_and_enters_bounded_backoff(tmp_path):
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(provider="dabble", status="complete", snapshot=_snapshot("dabble"), reason=None),
            "prizepicks": SimpleNamespace(provider="prizepicks", status="failed", snapshot=None, reason="timeout"),
        }
    )
    recorder = FakeRecorder()
    coordinator, engine = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=board,
        recorder=recorder,
        settings=_settings(fast_interval=timedelta(seconds=1), backoff_base=timedelta(minutes=5)),
    )

    first = coordinator.run(providers=("dabble", "prizepicks"))
    second = coordinator.run(providers=("dabble", "prizepicks"))

    assert first.status == "partial"
    assert second.status == "no_work"
    assert board.calls == [("dabble", "prizepicks")]
    assert [provider for provider, _ in recorder.snapshots] == ["dabble"]
    assert [item["provider"] for item in recorder.failures] == ["prizepicks"]
    with engine.connect() as connection:
        state = connection.execute(
            select(ProjectionCollectionProviderState.__table__).where(
                ProjectionCollectionProviderState.__table__.c.provider == "prizepicks"
            )
        ).mappings().one()
    assert state["consecutive_failures"] == 1
    assert state["backoff_until"].replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=5)


def test_diagnostics_are_bounded_and_exclude_raw_scope_identifiers(tmp_path):
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=FakeBoard(),
    )
    fence, _ = coordinator._acquire_lease(NOW)
    coordinator._update_state(
        provider="dabble",
        now=NOW,
        interval=timedelta(minutes=5),
        success=True,
        changed_at=NOW,
        counts=(18, 2),
    )

    diagnostics = coordinator.diagnostics(limit=999)

    assert diagnostics["active_count"] == 18
    assert diagnostics["unresolved_count"] == 2
    assert diagnostics["lease"] == {
        "active": True,
        "fence": fence,
        "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
    }
    provider = diagnostics["providers"][0]
    assert provider["provider"] == "dabble"
    assert provider["last_changed_snapshot_at"] == NOW.isoformat()
    assert provider["active_count"] == 18
    assert provider["unresolved_count"] == 2
    assert "query_key" not in provider
    assert "raw" not in str(diagnostics).casefold()
    coordinator._release_lease(NOW, fence)


def test_overlapping_runs_have_one_database_lease_winner(tmp_path):
    entered = Event()
    release = Event()
    board = FakeBoard(
        {"dabble": SimpleNamespace(provider="dabble", status="complete", snapshot=_snapshot("dabble"), reason=None)},
        entered=entered,
        release=release,
    )
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=board,
    )
    second = ProjectionCollectionCoordinator(
        coordinator.engine,
        board_service=board,
        recording_service=FakeRecorder(),
        event_reader=lambda season: [_event(scheduled_at=NOW + timedelta(hours=1))],
        season=SEASON,
        settings=_settings(),
        clock=lambda: NOW,
        owner="second-collector",
    )
    first_result = []
    worker = Thread(target=lambda: first_result.append(coordinator.run(providers=("dabble",))))
    worker.start()
    assert entered.wait(timeout=2)
    assert second.run(providers=("dabble",)).status == "busy"
    release.set()
    worker.join(timeout=2)
    assert first_result[0].status == "complete"
    assert board.calls == [("dabble",)]
