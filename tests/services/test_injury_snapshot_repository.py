"""Temporary-database tests for durable injury evidence snapshots."""

from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select

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
    assert stored.raw_payload == [{"ID": "6504", "status": "Out"}]
    assert stored.normalized_entries == (
        {
            "entry_id": "rotowire:6504",
            "canonical_player_id": 2544,
            "canonical_status": "Out",
        },
    )
    assert stored.retrieved_at == NOW
    assert stored.source_entries == (
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

    assert repository.get(first).raw_payload == [{"ID": "6504", "status": "Out"}]
    assert repository.get(second).raw_payload == [{"ID": "6504", "status": "Out"}]
    assert repository.get_latest_source("rotowire") == source
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(InjurySourceSnapshot.__table__)
        ).scalar_one() == 1
