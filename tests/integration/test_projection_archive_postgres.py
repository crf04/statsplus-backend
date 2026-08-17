"""PostgreSQL production-fence coverage for projection transitions (#105)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.models import Base
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
from app.services.projection_archive import ProjectionArchive
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
