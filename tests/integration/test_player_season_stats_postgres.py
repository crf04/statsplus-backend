"""Postgres checks for the season player stats store (crf04/statsplus-backend#509):
migration 074, whole-season replace, and the read's SQL."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from app.errors import SeasonNotLoadedError
from app.migrations import run_migrations
from app.services.player_season_stats import PlayerSeasonStatsRepository
from tests.support.season_stats_world import (
    EVEN_LINE,
    JOKIC,
    JOKIC_LINE,
    RESERVE_LINE,
    rows,
    service_for,
)

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


def test_migration_074_creates_the_keyed_table_and_reruns_as_a_noop(engine):
    inspector = inspect(engine)

    assert "player_season_stats" in inspector.get_table_names()
    assert inspector.get_pk_constraint("player_season_stats")["constrained_columns"] == [
        "season",
        "season_type",
        "player_id",
    ]
    assert run_migrations(engine).applied == ()


def test_replace_season_is_idempotent_and_scoped_to_one_season(engine):
    repository = PlayerSeasonStatsRepository(engine)
    repository.replace_season("2023-24", "Regular Season", rows("2023-24", JOKIC_LINE, RESERVE_LINE))
    repository.replace_season("2024-25", "Regular Season", rows("2024-25", JOKIC_LINE))

    repository.replace_season("2023-24", "Regular Season", rows("2023-24", JOKIC_LINE, RESERVE_LINE))
    repository.replace_season("2023-24", "Regular Season", rows("2023-24", {**JOKIC_LINE, "GP": 80}))

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT season, player_id, gp FROM player_season_stats ORDER BY season, player_id")
        ).all()
    assert [tuple(row) for row in stored] == [("2023-24", JOKIC, 80), ("2024-25", JOKIC, 79)]


def test_a_failed_replace_rolls_back_the_delete(engine):
    repository = PlayerSeasonStatsRepository(engine)
    repository.replace_season("2023-24", "Regular Season", rows("2023-24", JOKIC_LINE))
    incomplete = {"season": "2023-24", "season_type": "Regular Season", "player_id": 1}

    with pytest.raises(Exception, match="null value|not-null"):
        repository.replace_season("2023-24", "Regular Season", [incomplete])

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM player_season_stats")).scalar() == 1


def test_the_service_reads_ordered_documents_and_lists_loaded_seasons(engine):
    repository = PlayerSeasonStatsRepository(engine)
    repository.replace_season(
        "2023-24", "Regular Season", rows("2023-24", EVEN_LINE, JOKIC_LINE, RESERVE_LINE)
    )
    repository.replace_season("2024-25", "Regular Season", rows("2024-25", JOKIC_LINE))
    service = service_for(repository)

    document = service.get_season_stats("2023-24")
    single = service.get_season_stats("2023-24", JOKIC)

    assert [player["player_id"] for player in document["players"]] == [JOKIC, 1700001, 1700002]
    assert single["players"][0]["per_game"]["pts"] == 26.4
    assert single["players"][0]["totals"]["min"] == 2736.5333333333333
    with pytest.raises(SeasonNotLoadedError) as raised:
        service.get_season_stats("2022-23")
    assert raised.value.loaded_seasons == ["2023-24", "2024-25"]
