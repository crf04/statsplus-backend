"""PostgreSQL coverage for projection archive retention (#502): FK RESTRICT and batch locking."""

from __future__ import annotations

import os
import threading
import time

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError

from app.migrations import downgrade_projection_retention, run_migrations
from app.models import Base
from app.models.projection_archive import (
    ClosingProjectionMembership,
    ProjectionArchiveScopeLock,
    ProjectionObservation,
)
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchiveReadScope,
    projection_query_key,
)
from app.services.projection_retention import RETENTION_ADVISORY_LOCK_ID
from tests.services.test_projection_retention import (
    EXPECTED_HISTORY,
    EXPECTED_PROTECTED,
    EXPECTED_TABLES,
    G0,
    G1,
    LATE_ARRIVAL,
    NOW,
    PRUNED,
    QUERY,
    SEASON,
    SEEDED,
    Events,
    _forget_closing_protection,
    history,
    late_arrival,
    observations,
    retention,
    seed,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def pg_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")
    run_migrations(engine)
    yield engine
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")
    engine.dispose()


def _underdog(engine):
    return [row for row in observations(engine) if row[0] == "underdog"]


def test_postgres_prune_keeps_readers_whole_and_reruns_as_a_noop(pg_engine):
    seed(pg_engine)
    reader = LatestProjectionPlayerPoolReader(
        pg_engine,
        tuple(ProjectionArchiveReadScope(provider=p, query=QUERY) for p in ("dabble", "underdog")),
        clock=lambda: NOW,
        event_reader=Events(),
    )

    def reads():
        return (
            reader.get_retained_pool_for_game(season=SEASON, game_id=G0).players,
            reader.get_retained_pool_for_game(season=SEASON, game_id=G1).players,
            reader.get_pool_for_game(season=SEASON, game_id=G1).players,
        )

    before = reads()
    dry = retention(pg_engine).run(dry_run=True)
    assert observations(pg_engine) == SEEDED

    report = retention(pg_engine).run(dry_run=False)

    assert (dry.outcome, report.outcome) == ("succeeded", "succeeded")
    assert dry.as_dict()["tables"] == report.as_dict()["tables"] == EXPECTED_TABLES
    assert dry.as_dict()["protected"] == report.as_dict()["protected"] == EXPECTED_PROTECTED
    assert observations(pg_engine) == PRUNED
    assert history(pg_engine) == EXPECTED_HISTORY
    assert reads() == before

    second = retention(pg_engine).run(dry_run=False)
    assert sum(cell["delete"] for table in second.tables.values() for cell in table.values()) == 0
    assert sum(cell["rows"] for cell in second.history.values()) == 0
    assert observations(pg_engine) == PRUNED


def test_postgres_foreign_keys_restrict_and_a_buggy_batch_rolls_back(pg_engine, monkeypatch):
    seed(pg_engine)
    membership = ClosingProjectionMembership.__table__
    observation = ProjectionObservation.__table__
    with pg_engine.connect() as connection:
        member = connection.execute(select(membership.c.observation_id).limit(1)).scalar_one()
    with pytest.raises(IntegrityError), pg_engine.begin() as connection:
        connection.execute(delete(observation).where(observation.c.observation_id == member))
    _forget_closing_protection(monkeypatch)
    before = _underdog(pg_engine)

    report = retention(pg_engine).run(dry_run=False)

    assert (report.outcome, report.failure) == ("failed", {"reason": "IntegrityError"})
    assert _underdog(pg_engine) == before


def test_a_delete_batch_waits_for_the_collectors_scope_lock(pg_engine):
    seed(pg_engine)
    lock = ProjectionArchiveScopeLock.__table__
    holder = pg_engine.connect()
    transaction = holder.begin()
    holder.execute(
        select(lock)
        .where(
            lock.c.provider == "underdog",
            lock.c.season == SEASON,
            lock.c.query_key == projection_query_key(QUERY),
        )
        .with_for_update()
    ).one()
    result = {}
    worker = threading.Thread(target=lambda: result.update(report=retention(pg_engine).run(dry_run=False)))
    worker.start()
    try:
        time.sleep(1.5)
        # The collector holds underdog's scope: its rows wait, dabble's do not.
        assert worker.is_alive()
        assert _underdog(pg_engine) == [row for row in SEEDED if row[0] == "underdog"]
        assert [row for row in observations(pg_engine) if row[0] == "dabble"] == [
            ("dabble", "db-11-pts", 27.5, "10-13 12:00")
        ]
    finally:
        transaction.commit()
        holder.close()
        worker.join(timeout=30)
    assert result["report"].outcome == "succeeded"
    assert observations(pg_engine) == PRUNED


def test_a_second_concurrent_run_is_refused(pg_engine):
    seed(pg_engine)
    with pg_engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": RETENTION_ADVISORY_LOCK_ID})
        try:
            report = retention(pg_engine).run(dry_run=False)
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": RETENTION_ADVISORY_LOCK_ID})
            holder.commit()

    assert (report.outcome, report.failure) == ("failed", {"reason": "another retention run holds the lock"})
    assert observations(pg_engine) == SEEDED


def test_postgres_late_observation_lands_between_its_neighbours(pg_engine):
    assert late_arrival(pg_engine) == LATE_ARRIVAL


INDEXES = (
    "ix_projection_observations_observed_at",
    "ix_projection_observations_source_poll",
    "ix_latest_player_projections_observation",
    "ix_projection_closing_memberships_generation",
    "ix_projection_provider_polls_snapshot",
)


def _index_states(engine) -> dict[str, bool | None]:
    with engine.connect() as connection:
        return {
            name: connection.execute(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"), {"name": name}
            ).scalar_one_or_none()
            for name in INDEXES
        }


def test_migration_074_builds_its_indexes_without_blocking_collector_writes(pg_engine):
    seed(pg_engine)
    with pg_engine.begin() as connection:
        downgrade_projection_retention(connection)
    assert _index_states(pg_engine) == dict.fromkeys(INDEXES)
    # An open collector transaction: a concurrent build waits for it, but
    # takes no lock that stops a new write.
    collector = pg_engine.connect()
    collector_transaction = collector.begin()
    collector.execute(text("LOCK TABLE projection_observations IN ROW EXCLUSIVE MODE"))
    result = {}
    migrating = threading.Thread(target=lambda: result.update(applied=run_migrations(pg_engine).applied))
    migrating.start()
    try:
        time.sleep(1.5)
        assert migrating.is_alive()
        with pg_engine.begin() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
            writer.execute(text("LOCK TABLE projection_observations IN ROW EXCLUSIVE MODE"))
    finally:
        collector_transaction.commit()
        collector.close()
        migrating.join(timeout=60)
    assert result["applied"] == ("074_projection_retention",)
    assert _index_states(pg_engine) == dict.fromkeys(INDEXES, True)


def test_an_interrupted_index_build_converges_on_the_next_migration_run(pg_engine):
    with pg_engine.begin() as connection:
        # What an interrupted CREATE INDEX CONCURRENTLY leaves: one index
        # missing, one present but invalid.
        connection.execute(text("DROP INDEX ix_projection_observations_observed_at"))
        connection.execute(
            text(
                "UPDATE pg_index SET indisvalid = false "
                "WHERE indexrelid = to_regclass('ix_projection_observations_source_poll')"
            )
        )
    assert _index_states(pg_engine)["ix_projection_observations_observed_at"] is None
    assert _index_states(pg_engine)["ix_projection_observations_source_poll"] is False

    assert run_migrations(pg_engine).applied == ()

    assert _index_states(pg_engine) == dict.fromkeys(INDEXES, True)
