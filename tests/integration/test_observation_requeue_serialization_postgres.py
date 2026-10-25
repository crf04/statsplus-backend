"""An observation accepted while a composition holds its claim, on Postgres.

The composition worker keeps the job row locked from claim to completion.  An
observation accepted meanwhile must wait for that run to record its failure,
then requeue the job, so the last team's observation is never lost.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.collection_control import CompositionJob
from app.services.collection_control import PublicationService

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

STREAM = "exact_shot_zones_opponent_season"
CUTOFF = datetime(2026, 10, 25, 8, tzinfo=UTC)


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


def test_an_observation_accepted_mid_composition_requeues_after_the_failure(engine):
    publications = PublicationService(engine, clock=lambda: CUTOFF)
    publications.register_stream(
        STREAM,
        provider="nba",
        owner="residential_collector",
        required_observations=["shot_zones_opponent"],
        publication_strategy="snapshot_replace",
        supported_windows=["season"],
        enabled=True,
    )
    publications.enqueue(STREAM, season="2026-27", cutoff=CUTOFF, manifest_id="manifest-1")
    accepted = threading.Event()

    def accept_late_observation():
        with Session(engine) as session, session.begin():
            publications.enqueue_for_observation(
                "shot_zones_opponent",
                season="2026-27",
                cutoff=CUTOFF,
                manifest_id="manifest-1",
                window="season",
                session=session,
            )
        accepted.set()

    with Session(engine) as worker, worker.begin():
        job = worker.scalar(
            select(CompositionJob).where(CompositionJob.stream_key == STREAM).with_for_update()
        )
        job.status = "running"
        worker.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            acceptance = pool.submit(accept_late_observation)
            accepted_while_claimed = accepted.wait(2)
            job.status = "failed"
            job.last_error = "publication_candidate_invalid"
            worker.commit()
            acceptance.result(20)

    with Session(engine) as session:
        job = session.scalar(select(CompositionJob).where(CompositionJob.stream_key == STREAM))
    assert not accepted_while_claimed, "acceptance finished while the composition held its claim"
    assert (job.status, job.last_error, job.attempts, job.generation) == ("queued", None, 1, 2)
