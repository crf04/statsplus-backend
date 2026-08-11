"""Temporary-database tests for durable injury evidence snapshots."""

from datetime import datetime, timezone

from sqlalchemy import create_engine

from app.migrations import run_migrations
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

    repository.replace(
        scope,
        raw_payload=[{"ID": "6504", "status": "Questionable"}],
        normalized_entries=[
            {
                "entry_id": "rotowire:6504",
                "canonical_player_id": 2544,
                "canonical_status": "Questionable",
            }
        ],
        retrieved_at=NOW,
    )
    repository.replace(
        scope,
        raw_payload=[{"ID": "6504", "status": "Out"}],
        normalized_entries=[
            {
                "entry_id": "rotowire:6504",
                "canonical_player_id": 2544,
                "canonical_status": "Out",
            }
        ],
        retrieved_at=NOW,
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
