"""Migration 075: retention runs and the prune indexes (#502)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

from app.migrations import downgrade_projection_retention, run_migrations
from tests.services.test_projection_retention import SEEDED, observations, seed

pytestmark = pytest.mark.real_migrations

INDEXES = {
    "projection_observations": {
        "ix_projection_observations_observed_at": ["observed_at"],
        "ix_projection_observations_source_poll": ["source_poll_id"],
    },
    "latest_player_projections": {"ix_latest_player_projections_observation": ["observation_id"]},
    "projection_closing_memberships": {"ix_projection_closing_memberships_generation": ["generation_id"]},
    "projection_provider_polls": {"ix_projection_provider_polls_snapshot": ["snapshot_id"]},
}


def _indexes(engine) -> dict[str, dict[str, list[str]]]:
    inspector = inspect(engine)
    return {
        table: {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes(table)
            if index["name"] in INDEXES[table]
        }
        for table in INDEXES
    }


def _tables(engine) -> set[str]:
    names = set(inspect(engine).get_table_names())
    return names & {"projection_retention_runs"}


def test_existing_archive_gains_the_tables_and_indexes_and_keeps_its_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    run_migrations(engine)
    assert _indexes(engine) == INDEXES
    seed(engine)
    with engine.begin() as connection:
        downgrade_projection_retention(connection)
    assert _tables(engine) == set()
    assert _indexes(engine) == {table: {} for table in INDEXES}

    assert run_migrations(engine).applied == ("075_projection_retention",)

    assert _tables(engine) == {"projection_retention_runs"}
    assert _indexes(engine) == INDEXES
    assert observations(engine) == SEEDED
    assert run_migrations(engine).applied == ()
    engine.dispose()
