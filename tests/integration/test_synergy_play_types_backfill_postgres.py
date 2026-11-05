"""Postgres check for the 2025-26 Synergy backfill (crf04/statsplus#416).

The same production rebuild and command as the SQLite suite, on the database
production runs: acceptance queues under the job row lock, the worker drains
it, and a rerun changes nothing.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from tests.test_synergy_play_types_backfill_script import _run, _season_scores, _state, build_production

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def production():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield build_production(url, engine)
    engine.dispose()


def test_the_backfill_publishes_2025_26_play_types_once_and_leaves_the_active_season(production):
    url, engine, _manifest = production
    before = _state(engine)

    assert _run(url) == 0
    settled = _state(engine)
    assert _run(url) == 0

    assert _season_scores(engine)["TOV"]["season"]["components"]["play_types"] == {
        "value": 0.365804,
        "thin": False,
    }
    assert _state(engine) == settled
    assert settled["seasons"] == before["seasons"] == {"2025-26": "inactive", "2026-27": "active"}
    assert settled["manifests"] == before["manifests"]
    assert settled["observations"] == before["observations"] + 22
