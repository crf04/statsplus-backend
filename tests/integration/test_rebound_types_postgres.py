"""Postgres checks for Rebound Types (crf04/statsplus#234, backend #446):
migrations 069 and 070 on a deployed-shape database, and the ledger backfill's
row-locked, idempotent write."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
)
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_rebound_types_ledger import SIX, _game

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def _as_deployed_before_069(engine):
    with engine.begin() as connection:
        for table in (
            "canonical_game_ledger_team_facts", "canonical_game_ledger_player_facts",
        ):
            for name in SIX:
                connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
        connection.execute(text(
            "ALTER TABLE player_diet_facts DROP CONSTRAINT ck_player_diet_base"
        ))
        connection.execute(text(
            "ALTER TABLE player_diet_facts DROP CONSTRAINT ck_player_diet_volume_unit"
        ))
        connection.execute(text(
            "ALTER TABLE player_diet_surface_observations "
            "DROP CONSTRAINT ck_player_diet_observation_base"
        ))
        connection.execute(text(
            "ALTER TABLE player_diet_facts ADD CONSTRAINT ck_player_diet_base CHECK "
            "(base IN ('play_types', 'shot_zones', 'shot_types', 'assist_locations'))"
        ))
        connection.execute(text(
            "ALTER TABLE player_diet_facts ADD CONSTRAINT ck_player_diet_volume_unit "
            "CHECK ((base = 'play_types' AND volume_unit = 'possessions') OR "
            "(base IN ('shot_zones', 'shot_types') AND volume_unit = 'field_goal_attempts') "
            "OR (base = 'assist_locations' AND volume_unit = 'assists'))"
        ))
        connection.execute(text(
            "ALTER TABLE player_diet_surface_observations ADD CONSTRAINT "
            "ck_player_diet_observation_base CHECK "
            "(base IN ('play_types', 'shot_zones', 'shot_types', 'assist_locations'))"
        ))
        connection.execute(text("DELETE FROM schema_migrations WHERE version IN (69, 70)"))


_DIET_INSERT = (
    "INSERT INTO player_diet_facts (season, player_id, base, slice_key, share, "
    "volume, games_played, volume_unit, provider, retrieved_at) VALUES "
    "('2025-26', 7, :base, 'DefTwoPtRebounds', 0.5, 10, 5, :unit, 'pbp_stats', now())"
)


def test_migrations_069_and_070_upgrade_a_deployed_postgres_database(engine):
    _as_deployed_before_069(engine)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(_DIET_INSERT), {"base": "rebound_types", "unit": "rebounds"})

    upgraded = run_migrations(engine)

    assert upgraded.applied == (
        "069_ledger_rebound_types", "070_player_diet_rebound_types_base",
    )
    assert run_migrations(engine).applied == ()
    for table in (
        "canonical_game_ledger_team_facts", "canonical_game_ledger_player_facts",
    ):
        assert set(SIX) <= {c["name"] for c in inspect(engine).get_columns(table)}
    with engine.begin() as connection:
        connection.execute(text(_DIET_INSERT), {"base": "rebound_types", "unit": "rebounds"})
    for base, unit in (("rebound_types", "assists"), ("dunks", "rebounds")):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(_DIET_INSERT.replace("7,", "8,")), {"base": base, "unit": unit})


def test_the_backfill_fills_and_a_rerun_changes_nothing_on_postgres(engine):
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_game(_game())
    nulls = {name: None for name in SIX}
    with engine.begin() as connection:
        connection.execute(update(CanonicalGameLedgerPlayerFact.__table__).values(nulls))
        connection.execute(update(CanonicalGameLedgerTeamFact.__table__).values(nulls))

    first = repository.backfill_rebound_types(season="2024-25")
    second = repository.backfill_rebound_types(season="2024-25")

    assert (first.updated, first.rejected) == (1, ())
    assert (second.updated, second.unchanged) == (0, 1)
    table = CanonicalGameLedgerPlayerFact.__table__
    with engine.connect() as connection:
        rows = [
            tuple(row)
            for row in connection.execute(
                select(table.c.player_id, *[getattr(table.c, n) for n in SIX])
                .order_by(table.c.player_id)
            )
        ]
    assert rows[0] == (101, 2, 0, 0, 0, 1, 0)
    assert rows[3] == (201, 1, 0, 0, 1, 0, 1)
