"""The projection collector's DFS board, through the wired dependency graph.

The request-time board answers inside fifteen seconds.  The background
collector is not a request: a provider that is merely slower than that, or
that would wait behind other providers, must still be recorded.
"""

from datetime import UTC, datetime, timedelta
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select

from app.config.settings import load_settings
from app.dependencies import build_dependencies
from app.migrations import run_migrations
from app.models.projection_archive import ProviderPoll
from app.models.projection_collection import ProjectionCollectionProviderState
from app.providers.dfs import CoverageEvidence, NBAMarketQuery, ProviderSnapshot, SnapshotStatus
from app.providers.registry import dfs_provider_names
from app.services.dfs_board import ProviderFailureReason, ProviderOutcomeStatus


def _snapshot(provider):
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=(),
        coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
        retrieved_at=datetime.now(UTC),
    )


class _SlowAdapter:
    """Answers ``seconds`` later on the boards' monotonic clock."""

    def __init__(self, name, clock, seconds):
        self.name = name
        self.clock = clock
        self.seconds = seconds

    def get_snapshot(self, query, context):
        self.clock.value += self.seconds
        return _snapshot(self.name)


class _RendezvousAdapter:
    """Answers only once every provider of the board is in flight."""

    def __init__(self, name, barrier):
        self.name = name
        self.barrier = barrier

    def get_snapshot(self, query, context):
        try:
            self.barrier.wait()
        except BrokenBarrierError as error:
            raise TimeoutError("a provider was queued behind the others") from error
        return _snapshot(self.name)


def _wired(tmp_path, monkeypatch, adapters, **environ):
    engine = create_engine(f"sqlite:///{tmp_path / 'collector.sqlite3'}")
    run_migrations(engine)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    monkeypatch.setattr("app.dependencies.build_dfs_provider", lambda name, runtime: adapters[name])
    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "FIREBASE_ADMIN_DISABLED": "true",
            "DATABASE_URL": "postgresql://statsplus.example/db",
            "DFS_ENABLED_PROVIDERS": ",".join(adapters),
            **environ,
        }
    )
    dependencies = build_dependencies(settings)
    tip_off = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(
        dependencies.projection_collection_coordinator,
        "event_reader",
        lambda season: [
            {
                "nba_game_id": "0022600001",
                "scheduled_at": tip_off,
                "status_text": "Scheduled",
                "status_code": 1,
            }
        ],
    )
    return dependencies, engine


def _polls(engine):
    table = ProviderPoll.__table__
    with engine.connect() as connection:
        return sorted(
            (row["provider"], row["outcome"], row["failure_reason"])
            for row in connection.execute(
                select(table.c.provider, table.c.outcome, table.c.failure_reason)
            ).mappings()
        )


def _states(engine):
    table = ProjectionCollectionProviderState.__table__
    with engine.connect() as connection:
        return sorted(
            (row["provider"], row["consecutive_failures"], row["last_failure_reason"])
            for row in connection.execute(
                select(table.c.provider, table.c.consecutive_failures, table.c.last_failure_reason)
            ).mappings()
        )


def test_collector_records_a_provider_the_request_time_board_drops_at_fifteen_seconds(tmp_path, monkeypatch):
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr("app.services.dfs_board.time", SimpleNamespace(monotonic=lambda: clock.value))
    dependencies, engine = _wired(
        tmp_path, monkeypatch, {"underdog": _SlowAdapter("underdog", clock, seconds=20)}
    )

    request_board = dependencies.dfs_board_service.get_board(NBAMarketQuery())
    result = dependencies.projection_collection_coordinator.run()

    assert [
        (outcome.provider, outcome.status, outcome.reason) for outcome in request_board.provider_outcomes
    ] == [("underdog", ProviderOutcomeStatus.FAILED, ProviderFailureReason.DEADLINE_EXCEEDED)]
    assert result.status == "complete"
    assert _polls(engine) == [("underdog", "changed", None)]
    assert _states(engine) == [("underdog", 0, None)]


@pytest.mark.parametrize(
    ("seconds", "poll", "state"),
    (
        (25, ("novig", "changed", None), ("novig", 0, None)),
        (31, ("novig", "failed", "deadline_exceeded"), ("novig", 1, "deadline_exceeded")),
    ),
)
def test_collector_board_ends_at_the_configured_collector_deadline(
    tmp_path, monkeypatch, seconds, poll, state
):
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr("app.services.dfs_board.time", SimpleNamespace(monotonic=lambda: clock.value))
    dependencies, engine = _wired(
        tmp_path,
        monkeypatch,
        {"novig": _SlowAdapter("novig", clock, seconds=seconds)},
        PROJECTION_COLLECTION_BOARD_DEADLINE_SECONDS="30",
    )

    dependencies.projection_collection_coordinator.run()

    assert _polls(engine) == [poll]
    assert _states(engine) == [state]


def test_collector_starts_every_enabled_provider_at_once(tmp_path, monkeypatch):
    names = dfs_provider_names()
    barrier = Barrier(len(names), timeout=5)
    dependencies, engine = _wired(
        tmp_path, monkeypatch, {name: _RendezvousAdapter(name, barrier) for name in names}
    )

    result = dependencies.projection_collection_coordinator.run()

    assert result.status == "complete"
    assert _polls(engine) == sorted((name, "changed", None) for name in names)
