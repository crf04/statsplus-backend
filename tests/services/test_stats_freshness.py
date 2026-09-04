"""Persisted freshness for the atomically published stats surface."""

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text

from app.config.settings import RuntimeSettings
from app.migrations import run_migrations
from app.services.data_service import DataService
from app.services.stats_freshness_repository import (
    PLAYER_GAME_LOG_SURFACE,
    StatsFreshness,
    StatsFreshnessRepository,
)


def test_named_stats_surfaces_publish_and_read_independently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.sqlite3'}")
    run_migrations(engine)
    stats_completed = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    logs_completed = datetime(2026, 1, 2, 11, tzinfo=timezone.utc)

    StatsFreshnessRepository(engine).record_success(stats_completed)
    logs = StatsFreshnessRepository(engine, surface=PLAYER_GAME_LOG_SURFACE)
    logs.record_success(logs_completed)

    assert StatsFreshnessRepository(engine).get() == StatsFreshness(
        last_successful_completion=stats_completed,
    )
    assert logs.get() == StatsFreshness(
        last_successful_completion=logs_completed,
    )


def test_stats_freshness_is_missing_before_first_success(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.sqlite3'}")
    run_migrations(engine)

    assert StatsFreshnessRepository(engine).get() == StatsFreshness(
        last_successful_completion=None
    )


def test_successful_atomic_stats_publication_records_completion(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.sqlite3'}")
    run_migrations(engine)
    completed_at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    service = DataService(
        engine,
        settings=RuntimeSettings(environment="testing"),
        clock=lambda: completed_at,
        stats_freshness=StatsFreshnessRepository(engine),
    )
    monkeypatch.setattr(
        service,
        "_collect_all_frames",
        lambda: {"player_information": pd.DataFrame([{"TEAM_ID": 1}])},
    )

    assert service.update_all_data() is True
    assert StatsFreshnessRepository(engine).get() == StatsFreshness(
        last_successful_completion=completed_at
    )


def test_failed_stats_publication_preserves_last_success(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.sqlite3'}")
    run_migrations(engine)
    repository = StatsFreshnessRepository(engine)
    first = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    repository.record_success(first)
    service = DataService(
        engine,
        settings=RuntimeSettings(environment="testing"),
        clock=lambda: datetime(2026, 1, 2, 10, tzinfo=timezone.utc),
        stats_freshness=repository,
    )
    monkeypatch.setattr(
        service, "_collect_all_frames", lambda: {"bad-name!": pd.DataFrame([{"x": 1}])}
    )

    assert service.update_all_data() is False
    assert repository.get().last_successful_completion == first


def test_freshness_write_failure_rolls_back_stats_swap(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE player_information (value TEXT)"))
        connection.execute(text("INSERT INTO player_information VALUES ('old')"))
    class FailingFreshness:
        def record_success(self, *_args, **_kwargs):
            raise RuntimeError("write failed")

    service = DataService(
        engine,
        settings=RuntimeSettings(environment="testing"),
        stats_freshness=FailingFreshness(),
    )
    monkeypatch.setattr(
        service,
        "_collect_all_frames",
        lambda: {"player_information": pd.DataFrame([{"value": "new"}])},
    )
    assert service.update_all_data() is False
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT value FROM player_information")
            ).scalar_one()
            == "old"
        )
    assert (
        StatsFreshnessRepository(engine).get().last_successful_completion is None
    )
