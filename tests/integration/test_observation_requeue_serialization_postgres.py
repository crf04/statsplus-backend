"""An observation uploaded while a composition holds its job, on Postgres.

The composition worker keeps the job row locked from claim to completion.  An
upload arriving meanwhile must neither wait for it (the wait can outlast the
collector's lease and lose the upload) nor be accepted unseen by that run.  It
is refused as retryable, and the retry after the run requeues the job.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, wait

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.collection_control import CollectionObservation, CompositionJob
from app.services.collection_control import ControlPlaneError
from tests.test_residential_collector import (
    _compose_queued_slice,
    _composition_job,
    _grouped_shot_type_control_plane,
    _player_shot_type_rows,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

STREAM = "grouped_shot_types"


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


def _accepted(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(CollectionObservation)
            .where(CollectionObservation.observation_type == STREAM)
        ).scalar_one()


def test_an_upload_during_a_composition_is_retried_and_requeues_the_job(engine):
    _, _, _, deliver = _grouped_shot_type_control_plane(None, None, engine=engine)

    def deliver_rows(categories):
        deliver(
            lambda category: _player_shot_type_rows(2544, category, share=0.25, fga=100, fgm=46),
            only=categories,
        )

    deliver_rows(("Catch and Shoot", "Less Than 10 ft"))
    assert _composition_job(engine, STREAM) == (("queued", None, 0, 1), None)

    pool = ThreadPoolExecutor(max_workers=1)
    with Session(engine) as worker, worker.begin():
        job = worker.scalar(
            select(CompositionJob).where(CompositionJob.stream_key == STREAM).with_for_update()
        )
        job.status = "running"
        job.claimed_generation = job.generation
        worker.flush()
        upload = pool.submit(deliver_rows, ("Pullups",))
        returned_during_composition = upload in wait([upload], timeout=10).done
        job.status = "failed"
        job.claimed_generation = None
        job.last_error = "base_incomplete"
    error = upload.exception(timeout=30)
    pool.shutdown()

    assert returned_during_composition, "the upload waited for the composition"
    assert isinstance(error, ControlPlaneError) and str(error) == "collector_busy"
    assert _accepted(engine) == 2
    assert _composition_job(engine, STREAM) == (("failed", "base_incomplete", 0, 1), None)

    deliver_rows(("Pullups",))
    assert _accepted(engine) == 3
    assert _composition_job(engine, STREAM) == (("queued", None, 1, 2), None)
    assert _compose_queued_slice(engine) == 1
    assert _composition_job(engine, STREAM)[0] == ("succeeded", None, 1, 2)
