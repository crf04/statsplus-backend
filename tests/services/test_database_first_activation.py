"""Activation/read-side contracts for backend issue #87."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.collection_control import PublicationService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    DatabaseOnlyProviderGuard,
    LegacyWriteFence,
)
from app.services.database_first_benchmark import benchmark_matchup_reads
from app.services.database_first_drills import FailureDrillRunner
from app.services.database_first_rehearsal import HistoricalRehearsalRunner


UTC = timezone.utc
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'activation.sqlite3'}")
    run_migrations(engine)
    return engine


def test_reader_serves_active_last_good_and_marks_it_stale(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "database_first_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    publication = service.compose(
        "database_first_test",
        season="2025-26",
        cutoff=NOW,
        payload={"value": 1},
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW + timedelta(hours=2))
    result = reader.read("database_first_test", season="2025-26")
    assert result.payload == {"value": 1}
    assert result.publication_id == publication.publication_id
    assert result.freshness == "stale"


def test_reader_keeps_rollback_publication_available(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "rollback_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
    )
    first = service.compose(
        "rollback_test", season="2025-26", cutoff=NOW, payload={"value": 1}
    )
    service.compose(
        "rollback_test",
        season="2025-26",
        cutoff=NOW,
        payload={"value": 2},
        expected_fence=1,
    )
    service.rollback("rollback_test", reason="restore last good")
    result = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).read(
        "rollback_test", season="2025-26"
    )
    assert result.status == "rollback"
    assert result.available
    assert result.publication_id != first.publication_id
    assert result.payload == {"value": 1}


def test_reader_reports_independent_missing_and_mixed_streams(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    for key in ("one", "two"):
        service.register_stream(
            key,
            provider="ledger",
            owner="railway",
            required_observations=(),
            publication_strategy="replace",
            enabled=True,
        )
    service.compose("one", season="2025-26", cutoff=NOW, payload={"v": 1})
    metadata = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).metadata(
        ("one", "two"), season="2025-26"
    )
    assert metadata["streams"]["one"]["status"] == "active"
    assert metadata["streams"]["two"]["status"] == "missing"
    assert metadata["mixed_cutoff"] is False


def test_legacy_write_fence_fails_only_after_stream_activation(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "player_game_logs",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=False,
    )
    fence = LegacyWriteFence(engine)
    fence.assert_writable("player_game_logs")
    service.activate_stream("player_game_logs", reason="approved activation")
    try:
        fence.assert_writable("player_game_logs")
    except Exception as error:
        assert str(error) == "legacy_write_fenced"
    else:  # pragma: no cover - assertion makes the contract explicit
        raise AssertionError("activated stream accepted a legacy write")


def test_provider_guard_is_fail_closed():
    guard = DatabaseOnlyProviderGuard("nba")
    try:
        guard.fetch_game_logs()
    except AssertionError as error:
        assert "database-only" in str(error)
    else:  # pragma: no cover
        raise AssertionError("provider call guard did not fail closed")


def test_historical_rehearsal_runs_seven_dates_without_pointer_mutation(tmp_path):
    engine = _db(tmp_path)
    dates = tuple(date(2026, 4, day) for day in range(6, 13))
    report = HistoricalRehearsalRunner(engine).run(
        "2025-26",
        cutoffs=dates,
        collect=lambda cutoff: {"streams": ("player_game_logs",), "cutoff": cutoff.isoformat()},
        synergy_check=lambda cutoff: True,
    )
    assert report.status == "passed"
    assert len(report.records) == 7
    assert report.production_pointers_unchanged
    assert report.synergy_season_status == "passed"


def test_historical_rehearsal_rejects_unordered_window(tmp_path):
    engine = _db(tmp_path)
    dates = tuple(date(2026, 4, day) for day in range(6, 12))
    report = HistoricalRehearsalRunner(engine).run("2025-26", cutoffs=dates)
    assert report.status == "failed"
    assert "exactly 7" in (report.error or "")


def test_failure_drills_are_deterministic_and_named():
    report = FailureDrillRunner(clock=lambda: NOW).run()
    assert report.status == "passed"
    assert {item.name for item in report.drills} == set(FailureDrillRunner.NAMES)
    assert report.drills[0].attempts == 2


def test_benchmark_emits_query_plan_and_passes_local_gate(tmp_path):
    engine = _db(tmp_path)
    report = benchmark_matchup_reads(
        engine,
        baseline=lambda: None,
        database_first=lambda: None,
        iterations=2,
    )
    assert report.passed
    assert report.query_plans
