"""Temporary-database tests for durable injury evidence snapshots."""

from datetime import datetime, timezone

from sqlalchemy import create_engine, event, func, select

from app.migrations import run_migrations
from app.models.injury_snapshot import InjurySourceSnapshot
from app.services.injury_snapshot_repository import (
    InjurySnapshotRepository,
    InjurySnapshotScope,
)


NOW = datetime(2026, 1, 15, 23, 55, tzinfo=timezone.utc)


def test_snapshot_replaces_raw_and_normalized_evidence_atomically(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'injuries.sqlite3'}")
    run_migrations(engine)
    repository = InjurySnapshotRepository(engine)
    scope = InjurySnapshotScope("2025-26", "0022500584")

    repository.publish(
        scope,
        source="rotowire",
        raw_payload=[{"ID": "6504", "status": "Questionable"}],
        source_entries=[{"entry_id": "rotowire:6504", "raw_status": "Questionable"}],
        normalized_entries=[
            {
                "entry_id": "rotowire:6504",
                "canonical_player_id": 2544,
                "canonical_status": "Questionable",
            }
        ],
        retrieved_at=NOW,
        unresolved_team_entry_count=0,
    )
    repository.publish(
        scope,
        source="rotowire",
        raw_payload=[{"ID": "6504", "status": "Out"}],
        source_entries=[{"entry_id": "rotowire:6504", "raw_status": "Out"}],
        normalized_entries=[
            {
                "entry_id": "rotowire:6504",
                "canonical_player_id": 2544,
                "canonical_status": "Out",
            }
        ],
        retrieved_at=NOW,
        unresolved_team_entry_count=0,
    )

    stored = repository.get(scope)
    assert stored is not None
    assert stored.normalized_entries == (
        {
            "entry_id": "rotowire:6504",
            "canonical_player_id": 2544,
            "canonical_status": "Out",
        },
    )
    assert stored.retrieved_at == NOW
    evidence = repository.get_evidence(scope)
    assert evidence is not None
    assert evidence.raw_payload == [{"ID": "6504", "status": "Out"}]
    assert evidence.source_entries == (
        {"entry_id": "rotowire:6504", "raw_status": "Out"},
    )


def test_one_league_feed_is_reused_without_copying_raw_payload_per_game(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared-injuries.sqlite3'}")
    run_migrations(engine)
    repository = InjurySnapshotRepository(engine)
    first = InjurySnapshotScope("2025-26", "0022500584")
    second = InjurySnapshotScope("2025-26", "0022500585")

    source = repository.publish(
        first,
        source="rotowire",
        raw_payload=[{"ID": "6504", "status": "Out"}],
        source_entries=[{"entry_id": "rotowire:6504", "raw_status": "Out"}],
        normalized_entries=[{"entry_id": "rotowire:6504", "team_id": 1}],
        retrieved_at=NOW,
        unresolved_team_entry_count=0,
    )
    repository.replace_from_source(
        second,
        source_snapshot=source,
        normalized_entries=[],
        unresolved_team_entry_count=0,
    )

    assert repository.get_evidence(first).raw_payload == [
        {"ID": "6504", "status": "Out"}
    ]
    assert repository.get_evidence(second).raw_payload == [
        {"ID": "6504", "status": "Out"}
    ]
    assert repository.get_latest_source("rotowire") == source
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(InjurySourceSnapshot.__table__)
        ).scalar_one() == 1


def test_game_snapshot_read_does_not_load_league_source_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hot-path.sqlite3'}")
    run_migrations(engine)
    repository = InjurySnapshotRepository(engine)
    scope = InjurySnapshotScope("2025-26", "0022500584")
    repository.publish(
        scope,
        source="rotowire",
        raw_payload=[{"large": "league payload"}],
        source_entries=[{"entry_id": "rotowire:6504"}],
        normalized_entries=[{"entry_id": "rotowire:6504", "team_id": 1}],
        retrieved_at=NOW,
        unresolved_team_entry_count=2,
    )
    statements = []

    def record_statement(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        stored = repository.get(scope)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert stored.normalized_entries == ({"entry_id": "rotowire:6504", "team_id": 1},)
    assert stored.unresolved_team_entry_count == 2
    assert len(statements) == 1
    assert "injury_source_snapshots" not in statements[0]
    assert "raw_payload" not in statements[0]


def test_source_retention_prunes_only_old_unreferenced_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite3'}")
    run_migrations(engine)
    repository = InjurySnapshotRepository(engine)
    retained_scope = InjurySnapshotScope("2025-26", "retained")
    rotating_scope = InjurySnapshotScope("2025-26", "rotating")
    retained = repository.publish(
        retained_scope,
        source="rotowire",
        raw_payload=[{"version": "retained"}],
        source_entries=[],
        normalized_entries=[],
        retrieved_at=NOW,
        unresolved_team_entry_count=0,
    )

    for version in range(15):
        repository.publish(
            rotating_scope,
            source="rotowire",
            raw_payload=[{"version": version}],
            source_entries=[],
            normalized_entries=[],
            retrieved_at=NOW,
            unresolved_team_entry_count=0,
        )

    with engine.connect() as connection:
        source_ids = connection.execute(
            select(InjurySourceSnapshot.id).order_by(InjurySourceSnapshot.id)
        ).scalars().all()
    assert retained.source_snapshot_id in source_ids
    assert len(source_ids) == 14
    assert repository.get_evidence(retained_scope).raw_payload == [
        {"version": "retained"}
    ]
    assert repository.get_evidence(rotating_scope).raw_payload == [{"version": 14}]
