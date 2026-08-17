"""Behavioral coverage for durable projection evidence and live Player Pools."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from sqlalchemy import create_engine, select

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
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
    Selection,
    SelectionDirection,
    SelectionModifier,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
    ProjectionSelectionPlayerPoolReader,
)
from app.services.statistic_catalog import StatisticCatalog


OBSERVED_AT = datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500501"


def _reader(
    engine,
    *,
    provider: str = "dabble",
    query: NBAMarketQuery | None = None,
) -> LatestProjectionPlayerPoolReader:
    return LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(
            provider=provider,
            query=query or NBAMarketQuery(season=SEASON),
        ),
        clock=lambda: OBSERVED_AT + timedelta(minutes=10),
    )


def test_complete_snapshot_becomes_a_database_first_live_player_pool(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-archive.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(
        provider_id="pts",
        canonical_id=statistic.id,
        label="Points",
        components=statistic.components,
    )
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="market-7",
        athlete=AthleteEvidence(
            provider_id="athlete-77",
            canonical_id=2544,
            name="LeBron James",
            team=TeamEvidence(canonical_id=1610612747, abbreviation="LAL"),
        ),
        event=EventEvidence(provider_id="event-5", canonical_id=GAME_ID),
        team=TeamEvidence(canonical_id=1610612747, abbreviation="LAL"),
        statistic=evidence,
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=statistic,
            provider="dabble",
        ),
        threshold=MarketThreshold("27.5", "count", original_value="27.5"),
        status=MarketStatus.AVAILABLE,
        status_label="open",
        variant=MarketVariant.STANDARD,
        variant_label="standard",
        scoring_period=ScoringPeriod.FULL_GAME,
        scoring_period_label="full game",
        selections=(
            Selection(
                selection_id="higher-7",
                label="Higher",
                direction=SelectionDirection.HIGHER,
                status="active",
                modifiers=(
                    SelectionModifier("1.5", "multiplier", "selection", "1.5x"),
                ),
                american_price=-110,
                decimal_price="1.91",
            ),
        ),
    )
    snapshot = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            skipped_count=0,
            expected_total=1,
        ),
        retrieved_at=OBSERVED_AT,
    )

    archive = ProjectionArchive(engine, catalog)
    scope = ProjectionArchiveReadScope(
        provider="dabble", query=NBAMarketQuery(season=SEASON)
    )
    recorder = ProjectionRecordingService(archive, scope)
    with pytest.raises(ValueError, match="query is outside the configured read scope"):
        recorder.record_complete_snapshot(
            snapshot,
            query=NBAMarketQuery(
                season=SEASON,
                market_statuses=(MarketStatus.AVAILABLE,),
            ),
            accepted_at=OBSERVED_AT,
        )
    other_provider_market = replace(
        market,
        provider="prizepicks",
        market_id="prize-market-7",
        statistic_match=replace(market.statistic_match, provider="prizepicks"),
    )
    with pytest.raises(ValueError, match="provider is outside the configured read scope"):
        recorder.record_complete_snapshot(
            replace(
                snapshot,
                provider="prizepicks",
                markets=(other_provider_market,),
            ),
            query=scope.query,
            accepted_at=OBSERVED_AT,
        )
    with engine.connect() as connection:
        assert connection.execute(select(ProviderPoll)).all() == []

    result = recorder.record_complete_snapshot(
        snapshot,
        query=scope.query,
        accepted_at=OBSERVED_AT,
    )
    pool = _reader(engine).get_pool_for_game(
        season=SEASON,
        game_id=GAME_ID,
    )

    assert result.changed is True
    assert result.observation_count == 1
    archived = archive.load_source_snapshot(result.snapshot_id)
    assert archived is not None
    assert archived.markets[0].threshold == market.threshold
    assert archived.markets[0].selections == market.selections
    assert archived.markets[0].statistic_match is None
    assert pool is not None
    assert pool.players == (
        pool.players[0].__class__(
            canonical_player_id=2544,
            name="LeBron James",
            team_id=1610612747,
            market_categories=("PTS",),
            provenance={"dabble": ("PTS",)},
        ),
    )
    assert pool.team_counts == {1610612747: 1}
    assert pool.freshness == {
        "status": "fresh",
        "state": "live",
        "observed_at": OBSERVED_AT.isoformat(),
        "retrieved_at": OBSERVED_AT.isoformat(),
        "providers": {
            "dabble": {
                "status": "fresh",
                "retrieved_at": OBSERVED_AT.isoformat(),
            }
        },
    }

    replayed = archive.ingest_complete_snapshot(
        snapshot,
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )
    assert replayed == result

    repeated_retrieved_at = OBSERVED_AT.replace(minute=31)
    repeated = archive.ingest_complete_snapshot(
        replace(snapshot, retrieved_at=repeated_retrieved_at),
        query=NBAMarketQuery(
            season=SEASON,
            market_statuses=(MarketStatus.SUSPENDED, MarketStatus.AVAILABLE),
        ),
        accepted_at=OBSERVED_AT.replace(minute=32),
        poll_started_at=OBSERVED_AT.replace(minute=29),
    )
    assert repeated.changed is False
    assert repeated.snapshot_id == result.snapshot_id
    distinct_attempt = archive.ingest_complete_snapshot(
        replace(snapshot, retrieved_at=repeated_retrieved_at),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT.replace(minute=32),
        poll_started_at=OBSERVED_AT.replace(minute=30),
    )
    assert distinct_attempt.changed is False
    with engine.connect() as connection:
        polls = (
            connection.execute(
                select(ProviderPoll.__table__).order_by(
                    ProviderPoll.completed_at,
                    ProviderPoll.started_at,
                )
            )
            .mappings()
            .all()
        )
    assert [poll["outcome"] for poll in polls] == [
        "changed",
        "unchanged",
    ]
    assert [poll["observation_count"] for poll in polls] == [1, 1]
    assert polls[0]["started_at"] is None
    assert polls[1]["started_at"] == OBSERVED_AT.replace(minute=29, tzinfo=None)
    assert polls[1]["completed_at"] == OBSERVED_AT.replace(minute=32, tzinfo=None)
    assert polls[1]["retrieved_at"] == repeated_retrieved_at.replace(tzinfo=None)
    with engine.connect() as connection:
        assert len(connection.execute(select(ProjectionProviderSnapshot)).all()) == 1
        assert len(connection.execute(select(ProjectionObservation)).all()) == 1
        assert (
            len(connection.execute(select(ProjectionMaterializationGeneration)).all())
            == 1
        )
        assert len(connection.execute(select(ProjectionArchiveScopeLock)).all()) == 1

    remapped_archive = ProjectionArchive(engine, catalog)
    remapped_archive.market_categories["points"] = "PRA"
    remapped_at = repeated_retrieved_at + timedelta(seconds=1)
    remapped = remapped_archive.ingest_complete_snapshot(
        replace(snapshot, retrieved_at=remapped_at),
        query=scope.query,
        accepted_at=remapped_at,
    )
    assert remapped.changed is False
    assert remapped.materialization_outcome == "advanced"
    assert remapped.snapshot_id == result.snapshot_id
    with engine.connect() as connection:
        assert len(connection.execute(select(ProjectionProviderSnapshot)).all()) == 1
        assert len(connection.execute(select(ProjectionMaterializationGeneration)).all()) == 2
        remapped_generation = connection.execute(
            select(
                ProjectionMaterializationGeneration.materialization_checksum,
                ProjectionMaterializationGeneration.source_poll_id,
                ProviderPoll.outcome,
                ProviderPoll.retrieved_at,
            ).join(
                ProviderPoll,
                ProviderPoll.generation_id
                == ProjectionMaterializationGeneration.generation_id,
            ).where(
                ProjectionMaterializationGeneration.generation_id
                == remapped.generation_id
            )
        ).one()
        assert remapped_generation[0]
        assert remapped_generation[2] == "rematerialized"
        assert remapped_generation[3] == remapped_at.replace(tzinfo=None)
        remapped_observation = connection.execute(
            select(
                ProjectionObservation.source_poll_id,
                ProjectionObservation.observed_at,
            ).where(ProjectionObservation.generation_id == remapped.generation_id)
        ).one()
        assert remapped_observation[0] == remapped_generation[1]
        assert remapped_observation[1] == remapped_at.replace(tzinfo=None)
        assert connection.execute(
            select(LatestPlayerProjection.market_category)
        ).scalar_one() == "PRA"

    unchanged_mapping = remapped_archive.ingest_complete_snapshot(
        replace(snapshot, retrieved_at=remapped_at + timedelta(seconds=30)),
        query=scope.query,
        accepted_at=remapped_at + timedelta(seconds=30),
    )
    assert unchanged_mapping.materialization_outcome == "unchanged"
    assert unchanged_mapping.generation_id == remapped.generation_id

    unresolved_at = remapped_at + timedelta(minutes=1)
    unresolved = remapped_archive.ingest_complete_snapshot(
        replace(
            snapshot,
            markets=(replace(market, statistic_match=None),),
            retrieved_at=unresolved_at,
        ),
        query=scope.query,
        accepted_at=unresolved_at,
    )
    assert unresolved.materialization_outcome == "advanced"
    assert _reader(engine).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    ).players == ()

    resolved_at = unresolved_at + timedelta(minutes=1)
    resolved = remapped_archive.ingest_complete_snapshot(
        replace(snapshot, retrieved_at=resolved_at),
        query=scope.query,
        accepted_at=resolved_at,
    )
    assert resolved.materialization_outcome == "advanced"
    assert _reader(engine).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    ).players
    with engine.connect() as connection:
        assert len(connection.execute(select(ProjectionProviderSnapshot)).all()) == 1
        assert len(connection.execute(select(ProjectionObservation)).all()) == 4

    later_observation = OBSERVED_AT + timedelta(minutes=2)
    archive.ingest_complete_snapshot(
        replace(snapshot, retrieved_at=later_observation),
        query=NBAMarketQuery(
            season=SEASON,
            market_statuses=(MarketStatus.AVAILABLE,),
        ),
        accepted_at=later_observation,
    )
    other_provider_market = replace(
        market,
        provider="prizepicks",
        market_id="prize-market-7",
        statistic_match=replace(market.statistic_match, provider="prizepicks"),
    )
    archive.ingest_complete_snapshot(
        replace(
            snapshot,
            provider="prizepicks",
            markets=(other_provider_market,),
            retrieved_at=later_observation + timedelta(minutes=1),
        ),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=later_observation + timedelta(minutes=1),
    )
    combined = _reader(engine).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert (
        combined.freshness["providers"]["dabble"]["retrieved_at"]
        == resolved_at.isoformat()
    )
    much_later = _reader(engine).get_pool_for_game(
        season=SEASON,
        game_id=GAME_ID,
    )
    assert much_later.freshness["state"] == "live"
    assert much_later.players[0].canonical_player_id == pool.players[0].canonical_player_id


def test_non_targetable_normalized_evidence_is_archived_but_not_published(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'non-targetable.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    evidence = StatisticEvidence(provider_id="mystery", label="Fantasy Score")
    market = PlayerProjectionMarket(
        provider="underdog",
        athlete=AthleteEvidence(
            provider_id="athlete-1",
            canonical_id=1,
            name="Recorded Player",
            team=TeamEvidence(canonical_id=10),
        ),
        event=EventEvidence(provider_id="event-1", canonical_id=GAME_ID),
        team=TeamEvidence(canonical_id=10),
        statistic=evidence,
        status=MarketStatus.SUSPENDED,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    snapshot = ProviderSnapshot(
        provider="underdog",
        status=SnapshotStatus.COMPLETE,
        markets=(market,),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=1,
        ),
        retrieved_at=OBSERVED_AT,
    )
    archive = ProjectionArchive(engine, catalog)

    result = archive.ingest_complete_snapshot(
        snapshot,
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )
    pool = _reader(engine, provider="underdog").get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )

    assert result.observation_count == 1
    assert archive.load_source_snapshot(result.snapshot_id).markets == (market,)
    assert pool.players == ()
    assert pool.freshness["state"] == "missing"


def test_new_complete_snapshot_replaces_the_provider_latest_set(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'replacement.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    athlete = AthleteEvidence(
        canonical_id=7,
        name="Canonical Player",
        team=TeamEvidence(canonical_id=10),
    )
    market = PlayerProjectionMarket(
        provider="dabble",
        athlete=athlete,
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
        threshold=MarketThreshold("20.5", "count"),
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    archive.ingest_complete_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(market,),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                expected_total=1,
            ),
            retrieved_at=OBSERVED_AT,
        ),
        query=query,
        accepted_at=OBSERVED_AT,
    )

    replacement_time = OBSERVED_AT + timedelta(minutes=1)
    archive.ingest_complete_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(replace(market, status=MarketStatus.SUSPENDED),),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                expected_total=1,
            ),
            retrieved_at=replacement_time,
        ),
        query=query,
        accepted_at=replacement_time,
    )

    older_result = archive.ingest_complete_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(
                replace(
                    market,
                    threshold=MarketThreshold("19.5", "count"),
                ),
            ),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                expected_total=1,
            ),
            retrieved_at=OBSERVED_AT + timedelta(seconds=30),
        ),
        query=query,
        accepted_at=replacement_time + timedelta(minutes=1),
    )
    assert older_result.changed is True
    assert older_result.materialization_outcome == "older_not_promoted"
    assert archive.load_source_snapshot(older_result.snapshot_id) is not None

    same_time_conflict = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(
            replace(
                market,
                threshold=MarketThreshold("18.5", "count"),
            ),
        ),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=1,
        ),
        retrieved_at=replacement_time,
    )
    same_time_result = archive.ingest_complete_snapshot(
        same_time_conflict,
        query=query,
        accepted_at=replacement_time + timedelta(minutes=2),
    )
    assert same_time_result.changed is True
    assert same_time_result.materialization_outcome == "same_time_not_promoted"
    assert archive.load_source_snapshot(same_time_result.snapshot_id) is not None
    with engine.connect() as connection:
        generation_outcome = connection.execute(
            select(ProjectionMaterializationGeneration.outcome).where(
                ProjectionMaterializationGeneration.generation_id
                == same_time_result.generation_id
            )
        ).scalar_one()
    assert generation_outcome == "same_time_not_promoted"

    newest_time = replacement_time + timedelta(minutes=3)
    newest_result = archive.ingest_complete_snapshot(
        replace(
            same_time_conflict,
            markets=(replace(market, threshold=MarketThreshold("24.5", "count")),),
            retrieved_at=newest_time,
        ),
        query=query,
        accepted_at=newest_time,
    )
    later_older = archive.ingest_complete_snapshot(
        replace(
            same_time_conflict,
            markets=(replace(market, threshold=MarketThreshold("23.5", "count")),),
            retrieved_at=newest_time - timedelta(seconds=30),
        ),
        query=query,
        accepted_at=newest_time + timedelta(minutes=1),
    )
    assert later_older.materialization_outcome == "older_not_promoted"
    with engine.connect() as connection:
        latest_generations = set(
            connection.execute(
                select(LatestPlayerProjection.generation_id)
            ).scalars()
        )
        older_generation = connection.execute(
            select(ProjectionMaterializationGeneration.outcome).where(
                ProjectionMaterializationGeneration.generation_id
                == later_older.generation_id
            )
        ).scalar_one()
        older_poll = connection.execute(
            select(ProviderPoll.outcome, ProviderPoll.observation_count).where(
                ProviderPoll.snapshot_id == later_older.snapshot_id
            )
        ).one()
    assert latest_generations == {newest_result.generation_id}
    assert older_generation == "older_not_promoted"
    assert older_poll == ("changed", 1)

    pool = _reader(engine).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert pool.freshness["state"] == "live"


def test_duplicate_content_market_reference_keeps_all_evidence_and_first_latest(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicate-market.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    first = PlayerProjectionMarket(
        provider="dabble",
        athlete=AthleteEvidence(
            canonical_id=7,
            name="Canonical Player",
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
        threshold=MarketThreshold("20.5", "count"),
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    result = ProjectionArchive(engine, catalog).ingest_complete_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(
                first,
                first,
            ),
            coverage=CoverageEvidence(
                fetched_count=2,
                eligible_count=2,
                normalized_count=2,
                expected_total=2,
            ),
            retrieved_at=OBSERVED_AT,
        ),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )

    with engine.connect() as connection:
        observations = (
            connection.execute(
                select(
                    ProjectionObservation.observation_id,
                    ProjectionObservation.ordinal,
                ).order_by(ProjectionObservation.ordinal)
            )
            .mappings()
            .all()
        )
        latest_observation_id = connection.execute(
            select(LatestPlayerProjection.observation_id)
        ).scalar_one()
    assert result.observation_count == 2
    assert len(observations) == 2
    assert latest_observation_id == observations[0]["observation_id"]


def test_multi_game_pool_reports_partial_status_when_any_game_is_missing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial-games.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="one-game",
        athlete=AthleteEvidence(
            canonical_id=7,
            name="Canonical Player",
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
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )
    ProjectionArchive(engine, catalog).ingest_complete_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(market,),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                expected_total=1,
            ),
            retrieved_at=OBSERVED_AT,
        ),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )

    pool = _reader(engine).get_pool(
        season=SEASON, game_ids=(GAME_ID, "missing-game")
    )

    assert pool.freshness["status"] == "partial"
    assert pool.freshness["state"] == "live"
    assert pool.game_states[GAME_ID]["state"] == "live"
    assert pool.game_states["missing-game"] == {
        "state": "missing",
        "observed_at": None,
    }


def test_selection_reader_translates_only_missing_single_game_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'selection-missing.sqlite3'}")
    run_migrations(engine)
    reader = _reader(engine)
    selection_reader = ProjectionSelectionPlayerPoolReader(reader)

    assert (
        selection_reader.get_pool_for_game(
            season=SEASON,
            game_id=GAME_ID,
        )
        is None
    )
    assert (
        reader.get_pool_for_game(
            season=SEASON,
            game_id=GAME_ID,
        ).freshness["state"]
        == "missing"
    )


def test_projection_scope_transaction_serializes_archive_instances(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'scope-lock.sqlite3'}"
    first_engine = create_engine(database_url)
    second_engine = create_engine(database_url)
    run_migrations(first_engine)
    catalog = StatisticCatalog.load_default()
    first_archive = ProjectionArchive(first_engine, catalog)
    second_archive = ProjectionArchive(second_engine, catalog)
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def hold_scope():
        with first_archive._scope_transaction("dabble", SEASON, "query-scope"):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def enter_same_scope():
        with second_archive._scope_transaction("dabble", SEASON, "query-scope"):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(hold_scope)
        assert first_entered.wait(timeout=2)
        second = executor.submit(enter_same_scope)
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)
    assert second_entered.is_set()
