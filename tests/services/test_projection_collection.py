"""Offline behavioral tests for scheduled projection collection."""

from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update

from app.migrations import run_migrations
from app.models.projection_collection import (
    ProjectionCollectionLease,
    ProjectionCollectionProviderState,
)
from app.providers.dfs import (
    CoverageEvidence,
    NBAMarketQuery,
    ProviderSnapshot,
    SnapshotStatus,
)
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
        self.closing_events = []

    def record_snapshot(self, snapshot, **kwargs):
        self.snapshots.append((snapshot.provider, kwargs))
        return SimpleNamespace(changed=True, snapshot_id="internal", materialization_outcome="advanced")

    def record_failed_poll(self, **kwargs):
        self.failures.append(kwargs)
        return SimpleNamespace(poll_id="internal", outcome="failed")

    def freeze_closing_projection_sets(self, **kwargs):
        self.closing_events.append(kwargs)
        return ()


class BrokenBoard:
    def __init__(self):
        self.calls = []

    def get_board(self, query, *, providers):
        self.calls.append(tuple(providers))
        raise RuntimeError("adapter implementation defect with sensitive detail")


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

    result = coordinator.run(providers=("dabble",))

    assert result.status == "no_work"
    assert board.calls == []

    terminal_board = FakeBoard()
    terminal, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW - timedelta(hours=1), status_text="Final", status_code=3)],
        board=terminal_board,
    )
    assert terminal.run(providers=("dabble",)).status == "no_work"
    assert terminal_board.calls == []

    stale_board = FakeBoard()
    stale, _ = _coordinator(
        tmp_path,
        events=[
            _event(
                scheduled_at=NOW - timedelta(hours=24, seconds=1),
                status_text="Scheduled",
                status_code=1,
            )
        ],
        board=stale_board,
    )
    assert stale.run(providers=("dabble",)).status == "no_work"
    assert stale_board.calls == []


def test_terminal_event_is_closed_by_collector_without_a_provider_poll(tmp_path):
    board = FakeBoard()
    recorder = FakeRecorder()
    started_at = NOW - timedelta(hours=1)
    event = _event(
        scheduled_at=NOW - timedelta(hours=1),
        status_text="Final",
        status_code=3,
    )
    event["first_observed_started_at"] = started_at.isoformat()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[event],
        board=board,
        recorder=recorder,
    )

    result = coordinator.run(providers=("dabble",))

    assert result.status == "no_work"
    assert board.calls == []
    assert recorder.closing_events == [
        {
            "events": (event,),
            "query": NBAMarketQuery(season=SEASON),
            "created_at": NOW,
        }
    ]


def test_collector_closes_a_game_observed_started_during_the_poll(tmp_path):
    event = _event(scheduled_at=NOW + timedelta(hours=1))

    class TransitionBoard(FakeBoard):
        def get_board(self, query, *, providers):
            event.update(
                status_text="Q1",
                status_code=2,
                first_observed_started_at=NOW.isoformat(),
            )
            return super().get_board(query, providers=providers)

    board = TransitionBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble"),
                reason=None,
            )
        }
    )
    recorder = FakeRecorder()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[event],
        board=board,
        recorder=recorder,
    )

    result = coordinator.run(providers=("dabble",))

    assert result.status == "complete"
    assert recorder.closing_events == [
        {
            "events": (event,),
            "query": NBAMarketQuery(season=SEASON),
            "created_at": NOW,
        }
    ]


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


def test_pregame_clock_text_without_status_code_remains_collectible(tmp_path):
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble"),
                reason=None,
            )
        }
    )
    coordinator, _ = _coordinator(
        tmp_path,
        events=[
            _event(
                scheduled_at=NOW + timedelta(hours=1),
                status_text="7:00 pm ET",
                status_code=None,
            )
        ],
        board=board,
    )

    assert coordinator.run(providers=("dabble",)).status == "complete"
    assert board.calls == [("dabble",)]


def test_live_clock_text_without_status_code_stops_collection(tmp_path):
    board = FakeBoard()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[
            _event(
                scheduled_at=NOW - timedelta(hours=1),
                status_text="Q3 5:22",
                status_code=None,
            )
        ],
        board=board,
    )

    assert coordinator.run(providers=("dabble",)).status == "no_work"
    assert board.calls == []


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


def test_fast_window_recomputes_due_time_from_the_last_slow_poll(tmp_path):
    initial = datetime.now(timezone.utc)
    current = [initial]
    scheduled_at = initial + timedelta(hours=2, minutes=15)
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble"),
                reason=None,
            )
        }
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'projection-cadence-transition.sqlite3'}"
    )
    run_migrations(engine)
    coordinator = ProjectionCollectionCoordinator(
        engine,
        board_service=board,
        recording_service=FakeRecorder(),
        event_reader=lambda _season: [_event(scheduled_at=scheduled_at)],
        season=SEASON,
        settings=_settings(),
        clock=lambda: current[0],
        owner="cadence-transition",
    )

    assert coordinator.run(providers=("dabble",)).status == "complete"
    current[0] = initial + timedelta(minutes=15)
    assert coordinator.run(providers=("dabble",)).status == "complete"
    assert board.calls == [("dabble",), ("dabble",)]


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

    before_poll = datetime.now(timezone.utc)
    first = coordinator.run(providers=("dabble", "prizepicks"))
    after_poll = datetime.now(timezone.utc)
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
    backoff_until = state["backoff_until"].replace(tzinfo=timezone.utc)
    assert before_poll + timedelta(minutes=5, seconds=-1) <= backoff_until
    assert backoff_until <= after_poll + timedelta(minutes=5, seconds=1)


def test_failure_backoff_never_retries_faster_than_current_cadence(tmp_path):
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="failed",
                snapshot=None,
                reason="timeout",
            )
        }
    )
    coordinator, engine = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=3))],
        board=board,
        settings=_settings(
            slow_interval=timedelta(minutes=30),
            backoff_base=timedelta(minutes=1),
        ),
    )

    before_poll = datetime.now(timezone.utc)
    assert coordinator.run(providers=("dabble",)).status == "partial"
    after_poll = datetime.now(timezone.utc)

    with engine.connect() as connection:
        state = connection.execute(
            select(ProjectionCollectionProviderState.__table__).where(
                ProjectionCollectionProviderState.__table__.c.provider == "dabble"
            )
        ).mappings().one()
    backoff_until = state["backoff_until"].replace(tzinfo=timezone.utc)
    assert before_poll + timedelta(minutes=30, seconds=-1) <= backoff_until
    assert backoff_until <= after_poll + timedelta(minutes=30, seconds=1)


def test_stale_cache_fallback_records_provider_failure_and_keeps_backoff_open(tmp_path):
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot(
                    "dabble", retrieved_at=NOW - timedelta(minutes=10)
                ),
                reason=None,
                cache_status="stale",
                cache_failure_reason="timeout",
            )
        }
    )
    recorder = FakeRecorder()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=board,
        recorder=recorder,
        settings=_settings(backoff_base=timedelta(minutes=5)),
    )

    before_poll = datetime.now(timezone.utc)
    result = coordinator.run(providers=("dabble",))
    after_poll = datetime.now(timezone.utc)
    diagnostics = coordinator.diagnostics()

    assert result.status == "partial"
    assert recorder.snapshots == []
    assert [failure["failure_reason"] for failure in recorder.failures] == [
        "timeout"
    ]
    failure = diagnostics["providers"][0]["failure"]
    failure_at = datetime.fromisoformat(failure["last_at"])
    assert before_poll - timedelta(seconds=1) <= failure_at
    assert failure_at <= after_poll + timedelta(seconds=1)
    assert failure["reason"] == "timeout"
    assert failure["consecutive"] == 1
    assert diagnostics["providers"][0]["backoff"]["active"] is True


def test_board_defect_records_bounded_failure_for_every_due_provider(tmp_path):
    board = BrokenBoard()
    recorder = FakeRecorder()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=board,
        recorder=recorder,
    )

    result = coordinator.run(providers=("dabble", "prizepicks"))
    diagnostics = coordinator.diagnostics()

    assert result.status == "partial"
    assert result.reason == "provider_collection_failed"
    assert result.providers == ("dabble", "prizepicks")
    assert board.calls == [("dabble", "prizepicks")]
    assert [failure["provider"] for failure in recorder.failures] == [
        "dabble",
        "prizepicks",
    ]
    assert {failure["failure_reason"] for failure in recorder.failures} == {
        "upstream_error"
    }
    assert {
        provider["failure"]["reason"] for provider in diagnostics["providers"]
    } == {"upstream_error"}


def test_missing_board_outcome_records_bounded_provider_failure(tmp_path):
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble"),
                reason=None,
            )
        }
    )
    recorder = FakeRecorder()
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=board,
        recorder=recorder,
    )

    result = coordinator.run(providers=("dabble", "prizepicks"))

    assert result.status == "partial"
    assert [provider for provider, _kwargs in recorder.snapshots] == ["dabble"]
    assert [
        (failure["provider"], failure["failure_reason"])
        for failure in recorder.failures
    ] == [("prizepicks", "missing_outcome")]
    provider_diagnostics = {
        row["provider"]: row for row in coordinator.diagnostics()["providers"]
    }
    assert provider_diagnostics["prizepicks"]["failure"]["reason"] == (
        "missing_outcome"
    )


def test_diagnostics_are_bounded_and_exclude_raw_scope_identifiers(tmp_path):
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=FakeBoard(),
    )
    fence, _ = coordinator._acquire_lease(NOW)
    coordinator._update_state(
        provider="dabble",
        interval=timedelta(minutes=5),
        success=True,
        changed_at=NOW,
        counts=(18, 2),
    )

    diagnostics = coordinator.diagnostics(limit=999)

    assert diagnostics["active_count"] == 18
    assert diagnostics["unresolved_count"] == 2
    assert diagnostics["lease"]["active"] is True
    assert diagnostics["lease"]["fence"] == fence
    assert datetime.fromisoformat(
        diagnostics["lease"]["expires_at"]
    ).tzinfo is not None
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
    start = Barrier(2)
    results = []

    def run(candidate):
        start.wait(timeout=2)
        result = candidate.run(providers=("dabble",))
        results.append(result)
        if result.status == "busy":
            release.set()

    workers = [
        Thread(target=run, args=(candidate,))
        for candidate in (coordinator, second)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert sorted(result.status for result in results) == ["busy", "complete"]
    assert board.calls == [("dabble",)]
    with coordinator.engine.connect() as connection:
        fence = connection.execute(
            select(ProjectionCollectionLease.__table__.c.fence).where(
                ProjectionCollectionLease.__table__.c.lease_key == "projection"
            )
        ).scalar_one()
    assert fence == 1


def test_process_clock_skew_cannot_steal_a_database_live_lease(tmp_path):
    entered = Event()
    release = Event()
    first_board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble"),
                reason=None,
            )
        },
        entered=entered,
        release=release,
    )
    first, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=first_board,
    )
    future = NOW + timedelta(days=3650)
    second_board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble", retrieved_at=future),
                reason=None,
            )
        }
    )
    second = ProjectionCollectionCoordinator(
        first.engine,
        board_service=second_board,
        recording_service=FakeRecorder(),
        event_reader=lambda _season: [
            _event(scheduled_at=future + timedelta(hours=1))
        ],
        season=SEASON,
        settings=_settings(),
        clock=lambda: future,
        owner="future-clock-collector",
    )
    first_result = []
    first_errors = []

    def run_first():
        try:
            first_result.append(first.run(providers=("dabble",)))
        except Exception as error:  # pragma: no cover - assertion evidence
            first_errors.append(error)

    worker = Thread(target=run_first)
    worker.start()
    assert entered.wait(timeout=2)
    second_result = second.run(providers=("dabble",))
    release.set()
    worker.join(timeout=2)

    assert second_result.status == "busy"
    assert second_board.calls == []
    assert first_errors == []
    assert first_result[0].status == "complete"


def test_process_clock_skew_cannot_delay_provider_state_cadence(tmp_path):
    database_time = datetime.now(timezone.utc)
    current = [database_time + timedelta(hours=1)]
    scheduled_at = database_time + timedelta(hours=1)
    board = FakeBoard(
        {
            "dabble": SimpleNamespace(
                provider="dabble",
                status="complete",
                snapshot=_snapshot("dabble", retrieved_at=current[0]),
                reason=None,
            )
        }
    )
    coordinator, _ = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=scheduled_at)],
        board=board,
        now=current[0],
    )
    coordinator.clock = lambda: current[0]

    assert coordinator.run(providers=("dabble",)).status == "complete"
    current[0] = database_time + timedelta(minutes=10)
    assert coordinator.run(providers=("dabble",)).status == "complete"
    assert board.calls == [("dabble",), ("dabble",)]


@pytest.mark.parametrize("lease_change", ("expired", "taken_over"))
def test_expired_or_replaced_lease_returns_bounded_busy_outcome(
    tmp_path,
    lease_change,
):
    class LeaseChangingBoard:
        engine = None

        def get_board(self, query, *, providers):
            with self.engine.begin() as connection:
                values = {
                    "lease_expires_at": datetime.now(timezone.utc)
                    - timedelta(seconds=1),
                }
                if lease_change == "taken_over":
                    values = {
                        "owner": "takeover-collector",
                        "fence": 2,
                        "lease_expires_at": datetime.now(timezone.utc)
                        + timedelta(minutes=1),
                    }
                connection.execute(
                    update(ProjectionCollectionLease.__table__)
                    .where(
                        ProjectionCollectionLease.__table__.c.lease_key
                        == "projection"
                    )
                    .values(**values)
                )
            return SimpleNamespace(
                provider_outcomes=(
                    SimpleNamespace(
                        provider="dabble",
                        status="complete",
                        snapshot=_snapshot("dabble"),
                        reason=None,
                    ),
                )
            )

    board = LeaseChangingBoard()
    recorder = FakeRecorder()
    coordinator, engine = _coordinator(
        tmp_path,
        events=[_event(scheduled_at=NOW + timedelta(hours=1))],
        board=board,
        recorder=recorder,
    )
    board.engine = engine

    result = coordinator.run(providers=("dabble",))

    assert result.status == "busy"
    assert result.reason == "lease_lost"
    assert recorder.snapshots == []
    assert recorder.failures == []
