"""Slate neighbours and game-log seasons on PostgreSQL (crf04/statsplus#368).

The neighbour reads order by (season, scheduled_at) and the season listing
joins the pointer history, versions, and projection; these run the SQLite
scenarios' core paths against the production dialect.  Skipped unless
``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.models import Base
from tests.services import test_backtest_seasons as seasons
from tests.services import test_slate_neighbors as neighbours

pytestmark = pytest.mark.integration


def _clean(engine):
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    built = create_engine(url)
    _clean(built)
    yield built
    _clean(built)
    built.dispose()


def test_neighbours_across_a_season_boundary_on_postgres(engine):
    world = neighbours._World(engine)
    world.store("2025-26", neighbours._row("0042500001", "2025-26", "2026-06-14T00:00:00+00:00"))
    world.store(
        "2026-27",
        neighbours._row("0012600001", "2026-27", "2026-09-30T23:00:00+00:00"),
        neighbours._row("0022600001", "2026-27", "2026-10-22T00:00:00+00:00"),
    )

    assert world.neighbours("2026-07-15") == ("2026-06-13", "2026-10-21")
    assert world.neighbours("2026-10-21") == ("2026-06-13", None)
    world.assert_matches_the_slate_itself("2026-06-10", "2026-10-30")


def test_neighbours_skip_all_star_events_beyond_one_page_on_postgres(engine):
    world = neighbours._World(engine)
    exhibitions = [
        neighbours._row(
            f"00325{n:05d}", "2025-26", f"2026-02-{15 + n // 60:02d}T0{n % 6}:00:00+00:00",
            "All-Star Game",
        )
        for n in range(1, 130)
    ]
    world.store(
        "2025-26",
        neighbours._row("0022500001", "2025-26", "2026-02-10T00:00:00+00:00"),
        *exhibitions,
        neighbours._row("0022500002", "2025-26", "2026-02-25T00:00:00+00:00"),
    )

    assert world.neighbours("2026-02-20") == ("2026-02-09", "2026-02-24")


def test_seasons_list_the_active_and_retained_unrevoked_publications_on_postgres(engine):
    world = seasons.World(engine)
    for season in (seasons.LAST, seasons.NEW):
        world.compose(
            "player_game_logs", season, {"rows": seasons._season_logs(season, okc_games=1)}
        )

    assert world.logs.published_seasons() == ("2026-27", "2025-26")

    world.revoke("player_game_logs", seasons.LAST)

    assert world.logs.published_seasons() == ("2026-27",)
