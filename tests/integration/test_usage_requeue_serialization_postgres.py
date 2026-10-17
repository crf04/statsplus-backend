"""A real concurrent correction against the usage requeue on Postgres (#450).

The requeue locks the season's game and job rows before it reads its snapshot,
in a correction's own lock order.  Here the requeue is paused with its snapshot
read; a correction started meanwhile must wait for the requeue to finish, then
re-queue on top of it, so the job ends succeeded with the corrected evidence.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.services import ledger_materialization as materialization
from app.services.canonical_game_ledger import raw_rows_from_facts
from tests.services.test_correction_propagation import _boundary_observation_values
from tests.services.test_usage_recomposition import STREAM, _job, build_season

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=8)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def test_requeue_serializes_a_real_correction_until_its_snapshot_is_enqueued(engine):
    _engine, repository, runtime, game, clock, _count = build_season(engine)
    repository.backfill_usage(season=game.season)
    # Pause on the first game the requeue reads, so it has queued nothing (and
    # so holds no job-row lock of its own) while the correction runs.
    first = repository.list_games(game.season)[0].game_id
    game = repository.get_game(first)
    stored = game
    bare = replace(
        stored,
        player_facts=tuple(replace(p, usage_pct=88.875) for p in stored.player_facts),
        source_observation_id=stored.source_observation_id + "-correction",
        retrieved_at=clock() + timedelta(seconds=1),
        raw_checksum=None,
        checksum=None,
    )
    corrected = replace(bare, raw_rows=raw_rows_from_facts(bare)).with_checksum()
    snapshot = threading.Event()
    resume = threading.Event()
    correction_committed = threading.Event()
    original_get = repository.get_games

    def paused_get(ids, **kwargs):
        rows = original_get(ids, **kwargs)
        if (
            threading.current_thread().name.startswith("requeue")
            and not snapshot.is_set()
            and any(g is not None and g.game_id == game.game_id for g in rows)
        ):
            snapshot.set()
            assert resume.wait(15)
        return rows

    repository.get_games = paused_get

    def correct():
        repository.replace_games_atomic(
            (corrected,),
            accepted_observations={
                corrected.source_observation_id: _boundary_observation_values(
                    corrected,
                    cutoff=clock(),
                    manifest_id="usage-manifest",
                )
            },
        )
        correction_committed.set()
        runtime.compose_queued(game.season)

    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="requeue") as requeue_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="correct") as correction_pool,
    ):
        queued = requeue_pool.submit(
            materialization.requeue_ledger_streams,
            repository,
            game.season,
            (STREAM,),
            clock=clock,
        )
        assert snapshot.wait(15)
        correction = correction_pool.submit(correct)
        committed_before_resume = correction_committed.wait(2)
        resume.set()
        queued.result(20)
        correction.result(20)

    runtime.compose_queued(game.season)
    job = _job(engine)

    assert not committed_before_resume, "correction committed while requeue held its snapshot"
    assert job["status"] == "succeeded"
    assert job["last_error"] is None
    assert {p.usage_pct for p in repository.get_game(game.game_id).player_facts} == {88.875}
