"""Activation/read-side contracts for backend issue #87."""

from datetime import date, datetime, timedelta, timezone
import json
import hashlib

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion
from app.services.collection_control import PublicationService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    DatabaseOnlyProviderGuard,
    LegacyWriteFence,
    PublicationPayloadError,
    decode_player_game_logs,
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
    assert metadata["mixed_cutoff"] is True
    assert metadata["mixed_freshness"] is True


def test_reader_metadata_marks_fresh_plus_invalid_publication_as_mixed(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "fresh", provider="ledger", owner="railway", required_observations=(),
        publication_strategy="replace", enabled=True,
    )
    service.register_stream(
        "player_per36", provider="ledger", owner="railway",
        required_observations=(), publication_strategy="replace", enabled=True,
    )
    service.compose("fresh", season="2025-26", cutoff=NOW, payload={"value": 1})
    service.compose(
        "player_per36", season="2025-26", cutoff=NOW, payload={"rows": []}
    )

    metadata = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).metadata(
        ("fresh", "player_per36"), season="2025-26"
    )

    assert metadata["streams"]["player_per36"]["status"] == "unavailable"
    assert metadata["streams"]["player_per36"]["unavailable_reason"] == (
        "publication_payload_invalid"
    )
    assert metadata["mixed_cutoff"] is True
    assert metadata["mixed_freshness"] is True


def test_reader_marks_disabled_stream_as_the_only_legacy_fallback(tmp_path):
    engine = _db(tmp_path)
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_stream(
        "legacy_fallback_test",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=False,
    )
    result = DatabaseFirstPublicationReader(engine, clock=lambda: NOW).read(
        "legacy_fallback_test", season="2025-26"
    )
    assert result.legacy_fallback_allowed
    assert result.source == "legacy_database"
    assert result.status == "inactive"


def test_player_log_publication_decoder_is_strict():
    row = {
        "season": "2025-26",
        "season_type": "Regular Season",
        "player_id": 1,
        "game_id": "game-1",
        "player_name": "Player One",
        "game_date": "2026-01-01",
        "team_id": 1,
        "team_tricode": "AAA",
        "opponent_team_id": 2,
        "opponent_team_tricode": "BBB",
        "is_home": True,
        "minutes": 30.0,
        "points": 10,
        "rebounds": 5,
        "assists": 2,
        "field_goals_made": 4,
        "field_goals_attempted": 8,
        "three_pointers_made": 1,
        "three_pointers_attempted": 3,
        "free_throws_made": 1,
        "free_throws_attempted": 1,
        "offensive_rebounds": 1,
        "defensive_rebounds": 4,
        "turnovers": 1,
        "steals": 1,
        "blocks": 0,
        "personal_fouls": 1,
    }
    decoded = decode_player_game_logs([row], season="2025-26")
    assert decoded[0].game_id == "game-1"
    with pytest.raises(PublicationPayloadError):
        decode_player_game_logs([{**row, "is_home": "true"}], season="2025-26")


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
    player_log_payload = json.dumps({
        "rows": [{
            "season": "2025-26", "season_type": "Regular Season",
            "player_id": 1, "game_id": "game-1", "player_name": "Player One",
            "game_date": "2026-04-06", "team_id": 1, "team_tricode": "AAA",
            "opponent_team_id": 2, "opponent_team_tricode": "BBB",
            "is_home": True, "minutes": 30.0, "points": 10, "rebounds": 5,
            "assists": 2, "field_goals_made": 4, "field_goals_attempted": 8,
            "three_pointers_made": 1, "three_pointers_attempted": 3,
            "free_throws_made": 1, "free_throws_attempted": 1,
            "offensive_rebounds": 1, "defensive_rebounds": 4,
            "turnovers": 1, "steals": 1, "blocks": 0, "personal_fouls": 1,
        }]
    })
    synergy_payload = json.dumps({
        "base": "play_types",
        "rows": [{
            "player_id": 1, "slice_key": "Transition", "share": 0.1,
            "volume": 1.0, "games_played": 1, "volume_unit": "possessions",
            "provider": "nba_synergy",
        }],
    })
    with engine.begin() as connection:
        for cutoff in dates:
            connection.execute(PublicationVersion.__table__.insert().values(
                publication_id=f"rehearsal-{cutoff.isoformat()}",
                stream_key="player_game_logs",
                season="2025-26",
                cutoff=datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC),
                version=1,
                status="candidate",
                checksum=hashlib.sha256(player_log_payload.encode()).hexdigest(),
                payload=player_log_payload,
                created_at=NOW,
                fence=0,
            ))
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id="rehearsal-synergy",
            stream_key="synergy_play_types",
            season="2025-26",
            cutoff=datetime.combine(dates[-1], datetime.min.time(), tzinfo=UTC),
            version=1,
            status="candidate",
            checksum=hashlib.sha256(synergy_payload.encode()).hexdigest(),
            payload=synergy_payload,
            created_at=NOW,
            fence=0,
        ))
    report = HistoricalRehearsalRunner(engine, environment="unit").run(
        "2025-26",
        cutoffs=dates,
        collect=lambda cutoff: {
            "publication_ids": {
                "player_game_logs": f"rehearsal-{cutoff.isoformat()}"
            },
            "expected_facts": {"player_game_logs": json.loads(player_log_payload)},
        },
        synergy_check=lambda cutoff: {
            "candidate_publication_id": "rehearsal-synergy",
            "expected_facts": {"synergy_play_types": json.loads(synergy_payload)},
        },
    )
    assert report.status == "passed"
    assert len(report.records) == 7
    assert not report.production_pointers_unchanged
    assert not report.production_immutability_checked
    assert report.synergy_season_status == "passed"


def test_historical_rehearsal_rejects_unordered_window(tmp_path):
    engine = _db(tmp_path)
    dates = tuple(date(2026, 4, day) for day in range(6, 12))
    report = HistoricalRehearsalRunner(engine, environment="unit").run("2025-26", cutoffs=dates)
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


def test_benchmark_rejects_one_callable_for_both_paths(tmp_path):
    engine = _db(tmp_path)

    def read():
        return None

    with pytest.raises(ValueError, match="distinct"):
        benchmark_matchup_reads(
            engine, baseline=read, database_first=read, iterations=1
        )
