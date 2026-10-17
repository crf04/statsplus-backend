"""Postgres checks for the ``usage_pct`` recomposition requeue (#450).

The same scenarios as ``tests/services/test_usage_recomposition.py``, on a real
PostgreSQL database where row locks, not SQLite's single writer, serialize the
requeue against corrections.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from tests.services.test_usage_recomposition import (
    build_season,
    check_correction_before_the_snapshot,
    check_expired_collection_window,
    check_other_streams_are_untouched,
    check_superseded_governing_manifest,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def world():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield build_season(engine)
    engine.dispose()


def test_other_enabled_streams_are_not_published_by_a_player_log_requeue(world):
    check_other_streams_are_untouched(world)


def test_an_expired_collection_window_still_allows_the_requeue(world):
    check_expired_collection_window(world)


def test_a_correction_before_the_requeue_snapshot_is_not_overwritten_with_stale_lineage(
    world,
):
    check_correction_before_the_snapshot(world)


def test_games_observed_under_a_superseded_manifest_are_recomposed_untouched_elsewhere(
    world,
):
    check_superseded_governing_manifest(world)
