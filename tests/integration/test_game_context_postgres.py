"""Postgres checks for the stored game context (crf04/statsplus#466): migration 086,
refresh round trips that compare equal, and the Matchup read."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app.migrations import run_migrations
from app.services.game_context import GameContextRepository
from tests.services.test_game_context import SEASON, UPCOMING, UPCOMING_CONTEXT, seed_week
from tests.support.game_context_world import postpone

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

FIRST_RUN = datetime(2026, 10, 25, 14, 15, tzinfo=UTC)
LATER_RUN = datetime(2026, 10, 25, 16, 15, tzinfo=UTC)


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


def test_migration_086_reruns_and_a_second_refresh_changes_nothing_on_postgres(engine):
    assert {"game_contexts", "game_context_teams"} <= set(inspect(engine).get_table_names())
    assert run_migrations(engine).applied == ()
    seed_week(engine)
    GameContextRepository(engine, clock=lambda: FIRST_RUN).refresh(SEASON)
    repository = GameContextRepository(engine, clock=lambda: LATER_RUN)

    summary = repository.refresh(SEASON)

    assert (summary["inserted"], summary["updated"], summary["removed"], summary["unchanged"]) == (0, 0, 0, 4)
    assert repository.read(UPCOMING) == UPCOMING_CONTEXT
    with engine.connect() as connection:
        assert repository.read(UPCOMING, connection=connection) == UPCOMING_CONTEXT


def test_a_postponed_game_loses_its_team_rows_on_postgres(engine):
    seed_week(engine)
    repository = GameContextRepository(engine, clock=lambda: FIRST_RUN)
    repository.refresh(SEASON)
    postpone(engine, UPCOMING)

    assert repository.refresh(SEASON)["removed"] == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM game_context_teams WHERE game_id = :game_id"),
                {"game_id": UPCOMING},
            ).scalar()
            == 0
        )
