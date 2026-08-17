"""PostgreSQL production-fence coverage for projection transitions (#105)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, event, func, inspect, insert, select, text

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.models import Base
from app.migrations import run_migrations
from app.models.projection_archive import (
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
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


def _two_market_snapshot(catalog, retrieved_at, *, player_ids, thresholds):
    base = _snapshot(catalog, retrieved_at, thresholds[0])
    markets = tuple(
        replace(
            base.markets[0],
            market_id=f"market-{player_id}",
            athlete=replace(
                base.markets[0].athlete,
                canonical_id=player_id,
                name=f"Player {player_id}",
            ),
            threshold=MarketThreshold(threshold, "count"),
        )
        for player_id, threshold in zip(player_ids, thresholds, strict=True)
    )
    return replace(
        base,
        markets=markets,
        coverage=CoverageEvidence(
            fetched_count=2,
            eligible_count=2,
            normalized_count=2,
            expected_total=2,
        ),
    )


def test_concurrent_postgres_ingestion_has_one_temporal_winner_and_no_mixed_generation(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    older = _two_market_snapshot(
        catalog,
        OBSERVED_AT,
        player_ids=(7, 8),
        thresholds=("20.5", "10.5"),
    )
    newer = _two_market_snapshot(
        catalog,
        OBSERVED_AT + timedelta(minutes=1),
        player_ids=(9, 10),
        thresholds=("21.5", "11.5"),
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


@pytest.mark.parametrize("winner_index", (0, 1))
def test_first_fenced_equal_time_postgres_writer_is_the_only_promoted_generation(
    projection_pg_engine,
    winner_index,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    snapshots = (
        _two_market_snapshot(
            catalog,
            OBSERVED_AT,
            player_ids=(7, 8),
            thresholds=("20.5", "10.5"),
        ),
        _two_market_snapshot(
            catalog,
            OBSERVED_AT,
            player_ids=(9, 10),
            thresholds=("21.5", "11.5"),
        ),
    )
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    winner_engine = create_engine(database_url)
    loser_engine = create_engine(database_url)
    winner_locked = Event()
    loser_attempting = Event()
    scope = ProjectionArchiveReadScope(provider="dabble", query=query)
    with projection_pg_engine.begin() as connection:
        connection.execute(
            insert(ProjectionArchiveScopeLock).values(
                provider="dabble",
                season=SEASON,
                query_key=scope.query_key,
            )
        )

    @event.listens_for(winner_engine, "after_cursor_execute")
    def hold_winner_lock(_conn, _cursor, statement, _parameters, _context, _many):
        if "projection_archive_scope_locks" in statement and "FOR UPDATE" in statement:
            winner_locked.set()
            assert loser_attempting.wait(timeout=10)

    @event.listens_for(loser_engine, "before_cursor_execute")
    def observe_loser_attempt(_conn, _cursor, statement, _parameters, _context, _many):
        if "projection_archive_scope_locks" in statement and "FOR UPDATE" in statement:
            loser_attempting.set()

    def ingest(engine, snapshot):
        return ProjectionArchive(engine, catalog).ingest_snapshot(
            snapshot,
            query=query,
            accepted_at=OBSERVED_AT,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(ingest, winner_engine, snapshots[winner_index])
        assert winner_locked.wait(timeout=10)
        loser = executor.submit(ingest, loser_engine, snapshots[1 - winner_index])
        results = (winner.result(timeout=10), loser.result(timeout=10))

    with projection_pg_engine.connect() as connection:
        latest = connection.execute(
            select(
                LatestPlayerProjection.generation_id,
                LatestPlayerProjection.canonical_player_id,
                LatestPlayerProjection.market_reference,
            ).order_by(LatestPlayerProjection.canonical_player_id)
        ).all()
        promoted = connection.execute(
            select(ProviderPoll.promoted).order_by(ProviderPoll.poll_id)
        ).scalars().all()
        generation_outcomes = connection.execute(
            select(ProjectionMaterializationGeneration.outcome)
        ).scalars().all()
    assert len({row.generation_id for row in latest}) == 1
    assert tuple(row.canonical_player_id for row in latest) == (
        (7, 8) if winner_index == 0 else (9, 10)
    )
    assert len({row.market_reference for row in latest}) == 2
    assert tuple(result.materialization_outcome for result in results) == (
        "advanced",
        "same_time_not_promoted",
    )
    assert sorted(promoted) == [False, True]
    assert sorted(generation_outcomes) == ["advanced", "same_time_not_promoted"]
    event.remove(winner_engine, "after_cursor_execute", hold_winner_lock)
    event.remove(loser_engine, "before_cursor_execute", observe_loser_attempt)
    winner_engine.dispose()
    loser_engine.dispose()


@pytest.mark.parametrize("winner_category", ("PTS", "PRA"))
def test_same_evidence_postgres_mapping_change_replays_the_first_winner(
    projection_pg_engine,
    winner_category,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    snapshot = _two_market_snapshot(
        catalog,
        OBSERVED_AT,
        player_ids=(7, 8),
        thresholds=("20.5", "10.5"),
    )
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    winner_engine = create_engine(database_url)
    loser_engine = create_engine(database_url)
    winner_locked = Event()
    loser_attempting = Event()
    scope = ProjectionArchiveReadScope(provider="dabble", query=query)
    with projection_pg_engine.begin() as connection:
        connection.execute(
            insert(ProjectionArchiveScopeLock).values(
                provider="dabble",
                season=SEASON,
                query_key=scope.query_key,
            )
        )

    @event.listens_for(winner_engine, "after_cursor_execute")
    def hold_winner_lock(_conn, _cursor, statement, _parameters, _context, _many):
        if "projection_archive_scope_locks" in statement and "FOR UPDATE" in statement:
            winner_locked.set()
            assert loser_attempting.wait(timeout=10)

    @event.listens_for(loser_engine, "before_cursor_execute")
    def observe_loser_attempt(_conn, _cursor, statement, _parameters, _context, _many):
        if "projection_archive_scope_locks" in statement and "FOR UPDATE" in statement:
            loser_attempting.set()

    winner_archive = ProjectionArchive(winner_engine, catalog)
    loser_archive = ProjectionArchive(loser_engine, catalog)
    winner_archive.market_categories["points"] = winner_category
    loser_archive.market_categories["points"] = (
        "PRA" if winner_category == "PTS" else "PTS"
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(
            winner_archive.ingest_snapshot,
            snapshot,
            query=query,
            accepted_at=OBSERVED_AT,
        )
        assert winner_locked.wait(timeout=10)
        loser = executor.submit(
            loser_archive.ingest_snapshot,
            snapshot,
            query=query,
            accepted_at=OBSERVED_AT + timedelta(seconds=1),
        )
        results = (winner.result(timeout=10), loser.result(timeout=10))

    with projection_pg_engine.connect() as connection:
        latest = connection.execute(
            select(
                LatestPlayerProjection.generation_id,
                LatestPlayerProjection.market_category,
            )
        ).all()
        polls = connection.execute(
            select(ProviderPoll.outcome, ProviderPoll.promoted).order_by(
                ProviderPoll.completed_at
            )
        ).all()
        generation_outcomes = connection.execute(
            select(ProjectionMaterializationGeneration.outcome)
        ).scalars().all()
    assert len(latest) == 2
    assert len({row.generation_id for row in latest}) == 1
    assert {row.market_category for row in latest} == {winner_category}
    assert results[1] == results[0]
    assert polls == [("changed", True)]
    assert generation_outcomes == ["advanced"]
    event.remove(winner_engine, "after_cursor_execute", hold_winner_lock)
    event.remove(loser_engine, "before_cursor_execute", observe_loser_attempt)
    winner_engine.dispose()
    loser_engine.dispose()


def test_concurrent_duplicate_postgres_ingestion_is_idempotent(projection_pg_engine):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    snapshot = _two_market_snapshot(
        catalog,
        OBSERVED_AT,
        player_ids=(7, 8),
        thresholds=("20.5", "10.5"),
    )
    barrier = Barrier(2)
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)

    def ingest(index):
        barrier.wait(timeout=5)
        worker_engine = create_engine(database_url)
        try:
            return ProjectionArchive(worker_engine, catalog).ingest_snapshot(
                (
                    snapshot
                    if index == 0
                    else replace(snapshot, markets=tuple(reversed(snapshot.markets)))
                ),
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
        assert len(
            connection.execute(select(ProjectionObservation.observation_id)).all()
        ) == 2
        latest = connection.execute(
            select(LatestPlayerProjection.generation_id)
        ).scalars().all()
        assert len(latest) == 2
        assert len(set(latest)) == 1


def test_delayed_retry_postgres_worker_reuses_the_exact_evidence_poll(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    snapshot = _snapshot(catalog, OBSERVED_AT, "20.5")
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    first_engine = create_engine(database_url)
    retry_engine = create_engine(database_url)
    try:
        first = ProjectionArchive(first_engine, catalog).ingest_snapshot(
            snapshot,
            query=query,
            accepted_at=OBSERVED_AT,
        )
        retry_archive = ProjectionArchive(retry_engine, catalog)
        retry_archive.market_categories["points"] = "PRA"
        retry = retry_archive.ingest_snapshot(
            snapshot,
            query=query,
            accepted_at=OBSERVED_AT + timedelta(minutes=10),
            poll_started_at=OBSERVED_AT + timedelta(minutes=9),
        )
    finally:
        first_engine.dispose()
        retry_engine.dispose()

    assert retry == first
    with projection_pg_engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ProviderPoll)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ProjectionProviderSnapshot)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ProjectionMaterializationGeneration)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ProjectionObservation)
        ).scalar_one() == 1
        latest = connection.execute(
            select(
                LatestPlayerProjection.generation_id,
                LatestPlayerProjection.market_category,
            )
        ).one()
    assert latest == (first.generation_id, "PTS")


def test_postgres_migration_upgrades_an_existing_v37_projection_schema(
    projection_pg_engine,
):
    run_migrations(projection_pg_engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(projection_pg_engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    winner = _two_market_snapshot(
        catalog,
        OBSERVED_AT,
        player_ids=(7, 8),
        thresholds=("20.5", "10.5"),
    )
    older = _two_market_snapshot(
        catalog,
        OBSERVED_AT - timedelta(minutes=1),
        player_ids=(11, 12),
        thresholds=("19.5", "9.5"),
    )
    same_time = _two_market_snapshot(
        catalog,
        OBSERVED_AT,
        player_ids=(9, 10),
        thresholds=("21.5", "11.5"),
    )
    winner_result = archive.ingest_snapshot(
        winner,
        query=query,
        accepted_at=OBSERVED_AT,
    )
    archive.ingest_snapshot(
        older,
        query=query,
        accepted_at=OBSERVED_AT + timedelta(minutes=2),
    )
    archive.ingest_snapshot(
        same_time,
        query=query,
        accepted_at=OBSERVED_AT + timedelta(minutes=3),
    )
    unchanged_at = OBSERVED_AT + timedelta(minutes=1)
    archive.ingest_snapshot(
        replace(winner, retrieved_at=unchanged_at),
        query=query,
        accepted_at=OBSERVED_AT + timedelta(minutes=4),
    )
    archive.ingest_snapshot(
        replace(winner, retrieved_at=OBSERVED_AT + timedelta(seconds=30)),
        query=query,
        accepted_at=OBSERVED_AT + timedelta(minutes=5),
    )
    historical_poll_id = "v37_historical_postgres_poll"
    with projection_pg_engine.begin() as connection:
        winner_poll_id = connection.execute(
            select(ProjectionMaterializationGeneration.source_poll_id).where(
                ProjectionMaterializationGeneration.generation_id
                == winner_result.generation_id
            )
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO projection_provider_polls "
                "(poll_id, provider, season, query_key, started_at, completed_at, "
                "retrieved_at, outcome, promoted, failure_reason, snapshot_id, "
                "generation_id, observation_count) "
                "SELECT :historical, provider, season, query_key, started_at, "
                "completed_at, retrieved_at, outcome, promoted, failure_reason, "
                "snapshot_id, generation_id, observation_count "
                "FROM projection_provider_polls WHERE poll_id = :current"
            ),
            {"historical": historical_poll_id, "current": winner_poll_id},
        )
        connection.execute(
            text(
                "UPDATE projection_materialization_generations "
                "SET source_poll_id = :historical WHERE source_poll_id = :current"
            ),
            {"historical": historical_poll_id, "current": winner_poll_id},
        )
        connection.execute(
            text(
                "UPDATE projection_observations SET source_poll_id = :historical "
                "WHERE source_poll_id = :current"
            ),
            {"historical": historical_poll_id, "current": winner_poll_id},
        )
        connection.execute(
            text("DELETE FROM projection_provider_polls WHERE poll_id = :current"),
            {"current": winner_poll_id},
        )
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
        migrated_polls = connection.execute(text(
            "SELECT poll.outcome, poll.promoted, generation.outcome "
            "FROM projection_provider_polls AS poll "
            "JOIN projection_materialization_generations AS generation "
            "ON generation.generation_id = poll.generation_id "
            "ORDER BY poll.completed_at"
        )).all()
        assert migrated_polls == [
            ("changed", True, "advanced"),
            ("changed", False, "older_not_promoted"),
            ("changed", False, "same_time_not_promoted"),
            ("unchanged", True, "advanced"),
            ("unchanged", False, "advanced"),
        ]
        latest_times = connection.execute(text(
            "SELECT DISTINCT observed_at, confirmed_at FROM latest_player_projections"
        )).one()
        assert latest_times.observed_at == OBSERVED_AT
        assert latest_times.confirmed_at == unchanged_at
        before_replay = tuple(
            connection.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (
                ProviderPoll,
                ProjectionProviderSnapshot,
                ProjectionMaterializationGeneration,
                ProjectionObservation,
                LatestPlayerProjection,
            )
        )

    replay = ProjectionArchive(projection_pg_engine, catalog).ingest_snapshot(
        winner,
        query=query,
        accepted_at=OBSERVED_AT + timedelta(minutes=10),
        poll_started_at=OBSERVED_AT + timedelta(minutes=9),
    )
    assert replay == winner_result
    with projection_pg_engine.connect() as connection:
        after_replay = tuple(
            connection.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (
                ProviderPoll,
                ProjectionProviderSnapshot,
                ProjectionMaterializationGeneration,
                ProjectionObservation,
                LatestPlayerProjection,
            )
        )
        assert connection.execute(
            select(ProviderPoll.poll_id).where(
                ProviderPoll.poll_id == historical_poll_id
            )
        ).scalar_one() == historical_poll_id
    assert after_replay == before_replay


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
        newer = _two_market_snapshot(
            catalog,
            OBSERVED_AT + timedelta(minutes=1),
            player_ids=(9, 10),
            thresholds=("21.5", "11.5"),
        )
        try:
            ProjectionArchive(writer_engine, catalog).ingest_snapshot(
                newer,
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

    assert [player.canonical_player_id for player in concurrent_pool.players] == [7, 8]
    after = LatestProjectionPlayerPoolReader(
        reader_engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT + timedelta(minutes=2),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert [player.canonical_player_id for player in after.players] == [9, 10]
    with reader_engine.connect() as connection:
        latest = connection.execute(
            select(
                LatestPlayerProjection.generation_id,
                LatestPlayerProjection.canonical_player_id,
                LatestPlayerProjection.market_reference,
            ).order_by(LatestPlayerProjection.canonical_player_id)
        ).all()
    assert len({row.generation_id for row in latest}) == 1
    assert [row.canonical_player_id for row in latest] == [9, 10]
    assert len({row.market_reference for row in latest}) == 2
    event.remove(reader_engine, "after_cursor_execute", pause_after_latest)
    reader_engine.dispose()
    writer_engine.dispose()
