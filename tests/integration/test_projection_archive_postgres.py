"""PostgreSQL production-fence coverage for projection transitions (#105)."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, event, func, inspect, insert, select, text

from app import create_app
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    FeatureSettings,
    NBASeasonSettings,
    ProviderSettings,
    RuntimeSettings,
)
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.dependencies import build_dependencies
from app.models import Base
from app.migrations import run_migrations
from app.models.projection_archive import (
    ClosingProjectionMembership,
    ClosingProjectionSet,
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
    Selection,
    SelectionDirection,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionSelectionPlayerPoolReader,
)
from app.services.matchup import MatchupService
from app.services.matchup_selection import MatchupSelectionService
from app.services.player_game_log_repository import PlayerGameLogReadFreshness
from app.services.slate_service import SlateService
from app.services.stats_freshness_repository import StatsFreshness
from app.services.statistic_catalog import StatisticCatalog


#: A market must offer a priced side to be targetable, and what that price is
#: is not what these tests are about, so every market here offers one.
_PRICED_SELECTIONS = (
    Selection(selection_id="higher", direction=SelectionDirection.HIGHER, american_price=-110),
)


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
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")
    run_migrations(engine)
    yield engine
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS schema_migrations")
    engine.dispose()


def test_concurrent_postgres_close_and_late_materialization_keep_one_fenced_set(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    initial = _snapshot(catalog, OBSERVED_AT, "20.5")
    archive = ProjectionArchive(projection_pg_engine, catalog)
    initial_result = archive.ingest_snapshot(
        initial,
        query=query,
        accepted_at=OBSERVED_AT,
    )
    start_at = OBSERVED_AT + timedelta(minutes=5)
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    barrier = Barrier(2)

    def close_set():
        worker_engine = create_engine(database_url)
        try:
            barrier.wait(timeout=10)
            return ProjectionArchive(worker_engine, catalog).freeze_closing_projection_set(
                provider="dabble",
                query=query,
                canonical_game_id=GAME_ID,
                started_at=start_at,
                created_at=start_at,
            )
        finally:
            worker_engine.dispose()

    def late_materialization():
        worker_engine = create_engine(database_url)
        try:
            barrier.wait(timeout=10)
            return ProjectionArchive(worker_engine, catalog).ingest_snapshot(
                _snapshot(catalog, start_at - timedelta(minutes=1), "21.5"),
                query=query,
                accepted_at=start_at + timedelta(minutes=1),
            )
        finally:
            worker_engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_result, late_result = tuple(
            executor.map(lambda task: task(), (close_set, late_materialization))
        )

    with projection_pg_engine.connect() as connection:
        sets = connection.execute(select(ClosingProjectionSet)).all()
        memberships = connection.execute(select(ClosingProjectionMembership)).all()
        observations = connection.execute(
            select(ProjectionObservation.observation_id, ProjectionObservation.observed_at)
        ).all()
    assert close_result.observation_count == 1
    assert late_result.materialization_outcome == "advanced"
    assert len(sets) == 1
    assert len(memberships) == 1
    initial_observation_id = next(
        observation_id
        for observation_id, observed_at in observations
        if observed_at == initial.retrieved_at
    )
    assert memberships[0].observation_id == initial_observation_id
    assert initial_result.snapshot_id != late_result.snapshot_id


def test_two_concurrent_postgres_closers_share_one_immutable_set(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    archive = ProjectionArchive(projection_pg_engine, catalog)
    archive.ingest_snapshot(
        _snapshot(catalog, OBSERVED_AT, "20.5"),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    start_at = OBSERVED_AT + timedelta(minutes=5)
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    barrier = Barrier(2)

    def close_set(_worker):
        worker_engine = create_engine(database_url)
        try:
            barrier.wait(timeout=10)
            return ProjectionArchive(
                worker_engine, catalog
            ).freeze_closing_projection_set(
                provider="dabble",
                query=query,
                canonical_game_id=GAME_ID,
                started_at=start_at,
                created_at=start_at,
            )
        finally:
            worker_engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(close_set, range(2)))

    assert len({result.closing_set_id for result in results}) == 1
    assert sorted(result.created for result in results) == [False, True]
    with projection_pg_engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ClosingProjectionSet)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ClosingProjectionMembership)
        ).scalar_one() == 1


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
        selections=_PRICED_SELECTIONS,
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


def test_postgres_failure_attempt_fences_late_evidence_until_recovery(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    archive = ProjectionArchive(projection_pg_engine, catalog)
    archive.ingest_snapshot(
        _snapshot(catalog, OBSERVED_AT, "20.5"),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    failure_started_at = OBSERVED_AT + timedelta(minutes=19)
    failure_completed_at = OBSERVED_AT + timedelta(minutes=20)
    archive.record_failed_poll(
        provider="dabble",
        query=query,
        poll_started_at=failure_started_at,
        completed_at=failure_completed_at,
        failure_reason="access_denied",
    )
    late_retrieved_at = OBSERVED_AT + timedelta(minutes=10)
    late_changed = archive.ingest_snapshot(
        _snapshot(catalog, late_retrieved_at, "21.5"),
        query=query,
        accepted_at=failure_completed_at + timedelta(seconds=1),
    )
    late_empty = archive.ingest_snapshot(
        replace(
            _snapshot(catalog, late_retrieved_at, "21.5"),
            markets=(),
            coverage=CoverageEvidence(
                fetched_count=0,
                eligible_count=0,
                normalized_count=0,
                expected_total=0,
            ),
        ),
        query=query,
        accepted_at=failure_completed_at + timedelta(seconds=2),
    )
    recovery_at = OBSERVED_AT + timedelta(minutes=21)
    recovery = archive.ingest_snapshot(
        _snapshot(catalog, recovery_at, "22.5"),
        query=query,
        accepted_at=recovery_at,
    )

    assert late_changed.materialization_outcome == "older_not_promoted"
    assert late_empty.materialization_outcome == "older_not_promoted"
    assert recovery.materialization_outcome == "advanced"
    with projection_pg_engine.connect() as connection:
        latest = connection.execute(
            select(
                LatestPlayerProjection.generation_id,
                LatestPlayerProjection.confirmed_at,
            )
        ).one()
        polls = connection.execute(
            select(ProviderPoll.outcome, ProviderPoll.promoted).order_by(
                ProviderPoll.completed_at
            )
        ).all()
        generation_outcomes = connection.execute(
            select(ProjectionMaterializationGeneration.outcome).order_by(
                ProjectionMaterializationGeneration.created_at
            )
        ).scalars().all()
    assert latest == (recovery.generation_id, recovery_at)
    assert polls == [
        ("changed", True),
        ("failed", False),
        ("changed", False),
        ("changed", False),
        ("changed", True),
    ]
    assert generation_outcomes == [
        "advanced",
        "older_not_promoted",
        "older_not_promoted",
        "advanced",
    ]


@pytest.mark.parametrize("late_empty", [False, True], ids=("changed", "complete-empty"))
def test_postgres_committed_failure_fences_waiting_preaccepted_evidence(
    projection_pg_engine,
    late_empty,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    initial = ProjectionArchive(projection_pg_engine, catalog).ingest_snapshot(
        _snapshot(catalog, OBSERVED_AT, "20.5"),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    failure_engine = create_engine(database_url)
    ingestion_engine = create_engine(database_url)
    failure_insert_reached = Event()
    release_failure = Event()
    ingestion_fence_attempted = Event()

    @event.listens_for(failure_engine, "before_cursor_execute")
    def pause_failed_poll(_conn, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO projection_provider_polls" not in statement:
            return
        failure_insert_reached.set()
        assert release_failure.wait(timeout=10)

    @event.listens_for(ingestion_engine, "before_cursor_execute")
    def observe_ingestion_fence(
        _conn, _cursor, statement, _parameters, _context, _many
    ):
        if "projection_archive_scope_locks" in statement and "FOR UPDATE" in statement:
            ingestion_fence_attempted.set()

    failure_archive = ProjectionArchive(failure_engine, catalog)
    ingestion_archive = ProjectionArchive(ingestion_engine, catalog)
    failure_started_at = OBSERVED_AT + timedelta(minutes=19)
    failure_completed_at = OBSERVED_AT + timedelta(minutes=20)
    late_retrieved_at = OBSERVED_AT + timedelta(minutes=10)
    late_accepted_at = OBSERVED_AT + timedelta(minutes=19, seconds=30)
    late_snapshot = _snapshot(catalog, late_retrieved_at, "21.5")
    if late_empty:
        late_snapshot = replace(
            late_snapshot,
            markets=(),
            coverage=CoverageEvidence(
                fetched_count=0,
                eligible_count=0,
                normalized_count=0,
                expected_total=0,
            ),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            failed = executor.submit(
                failure_archive.record_failed_poll,
                provider="dabble",
                query=query,
                poll_started_at=failure_started_at,
                completed_at=failure_completed_at,
                failure_reason="access_denied",
            )
            assert failure_insert_reached.wait(timeout=10)
            ingested = executor.submit(
                ingestion_archive.ingest_snapshot,
                late_snapshot,
                query=query,
                accepted_at=late_accepted_at,
            )
            assert ingestion_fence_attempted.wait(timeout=10)
            release_failure.set()
            failed.result(timeout=10)
            late = ingested.result(timeout=10)

        assert late.materialization_outcome == "older_not_promoted"
        reader = LatestProjectionPlayerPoolReader(
            projection_pg_engine,
            ProjectionArchiveReadScope(provider="dabble", query=query),
            clock=lambda: failure_completed_at,
        )
        pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
        assert pool.freshness["status"] == "stale-served"
        assert [player.canonical_player_id for player in pool.players] == [7]
        with projection_pg_engine.connect() as connection:
            latest = connection.execute(
                select(
                    LatestPlayerProjection.generation_id,
                    LatestPlayerProjection.canonical_player_id,
                )
            ).one()
            promoted = connection.execute(
                select(func.count()).select_from(ProviderPoll).where(
                    ProviderPoll.promoted.is_(True)
                )
            ).scalar_one()
        assert latest == (initial.generation_id, 7)
        assert promoted == 1
    finally:
        event.remove(failure_engine, "before_cursor_execute", pause_failed_poll)
        event.remove(ingestion_engine, "before_cursor_execute", observe_ingestion_fence)
        failure_engine.dispose()
        ingestion_engine.dispose()


class _PostgresRouteEventCatalog:
    def __init__(self, clock):
        self.clock = clock

    @staticmethod
    def count_events(season):
        assert season == SEASON
        return 1

    @staticmethod
    def _event():
        return {
            "nba_game_id": GAME_ID,
            "scheduled_at": "2026-01-02T23:00:00+00:00",
            "status_text": "Scheduled",
            "status_code": 1,
            "classification": "Regular Season",
            "away_team_id": 10,
            "away_team_tricode": "AWY",
            "away_team_name": "Away",
            "home_team_id": 20,
            "home_team_tricode": "HME",
            "home_team_name": "Home",
            "away_team": {"id": 10, "tricode": "AWY", "name": "Away"},
            "home_team": {"id": 20, "tricode": "HME", "name": "Home"},
        }

    def get_freshness(self, season, *, now):
        assert season == SEASON
        assert now == self.clock()
        return {"last_success_at": OBSERVED_AT.isoformat()}

    def get_events(self, season):
        assert season == SEASON
        return (self._event(),)

    def get_events_between(self, season, start, end):
        assert season == SEASON
        assert start < end
        return (self._event(),)


class _EmptyPlayerLogs:
    @staticmethod
    def get_player_summaries(_season, _player_ids, **_kwargs):
        return {}

    @staticmethod
    def get_read_freshness(_season, **_kwargs):
        return PlayerGameLogReadFreshness("missing", None)

    @staticmethod
    def get_season_rate(_season, _player_id, **_kwargs):
        return None

    @staticmethod
    def list_h2h_rows(_season, _player_id, _opponent_team_id, **_kwargs):
        return ()

    @staticmethod
    def list_archetype_rows(_season, _peer_ids, _opponent_team_id, **_kwargs):
        return ()


class _MissingStatsFreshness:
    @staticmethod
    def get():
        return StatsFreshness(None)


class _EmptyArchetypes:
    @staticmethod
    def list_peer_ids(_player_id):
        return ()


@pytest.fixture
def authenticated_postgres_projection_routes(
    projection_pg_engine,
    authenticate,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.utils.db.get_engine", lambda _settings=None: projection_pg_engine
    )
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    route_now = [OBSERVED_AT]
    event_catalog = _PostgresRouteEventCatalog(lambda: route_now[0])
    player_logs = _EmptyPlayerLogs()

    def build_route_graph(enabled_providers=("dabble",)):
        settings = RuntimeSettings(
            environment="testing",
            auth=AuthenticationSettings(firebase_admin_disabled=False),
            cache=CacheSettings(enabled=False),
            database={"url": str(projection_pg_engine.url)},
            features=FeatureSettings(projection_archive_read_enabled=True),
            providers=ProviderSettings(
                dfs_enabled_providers=tuple(enabled_providers),
            ),
            nba=NBASeasonSettings(current_season=SEASON),
        )
        assembled = build_dependencies(settings)
        template = assembled.projection_player_pool_reader
        reader = LatestProjectionPlayerPoolReader(
            projection_pg_engine,
            template.scopes,
            clock=lambda: route_now[0],
            required_providers=template.required_providers,
        )
        dependencies = replace(
            assembled,
            projection_player_pool_reader=reader,
            slate_service=SlateService(
                event_catalog,
                settings=settings,
                player_pool=reader,
                injuries=None,
                clock=lambda: route_now[0],
            ),
            matchup_service=MatchupService(
                event_catalog=event_catalog,
                player_pool=reader,
                player_logs=player_logs,
                player_diets=None,
                team_matchups=None,
                stats_freshness=_MissingStatsFreshness(),
                settings=settings,
                injuries=None,
                clock=lambda: route_now[0],
            ),
            matchup_selection_service=MatchupSelectionService(
                event_catalog=event_catalog,
                player_pool=ProjectionSelectionPlayerPoolReader(reader),
                player_logs=player_logs,
                archetypes=_EmptyArchetypes(),
                statistic_catalog=catalog,
                settings=settings,
                publication_reader=None,
            ),
        )
        app = create_app(
            {
                "TESTING": True,
                "RUNTIME_SETTINGS": settings,
                "DEPENDENCIES": dependencies,
                "SKIP_FIREBASE_INIT": True,
                "SKIP_TABLE_CREATE": True,
            }
        )
        return SimpleNamespace(
            assembled=assembled,
            client=app.test_client(),
            reader=reader,
            settings=settings,
        )

    graph = build_route_graph()
    return SimpleNamespace(
        archive=graph.assembled.projection_archive,
        build_route_graph=build_route_graph,
        catalog=catalog,
        client=graph.client,
        headers=authenticate(),
        query=query,
        route_now=route_now,
    )


def test_postgres_projection_routes_require_authentication(
    authenticated_postgres_projection_routes,
):
    client = authenticated_postgres_projection_routes.client
    assert client.get("/api/games/slate?date=2026-01-02").status_code == 401
    assert client.get(f"/api/games/matchup?game_id={GAME_ID}").status_code == 401
    assert client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=7"
    ).status_code == 401


def test_authenticated_postgres_routes_cover_partial_unchanged_and_complete_empty(
    authenticated_postgres_projection_routes,
):
    context = authenticated_postgres_projection_routes
    archive = context.archive
    complete = _snapshot(context.catalog, OBSERVED_AT, "20.5")
    archive.ingest_snapshot(complete, query=context.query, accepted_at=OBSERVED_AT)
    headers = context.headers

    slate = context.client.get("/api/games/slate?date=2026-01-02", headers=headers)
    matchup = context.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=headers
    )
    selection = context.client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=7",
        headers=headers,
    )
    assert slate.status_code == matchup.status_code == selection.status_code == 200
    assert matchup.get_json()["freshness"]["pool"]["state"] == "live"
    assert selection.get_json()["player_id"] == 7

    partial_at = OBSERVED_AT + timedelta(minutes=1)
    partial = replace(
        _snapshot(context.catalog, partial_at, "21.5"),
        status=SnapshotStatus.PARTIAL,
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=2,
        ),
    )
    partial_result = archive.ingest_snapshot(
        partial, query=context.query, accepted_at=partial_at
    )
    unchanged_at = partial_at + timedelta(minutes=1)
    unchanged = archive.ingest_snapshot(
        replace(partial, retrieved_at=unchanged_at),
        query=context.query,
        accepted_at=unchanged_at,
    )
    context.route_now[0] = unchanged_at
    assert partial_result.materialization_outcome == "advanced"
    assert unchanged.materialization_outcome == "unchanged"
    partial_matchup = context.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=headers
    )
    assert partial_matchup.status_code == 200
    assert partial_matchup.get_json()["freshness"]["pool"]["providers"]["dabble"] == {
        "status": "fresh",
        "retrieved_at": unchanged_at.isoformat(),
    }

    empty_at = unchanged_at + timedelta(minutes=1)
    complete_empty = replace(
        complete,
        retrieved_at=empty_at,
        markets=(),
        coverage=CoverageEvidence(
            fetched_count=0,
            eligible_count=0,
            normalized_count=0,
            expected_total=0,
        ),
    )
    archive.ingest_snapshot(
        complete_empty,
        query=context.query,
        accepted_at=empty_at,
    )
    context.route_now[0] = empty_at
    empty_slate = context.client.get(
        "/api/games/slate?date=2026-01-02", headers=headers
    )
    empty_matchup = context.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=headers
    )
    empty_selection = context.client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=7",
        headers=headers,
    )
    assert empty_slate.status_code == empty_matchup.status_code == 200
    assert empty_slate.get_json()["games"][0]["projection_state"] == {
        "state": "live",
        "observed_at": empty_at.isoformat(),
    }
    assert empty_matchup.get_json()["players"] == []
    assert empty_selection.status_code == 404


def test_authenticated_routes_recover_an_unresolved_market_after_mapping_replay(
    authenticated_postgres_projection_routes,
):
    context = authenticated_postgres_projection_routes
    unresolved = _snapshot(context.catalog, OBSERVED_AT, "20.5")
    unresolved_market = replace(
        unresolved.markets[0],
        athlete=replace(
            unresolved.markets[0].athlete,
            provider_id="athlete-unresolved",
            canonical_id=None,
        ),
    )
    unresolved = replace(unresolved, markets=(unresolved_market,))
    context.archive.ingest_snapshot(
        unresolved,
        query=context.query,
        accepted_at=OBSERVED_AT,
    )

    initial_slate = context.client.get(
        "/api/games/slate?date=2026-01-02", headers=context.headers
    )
    initial_matchup = context.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=context.headers
    )
    assert initial_slate.status_code == initial_matchup.status_code == 200
    assert initial_slate.get_json()["games"][0]["away_team"][
        "targetable_player_count"
    ] == 0
    assert initial_matchup.get_json()["players"] == []

    replayed_at = OBSERVED_AT + timedelta(minutes=10)
    replay = context.archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-unresolved",
        canonical_player_id=7,
        canonical_player_name="Player 7",
        canonical_team_id=10,
        replayed_at=replayed_at,
    )
    assert replay is not None
    assert replay.changed is True
    context.route_now[0] = replayed_at

    recovered_slate = context.client.get(
        "/api/games/slate?date=2026-01-02", headers=context.headers
    )
    recovered_matchup = context.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=context.headers
    )
    recovered_selection = context.client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=7",
        headers=context.headers,
    )
    assert (
        recovered_slate.status_code
        == recovered_matchup.status_code
        == recovered_selection.status_code
        == 200
    )
    assert recovered_slate.get_json()["games"][0]["away_team"][
        "targetable_player_count"
    ] == 1
    assert recovered_matchup.get_json()["players"][0]["canonical_id"] == 7
    assert recovered_selection.get_json()["player_id"] == 7


def test_concurrent_postgres_mapping_replay_advances_one_generation(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    unresolved = _snapshot(catalog, OBSERVED_AT, "20.5")
    unresolved = replace(
        unresolved,
        markets=(
            replace(
                unresolved.markets[0],
                athlete=replace(
                    unresolved.markets[0].athlete,
                    provider_id="athlete-concurrent",
                    canonical_id=None,
                ),
            ),
        ),
    )
    ProjectionArchive(projection_pg_engine, catalog).ingest_snapshot(
        unresolved,
        query=query,
        accepted_at=OBSERVED_AT,
    )

    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    archives = [
        ProjectionArchive(create_engine(database_url), catalog)
        for _ in range(2)
    ]
    barrier = Barrier(2)
    for archive in archives:
        original = archive._replay_scope

        def synchronized_replay_scope(*, _original=original, **kwargs):
            barrier.wait(timeout=10)
            return _original(**kwargs)

        archive._replay_scope = synchronized_replay_scope

    def replay(archive):
        return archive.replay_athlete_mapping(
            provider="dabble",
            provider_athlete_id="athlete-concurrent",
            canonical_player_id=7,
            canonical_player_name="Player 7",
            canonical_team_id=10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(replay, archive) for archive in archives]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(result.changed for result in results) == [False, True]
    assert results[0].generation_id == results[1].generation_id
    with projection_pg_engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ProjectionMaterializationGeneration)
        ).scalar_one() == 2
        assert connection.execute(
            select(func.count()).select_from(LatestPlayerProjection)
        ).scalar_one() == 1
    for archive in archives:
        archive.engine.dispose()


def test_postgres_mapping_replay_serializes_with_inflight_snapshot_ingestion(
    projection_pg_engine,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)

    def unresolved_snapshot(retrieved_at, threshold):
        snapshot = _snapshot(catalog, retrieved_at, threshold)
        return replace(
            snapshot,
            markets=(
                replace(
                    snapshot.markets[0],
                    athlete=replace(
                        snapshot.markets[0].athlete,
                        provider_id="athlete-ingest-race",
                        canonical_id=None,
                    ),
                ),
            ),
        )

    ProjectionArchive(projection_pg_engine, catalog).ingest_snapshot(
        unresolved_snapshot(OBSERVED_AT, "20.5"),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    database_url = projection_pg_engine.url.render_as_string(hide_password=False)
    replay_archive = ProjectionArchive(create_engine(database_url), catalog)
    ingestion_archive = ProjectionArchive(create_engine(database_url), catalog)
    replay_lock_acquired = Event()
    ingestion_submitted = Event()
    original_scope_transaction = replay_archive._scope_transaction

    @contextmanager
    def synchronized_scope_transaction(*args, **kwargs):
        with original_scope_transaction(*args, **kwargs) as connection:
            replay_lock_acquired.set()
            assert ingestion_submitted.wait(timeout=10)
            yield connection

    replay_archive._scope_transaction = synchronized_scope_transaction
    replayed_at = OBSERVED_AT + timedelta(minutes=10)
    inflight = unresolved_snapshot(
        OBSERVED_AT + timedelta(minutes=5),
        "21.5",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_future = executor.submit(
            replay_archive.replay_athlete_mapping,
            provider="dabble",
            provider_athlete_id="athlete-ingest-race",
            canonical_player_id=7,
            canonical_player_name="Player 7",
            canonical_team_id=10,
            replayed_at=replayed_at,
        )
        assert replay_lock_acquired.wait(timeout=10)
        ingestion_submitted.set()
        ingestion_future = executor.submit(
            ingestion_archive.ingest_snapshot,
            inflight,
            query=query,
            accepted_at=replayed_at + timedelta(minutes=1),
        )
        replay = replay_future.result(timeout=20)
        ingestion = ingestion_future.result(timeout=20)

    assert replay is not None and replay.changed is True
    assert ingestion.materialization_outcome == "older_not_promoted"
    with projection_pg_engine.connect() as connection:
        recovered = connection.execute(
            select(
                LatestPlayerProjection.canonical_player_id,
                LatestPlayerProjection.generation_id,
                ProjectionArchiveScopeLock.active_generation_id,
            ).join(
                ProjectionArchiveScopeLock,
                (ProjectionArchiveScopeLock.provider == LatestPlayerProjection.provider)
                & (ProjectionArchiveScopeLock.season == LatestPlayerProjection.season)
                & (
                    ProjectionArchiveScopeLock.query_key
                    == LatestPlayerProjection.query_key
                ),
            )
        ).one()
    assert recovered[0] == 7
    assert recovered[1] == recovered[2]
    replay_archive.engine.dispose()
    ingestion_archive.engine.dispose()


def test_authenticated_postgres_routes_expire_disabled_provider_history(
    authenticated_postgres_projection_routes,
):
    context = authenticated_postgres_projection_routes
    context.archive.ingest_snapshot(
        _snapshot(context.catalog, OBSERVED_AT, "20.5"),
        query=context.query,
        accepted_at=OBSERVED_AT,
    )
    disabled = context.build_route_graph(())
    before_expiry = disabled.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=context.headers
    )
    assert before_expiry.status_code == 200
    assert before_expiry.get_json()["freshness"]["pool"]["state"] == "live"

    context.route_now[0] = OBSERVED_AT + timedelta(minutes=15, seconds=1)
    expired = disabled.client.get(
        f"/api/games/matchup?game_id={GAME_ID}", headers=context.headers
    )
    expired_selection = disabled.client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=7",
        headers=context.headers,
    )
    assert expired.status_code == 200
    assert expired.get_json()["freshness"]["pool"]["state"] == "missing"
    assert expired_selection.status_code == 503


def test_authenticated_postgres_slate_uses_attempt_chronology_and_transition_fences(
    projection_pg_engine,
    authenticate,
):
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    archive = ProjectionArchive(projection_pg_engine, catalog)
    route_now = [OBSERVED_AT]
    reader = LatestProjectionPlayerPoolReader(
        projection_pg_engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: route_now[0],
        required_providers=("dabble",),
    )

    class EventCatalog:
        @staticmethod
        def count_events(season):
            assert season == SEASON
            return 1

        @staticmethod
        def get_freshness(season, *, now):
            assert season == SEASON
            assert now == route_now[0]
            return {"last_success_at": OBSERVED_AT.isoformat()}

        @staticmethod
        def get_events_between(season, start, end):
            assert season == SEASON
            assert start < end
            return (
                {
                    "nba_game_id": GAME_ID,
                    "scheduled_at": "2026-01-02T23:00:00+00:00",
                    "status_text": "Scheduled",
                    "status_code": 1,
                    "classification": "Regular Season",
                    "away_team": {
                        "id": 10,
                        "tricode": "AWY",
                        "name": "Away",
                    },
                    "home_team": {
                        "id": 20,
                        "tricode": "HME",
                        "name": "Home",
                    },
                },
            )

    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=False),
        cache=CacheSettings(enabled=False),
        database={"url": str(projection_pg_engine.url)},
        features=FeatureSettings(projection_archive_read_enabled=True),
        providers=ProviderSettings(dfs_enabled_providers=("dabble",)),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    slate_service = SlateService(
        EventCatalog(),
        settings=settings,
        player_pool=reader,
        injuries=None,
        clock=lambda: route_now[0],
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                slate_service=slate_service,
                user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    client = app.test_client()
    headers = authenticate()

    archive.ingest_snapshot(
        _snapshot(catalog, OBSERVED_AT, "20.5"),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    initial = client.get("/api/games/slate?date=2026-01-02", headers=headers)
    assert initial.status_code == 200
    assert initial.get_json()["games"][0]["projection_state"] == {
        "state": "live",
        "observed_at": OBSERVED_AT.isoformat(),
    }
    assert initial.get_json()["games"][0]["away_team"][
        "targetable_player_count"
    ] == 1

    recovery_during_attempt_at = OBSERVED_AT + timedelta(minutes=11)
    archive.ingest_snapshot(
        _snapshot(catalog, recovery_during_attempt_at, "21.5"),
        query=query,
        accepted_at=recovery_during_attempt_at,
    )
    failure_completed_at = OBSERVED_AT + timedelta(minutes=12)
    archive.record_failed_poll(
        provider="dabble",
        query=query,
        poll_started_at=OBSERVED_AT + timedelta(minutes=10),
        completed_at=failure_completed_at,
        failure_reason="access_denied",
    )
    route_now[0] = failure_completed_at
    recovered = client.get("/api/games/slate?date=2026-01-02", headers=headers)
    assert recovered.get_json()["freshness"]["pool"]["providers"]["dabble"] == {
        "status": "fresh",
        "retrieved_at": recovery_during_attempt_at.isoformat(),
    }

    newer_failure_completed_at = OBSERVED_AT + timedelta(minutes=20)
    archive.record_failed_poll(
        provider="dabble",
        query=query,
        poll_started_at=OBSERVED_AT + timedelta(minutes=19),
        completed_at=newer_failure_completed_at,
        failure_reason="access_denied",
    )
    late_retrieved_at = OBSERVED_AT + timedelta(minutes=15)
    late_changed = archive.ingest_snapshot(
        _snapshot(catalog, late_retrieved_at, "22.5"),
        query=query,
        accepted_at=newer_failure_completed_at + timedelta(seconds=1),
    )
    late_empty = archive.ingest_snapshot(
        replace(
            _snapshot(catalog, late_retrieved_at, "22.5"),
            markets=(),
            coverage=CoverageEvidence(
                fetched_count=0,
                eligible_count=0,
                normalized_count=0,
                expected_total=0,
            ),
        ),
        query=query,
        accepted_at=newer_failure_completed_at + timedelta(seconds=2),
    )
    route_now[0] = newer_failure_completed_at + timedelta(seconds=2)
    stale = client.get("/api/games/slate?date=2026-01-02", headers=headers)
    assert late_changed.materialization_outcome == "older_not_promoted"
    assert late_empty.materialization_outcome == "older_not_promoted"
    assert stale.get_json()["freshness"]["pool"]["providers"]["dabble"][
        "status"
    ] == "stale-served"
    assert stale.get_json()["games"][0]["projection_state"][
        "observed_at"
    ] == recovery_during_attempt_at.isoformat()
    assert stale.get_json()["games"][0]["away_team"][
        "targetable_player_count"
    ] == 1

    final_recovery_at = OBSERVED_AT + timedelta(minutes=21)
    archive.ingest_snapshot(
        _snapshot(catalog, final_recovery_at, "23.5"),
        query=query,
        accepted_at=final_recovery_at,
    )
    route_now[0] = final_recovery_at
    final = client.get("/api/games/slate?date=2026-01-02", headers=headers)
    assert final.get_json()["freshness"]["pool"]["providers"]["dabble"] == {
        "status": "fresh",
        "retrieved_at": final_recovery_at.isoformat(),
    }
    assert final.get_json()["games"][0]["projection_state"][
        "observed_at"
    ] == final_recovery_at.isoformat()


def test_postgres_migration_upgrades_an_existing_v40_projection_schema(
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
    historical_poll_id = "v40_historical_postgres_poll"
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
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 41"))

    upgraded = run_migrations(projection_pg_engine)
    repeated = run_migrations(projection_pg_engine)

    assert upgraded.applied == ("041_projection_archive_transitions",)
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
        assert latest_times.observed_at == unchanged_at
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
        _two_market_snapshot(
            catalog,
            OBSERVED_AT,
            player_ids=(7, 8),
            thresholds=("20.5", "10.5"),
        ),
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

    def record_failure():
        assert latest_selected.wait(timeout=10)
        try:
            ProjectionArchive(writer_engine, catalog).record_failed_poll(
                provider="dabble",
                query=query,
                poll_started_at=OBSERVED_AT + timedelta(minutes=1),
                completed_at=OBSERVED_AT + timedelta(minutes=2),
                failure_reason="access_denied",
            )
        finally:
            writer_committed.set()

    reader = LatestProjectionPlayerPoolReader(
        reader_engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT + timedelta(minutes=2),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            writer = executor.submit(record_failure)
            concurrent_pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
            writer.result(timeout=10)

        assert [
            player.canonical_player_id for player in concurrent_pool.players
        ] == [7, 8]
        assert concurrent_pool.freshness["status"] == "fresh"

        after = LatestProjectionPlayerPoolReader(
            reader_engine,
            ProjectionArchiveReadScope(provider="dabble", query=query),
            clock=lambda: OBSERVED_AT + timedelta(minutes=2),
        ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
        assert [player.canonical_player_id for player in after.players] == [7, 8]
        assert after.freshness["status"] == "stale-served"
        with reader_engine.connect() as connection:
            latest = connection.execute(
                select(
                    LatestPlayerProjection.generation_id,
                    LatestPlayerProjection.canonical_player_id,
                    LatestPlayerProjection.market_reference,
                ).order_by(LatestPlayerProjection.canonical_player_id)
            ).all()
        assert len({row.generation_id for row in latest}) == 1
        assert [row.canonical_player_id for row in latest] == [7, 8]
        assert len({row.market_reference for row in latest}) == 2
    finally:
        event.remove(reader_engine, "after_cursor_execute", pause_after_latest)
        reader_engine.dispose()
        writer_engine.dispose()
