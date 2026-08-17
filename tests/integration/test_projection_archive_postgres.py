"""PostgreSQL production-fence coverage for projection transitions (#105)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, event, inspect, select, text

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.models import Base
from app.migrations import run_migrations
from app.models.projection_archive import (
    LatestPlayerProjection,
    ProjectionMaterializationGeneration,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
)
from app.services.statistic_catalog import StatisticCatalog


pytestmark = pytest.mark.integration
SEASON = "2025-26"
GAME_ID = "0022500501"
OBSERVED_AT = datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def projection_pg_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _snapshot(catalog, retrieved_at, threshold):
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="market-1",
        athlete=AthleteEvidence(
            canonical_id=7,
            name="Player 7",
            team=TeamEvidence(canonical_id=10),
        ),
        event=EventEvidence(canonical_id=GAME_ID),
        team=TeamEvidence(canonical_id=10),
        statistic=evidence,
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=statistic,
            provider="dabble",
        ),
        threshold=MarketThreshold(threshold, "count"),
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    return ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=1,
        ),
        retrieved_at=retrieved_at,
    )


def test_concurrent_postgres_ingestion_has_one_temporal_winner_and_no_mixed_generation(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    older = _snapshot(catalog, OBSERVED_AT, "20.5")
    newer = replace(
        _snapshot(catalog, OBSERVED_AT + timedelta(minutes=1), "21.5")
    )
    barrier = Barrier(2)
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)

    def ingest(snapshot):
        barrier.wait(timeout=5)
        worker_engine = create_engine(database_url)
        try:
            return ProjectionArchive(worker_engine, catalog).ingest_snapshot(
                snapshot,
                query=query,
                accepted_at=OBSERVED_AT + timedelta(minutes=2),
            )
        finally:
            worker_engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(ingest, (older, newer)))

    with projection_pg_engine.connect() as connection:
        latest_generations = set(
            connection.execute(select(LatestPlayerProjection.generation_id)).scalars()
        )
        winning_generation = connection.execute(
            select(ProjectionMaterializationGeneration.generation_id).where(
                ProjectionMaterializationGeneration.retrieved_at
                == newer.retrieved_at
            )
        ).scalar_one()
        assert latest_generations == {winning_generation}
        assert len(connection.execute(select(ProviderPoll.poll_id)).all()) == 2
        assert len(
            connection.execute(select(ProjectionProviderSnapshot.snapshot_id)).all()
        ) == 2
    assert sum(result.materialization_outcome == "advanced" for result in results) in {
        1,
        2,
    }


def test_concurrent_duplicate_postgres_ingestion_is_idempotent(projection_pg_engine):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    snapshot = _snapshot(catalog, OBSERVED_AT, "20.5")
    barrier = Barrier(2)
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)

    def ingest(_index):
        barrier.wait(timeout=5)
        worker_engine = create_engine(database_url)
        try:
            return ProjectionArchive(worker_engine, catalog).ingest_snapshot(
                snapshot,
                query=query,
                accepted_at=OBSERVED_AT,
            )
        finally:
            worker_engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(ingest, range(2)))

    assert results[0] == results[1]
    with projection_pg_engine.connect() as connection:
        assert len(connection.execute(select(ProviderPoll.poll_id)).all()) == 1
        assert len(
            connection.execute(select(ProjectionProviderSnapshot.snapshot_id)).all()
        ) == 1
        assert len(
            connection.execute(
                select(ProjectionMaterializationGeneration.generation_id)
            ).all()
        ) == 1


def test_postgres_migration_upgrades_an_existing_v37_projection_schema(
    projection_pg_engine,
):
    run_migrations(projection_pg_engine)
    catalog = StatisticCatalog.load_default()
    ProjectionArchive(projection_pg_engine, catalog).ingest_snapshot(
        _snapshot(catalog, OBSERVED_AT, "20.5"),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )
    with projection_pg_engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE projection_provider_polls "
            "DROP CONSTRAINT ck_projection_provider_poll_payload, "
            "DROP CONSTRAINT ck_projection_provider_poll_outcome, "
            "DROP COLUMN promoted, DROP COLUMN failure_reason, "
            "ALTER COLUMN retrieved_at SET NOT NULL, "
            "ALTER COLUMN generation_id SET NOT NULL"
        ))
        connection.execute(text(
            "ALTER TABLE latest_player_projections DROP COLUMN confirmed_at"
        ))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 38"))

    upgraded = run_migrations(projection_pg_engine)
    repeated = run_migrations(projection_pg_engine)

    assert upgraded.applied == ("038_projection_archive_transitions",)
    assert repeated.applied == ()
    inspector = inspect(projection_pg_engine)
    polls = {
        column["name"]: column
        for column in inspector.get_columns("projection_provider_polls")
    }
    assert polls["retrieved_at"]["nullable"] is True
    assert polls["generation_id"]["nullable"] is True
    assert {"failure_reason", "promoted"} <= set(polls)
    latest = {
        column["name"]: column
        for column in inspector.get_columns("latest_player_projections")
    }
    assert latest["confirmed_at"]["nullable"] is False
    with projection_pg_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT promoted FROM projection_provider_polls WHERE poll_id IS NOT NULL"
        )).scalar_one() is True
        assert connection.execute(text(
            "SELECT confirmed_at = observed_at FROM latest_player_projections"
        )).scalar_one() is True


def test_postgres_reader_uses_one_snapshot_across_latest_and_poll_health(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    ProjectionArchive(projection_pg_engine, catalog).ingest_snapshot(
        _snapshot(catalog, OBSERVED_AT, "20.5"),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    reader_engine = create_engine(database_url)
    writer_engine = create_engine(database_url)
    latest_selected = Event()
    writer_committed = Event()

    @event.listens_for(reader_engine, "after_cursor_execute")
    def pause_after_latest(_conn, _cursor, statement, _parameters, _context, _many):
        if "latest_player_projections" not in statement or latest_selected.is_set():
            return
        latest_selected.set()
        assert writer_committed.wait(timeout=10)

    def replace_latest():
        assert latest_selected.wait(timeout=10)
        newer = _snapshot(catalog, OBSERVED_AT + timedelta(minutes=1), "21.5")
        newer_market = replace(
            newer.markets[0],
            market_id="market-2",
            athlete=replace(newer.markets[0].athlete, canonical_id=8, name="Player 8"),
        )
        try:
            ProjectionArchive(writer_engine, catalog).ingest_snapshot(
                replace(newer, markets=(newer_market,)),
                query=query,
                accepted_at=OBSERVED_AT + timedelta(minutes=1),
            )
        finally:
            writer_committed.set()

    reader = LatestProjectionPlayerPoolReader(
        reader_engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT + timedelta(minutes=2),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(replace_latest)
        concurrent_pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
        writer.result(timeout=10)

    assert [player.canonical_player_id for player in concurrent_pool.players] == [7]
    after = LatestProjectionPlayerPoolReader(
        reader_engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT + timedelta(minutes=2),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert [player.canonical_player_id for player in after.players] == [8]
    event.remove(reader_engine, "after_cursor_execute", pause_after_latest)
    reader_engine.dispose()
    writer_engine.dispose()
