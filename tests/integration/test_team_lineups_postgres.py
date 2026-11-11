"""Postgres checks for the stored daily lineups (crf04/statsplus#492): migration 087,
a rerun that leaves the same rows, and the team read across the Eastern day."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.migrations import run_migrations
from app.models.team_lineup import TeamLineup
from app.services.team_lineups import TeamLineupRepository, collect_team_lineups
from tests.services.test_team_lineups import (
    LATE_EVENING,
    OKC,
    OKC_INACTIVE,
    OKC_STARTERS,
    POR,
    RECORDED,
)
from tests.support.game_context_world import schedule

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


def test_migration_087_reruns_and_a_second_collection_leaves_the_same_rows_on_postgres(engine):
    assert "team_lineups" in inspect(engine).get_table_names()
    assert run_migrations(engine).applied == ()
    schedule(engine, "0022600216", "2026-11-11T04:00:00+00:00", OKC, POR, season="2026-27")

    collect_team_lineups(engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING)
    with engine.connect() as connection:
        first = connection.execute(select(TeamLineup.__table__).order_by("game_id", "team_id")).all()
    collect_team_lineups(engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING)

    with engine.connect() as connection:
        assert connection.execute(select(TeamLineup.__table__).order_by("game_id", "team_id")).all() == first
    assert TeamLineupRepository(engine, clock=lambda: LATE_EVENING).read("OKC") == {
        "status": "posted",
        "game_id": "0022600216",
        "game_date": "2026-11-10",
        "matchup": "OKC @ POR",
        "feed_timestamp": "2026-11-10T22:29:43-05:00",
        "collected_at": "2026-11-11T03:30:00+00:00",
        "starters": OKC_STARTERS,
        "inactive": OKC_INACTIVE,
    }
