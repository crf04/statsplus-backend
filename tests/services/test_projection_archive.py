"""Behavioral coverage for durable projection evidence and live Player Pools."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event, func, select

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
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


def _closing_snapshot(catalog, retrieved_at, threshold="27.5"):
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id=f"market-{threshold}",
        athlete=AthleteEvidence(
            canonical_id=2544,
            name="LeBron James",
            team=TeamEvidence(canonical_id=1610612747, abbreviation="LAL"),
        ),
        event=EventEvidence(canonical_id=GAME_ID),
        team=TeamEvidence(canonical_id=1610612747, abbreviation="LAL"),
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


def test_recorder_authorizes_exactly_enabled_scopes_including_empty(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-recording-auth.sqlite3'}")
    run_migrations(engine)
    archive = ProjectionArchive(engine, StatisticCatalog.load_default())
    query = NBAMarketQuery(season=SEASON)
    dabble_scope = ProjectionArchiveReadScope(provider="dabble", query=query)
    prizepicks_scope = ProjectionArchiveReadScope(provider="prizepicks", query=query)

    def empty_snapshot(provider: str, retrieved_at: datetime) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider=provider,
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(
                fetched_count=0,
                eligible_count=0,
                normalized_count=0,
                expected_total=0,
            ),
            retrieved_at=retrieved_at,
        )

    disabled = ProjectionRecordingService(
        archive,
        (),
        default_scope=dabble_scope,
    )
    for provider in ("dabble", "prizepicks"):
        with pytest.raises(ValueError, match="outside the configured recording scope"):
            disabled.record_complete_snapshot(
                empty_snapshot(provider, OBSERVED_AT),
                query=query,
                accepted_at=OBSERVED_AT,
            )
        with pytest.raises(ValueError, match="outside the configured recording scope"):
            disabled.record_failed_poll(
                provider=provider,
                query=query,
                completed_at=OBSERVED_AT,
                failure_reason="access_denied",
            )

    enabled = ProjectionRecordingService(
        archive,
        (dabble_scope,),
        default_scope=prizepicks_scope,
    )
    accepted = enabled.record_complete_snapshot(
        empty_snapshot("dabble", OBSERVED_AT),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    with pytest.raises(ValueError, match="outside the configured recording scope"):
        enabled.record_complete_snapshot(
            empty_snapshot("prizepicks", OBSERVED_AT + timedelta(minutes=1)),
            query=query,
            accepted_at=OBSERVED_AT + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="outside the configured recording scope"):
        enabled.record_failed_poll(
            provider="prizepicks",
            query=query,
            completed_at=OBSERVED_AT + timedelta(minutes=1),
            failure_reason="access_denied",
        )

    assert disabled.scopes == {}
    assert disabled.default_scope is dabble_scope
    assert disabled.scope is dabble_scope
    assert set(enabled.scopes) == {"dabble"}
    assert enabled.default_scope is prizepicks_scope
    assert enabled.scope is prizepicks_scope
    assert accepted.changed is True
    with engine.connect() as connection:
        assert len(connection.execute(select(ProviderPoll)).all()) == 1
        assert connection.execute(select(LatestPlayerProjection)).all() == []


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
    with pytest.raises(ValueError, match="query is outside the configured recording scope"):
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
    with pytest.raises(ValueError, match="provider is outside the configured recording scope"):
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


def test_closing_set_freezes_the_last_pre_start_observation_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-closing.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    initial_at = OBSERVED_AT
    start_at = OBSERVED_AT + timedelta(minutes=5)
    archive.ingest_complete_snapshot(
        _closing_snapshot(catalog, initial_at),
        query=query,
        accepted_at=initial_at,
    )

    late = archive.ingest_complete_snapshot(
        _closing_snapshot(catalog, start_at - timedelta(minutes=1), "28.5"),
        query=query,
        accepted_at=start_at + timedelta(minutes=1),
    )
    first = archive.freeze_closing_projection_set(
        provider="dabble",
        query=query,
        canonical_game_id=GAME_ID,
        started_at=start_at,
        created_at=start_at + timedelta(minutes=1),
    )
    repeated = archive.freeze_closing_projection_set(
        provider="dabble",
        query=query,
        canonical_game_id=GAME_ID,
        started_at=start_at + timedelta(minutes=1),
        created_at=start_at + timedelta(minutes=1),
    )

    assert first.created is True
    assert first.observation_count == 1
    assert late.materialization_outcome == "advanced"
    assert repeated.created is False
    assert repeated.closing_set_id == first.closing_set_id
    with engine.connect() as connection:
        membership = connection.execute(
            select(
                ClosingProjectionMembership.observation_id,
                ProjectionObservation.observed_at,
            )
            .join(
                ProjectionObservation,
                ProjectionObservation.observation_id
                == ClosingProjectionMembership.observation_id,
            )
            .where(
                ClosingProjectionMembership.closing_set_id == first.closing_set_id
            )
        ).first()
        latest_observation = connection.execute(
            select(LatestPlayerProjection.observation_id)
        ).scalar_one()
        snapshot_count = connection.execute(
            select(func.count()).select_from(ProjectionProviderSnapshot)
        ).scalar_one()
        observation_count = connection.execute(
            select(func.count()).select_from(ProjectionObservation)
        ).scalar_one()
        set_count = connection.execute(
            select(func.count()).select_from(ClosingProjectionSet)
        ).scalar_one()
    assert membership is not None
    assert membership.observed_at == initial_at.replace(tzinfo=None)
    assert latest_observation != membership.observation_id
    assert snapshot_count == 2
    assert observation_count == 2
    assert set_count == 1


def test_closing_replay_joins_observations_without_generation_id_bind_list(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-closing-join.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    for offset, threshold in ((0, "27.5"), (1, "28.5")):
        observed_at = OBSERVED_AT + timedelta(minutes=offset)
        archive.ingest_complete_snapshot(
            _closing_snapshot(catalog, observed_at, threshold),
            query=query,
            accepted_at=observed_at,
        )

    statements = []

    def capture_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        archive.freeze_closing_projection_set(
            provider="dabble",
            query=query,
            canonical_game_id=GAME_ID,
            started_at=OBSERVED_AT + timedelta(minutes=2),
            created_at=OBSERVED_AT + timedelta(minutes=2),
        )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture_statement)

    observation_reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "projection_observations" in statement
    ]
    assert observation_reads
    assert all("generation_id IN" not in statement for statement in observation_reads)
    assert any(
        "projection_materialization_generations.retrieved_at >" in statement
        for statement in observation_reads
    )


def test_started_reader_uses_closing_state_for_actual_start_and_final_status(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-status.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    observed_at = OBSERVED_AT
    archive.ingest_complete_snapshot(
        _closing_snapshot(catalog, observed_at),
        query=query,
        accepted_at=observed_at,
    )
    event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": (OBSERVED_AT + timedelta(minutes=5)).isoformat(),
        "status_text": "Delayed",
        "status_code": 1,
        "first_observed_started_at": None,
    }

    class ScopedEvents:
        def get_events_by_ids(self, _season, game_ids):
            return (event,) if GAME_ID in game_ids else ()

    now = [OBSERVED_AT + timedelta(minutes=4)]
    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: now[0],
        event_reader=ScopedEvents(),
    )

    delayed = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert delayed.freshness["state"] == "live"
    post_start = archive.ingest_complete_snapshot(
        replace(
            _closing_snapshot(
                catalog,
                OBSERVED_AT + timedelta(minutes=6),
            ),
            markets=(),
            coverage=CoverageEvidence(expected_total=0),
        ),
        query=query,
        accepted_at=OBSERVED_AT + timedelta(minutes=6),
    )
    assert post_start.materialization_outcome == "advanced"
    event.update(
        status_text="Q1",
        status_code=2,
        first_observed_started_at=(
            OBSERVED_AT + timedelta(minutes=5)
        ).isoformat(),
    )
    now[0] = OBSERVED_AT + timedelta(minutes=5)
    ProjectionRecordingService(
        archive,
        ProjectionArchiveReadScope(provider="dabble", query=query),
    ).freeze_closing_projection_sets(
        events=(event,),
        query=query,
        created_at=now[0],
    )
    started = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert started.freshness["state"] == "closing"
    assert started.freshness["providers"]["dabble"]["status"] == "closing"
    assert started.game_states[GAME_ID]["state"] == "closing"
    assert started.players[0].canonical_player_id == 2544
    observed = started.freshness["observed_at"]

    event.update(status_text="Final", status_code=3)
    now[0] = OBSERVED_AT + timedelta(hours=8)
    final = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert final.freshness["state"] == "closing"
    assert final.freshness["observed_at"] == observed
    assert [player.canonical_player_id for player in final.players] == [2544]


def test_closing_writer_falls_back_to_schedule_for_legacy_started_event(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-start-fallback.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    scheduled_at = OBSERVED_AT + timedelta(minutes=5)
    archive.ingest_complete_snapshot(
        _closing_snapshot(catalog, OBSERVED_AT),
        query=query,
        accepted_at=OBSERVED_AT,
    )

    results = ProjectionRecordingService(
        archive,
        ProjectionArchiveReadScope(provider="dabble", query=query),
    ).freeze_closing_projection_sets(
        events=(
            {
                "nba_game_id": GAME_ID,
                "scheduled_at": scheduled_at.isoformat(),
                "status_text": "Final",
                "status_code": 3,
                "first_observed_started_at": None,
            },
        ),
        query=query,
        created_at=scheduled_at + timedelta(hours=2),
    )

    assert len(results) == 1
    assert results[0].started_at == scheduled_at


def test_started_reader_reports_missing_without_synthesizing_a_player(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-closing-missing.sqlite3'}")
    run_migrations(engine)
    query = NBAMarketQuery(season=SEASON)

    class ScopedEvents:
        def get_events_by_ids(self, _season, game_ids):
            return (
                {
                    "nba_game_id": GAME_ID,
                    "scheduled_at": OBSERVED_AT.isoformat(),
                    "status_text": "Final",
                    "status_code": 3,
                    "first_observed_started_at": OBSERVED_AT.isoformat(),
                },
            ) if GAME_ID in game_ids else ()

    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT,
        event_reader=ScopedEvents(),
    )

    pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.players == ()
    assert pool.team_counts == {}
    assert pool.freshness["state"] == "missing"
    assert pool.game_states[GAME_ID] == {"state": "missing", "observed_at": None}
    selection_pool = ProjectionSelectionPlayerPoolReader(reader).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert selection_pool is not None
    assert selection_pool.players == ()


def test_started_reader_is_read_only_and_uses_scoped_event_lookup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-read-only.sqlite3'}")
    run_migrations(engine)
    query = NBAMarketQuery(season=SEASON)

    class ScopedEvents:
        calls = []

        def get_events_by_ids(self, season, game_ids):
            self.calls.append((season, tuple(game_ids)))
            assert season == SEASON
            assert tuple(game_ids) == (GAME_ID,)
            return (
                {
                    "nba_game_id": GAME_ID,
                    "scheduled_at": OBSERVED_AT.isoformat(),
                    "status_text": "Q1",
                    "status_code": 2,
                    "first_observed_started_at": (
                        OBSERVED_AT + timedelta(minutes=5)
                    ).isoformat(),
                },
            )

    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT + timedelta(minutes=10),
        event_reader=ScopedEvents(),
    )

    pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.freshness["state"] == "missing"
    assert reader.event_reader.calls == [(SEASON, (GAME_ID,))]
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ClosingProjectionSet)
        ).scalar_one() == 0

    reader.event_reader.calls.clear()
    assert ProjectionSelectionPlayerPoolReader(reader).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    ) is not None
    assert reader.event_reader.calls == [(SEASON, (GAME_ID,))]


def test_mixed_live_closing_and_missing_games_keep_state_specific_freshness(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-mixed-state.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    live_game_id = "0022500502"
    missing_game_id = "0022500503"
    start_at = OBSERVED_AT + timedelta(minutes=5)
    live_at = OBSERVED_AT + timedelta(minutes=9)
    archive.ingest_complete_snapshot(
        _closing_snapshot(catalog, OBSERVED_AT),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    closing_event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": start_at.isoformat(),
        "status_text": "Q1",
        "status_code": 2,
        "first_observed_started_at": start_at.isoformat(),
    }
    ProjectionRecordingService(
        archive,
        ProjectionArchiveReadScope(provider="dabble", query=query),
    ).freeze_closing_projection_sets(
        events=(closing_event,),
        query=query,
        created_at=start_at,
    )
    live_source = _closing_snapshot(catalog, live_at, "30.5")
    live_market = replace(
        live_source.markets[0],
        market_id="live-market",
        event=EventEvidence(canonical_id=live_game_id),
    )
    archive.ingest_complete_snapshot(
        replace(live_source, markets=(live_market,)),
        query=query,
        accepted_at=live_at,
    )
    events = (
        closing_event,
        {
            "nba_game_id": live_game_id,
            "scheduled_at": (OBSERVED_AT + timedelta(hours=1)).isoformat(),
            "status_text": "Scheduled",
            "status_code": 1,
            "first_observed_started_at": None,
        },
        {
            "nba_game_id": missing_game_id,
            "scheduled_at": start_at.isoformat(),
            "status_text": "Q1",
            "status_code": 2,
            "first_observed_started_at": start_at.isoformat(),
        },
    )

    class ScopedEvents:
        def get_events_by_ids(self, _season, game_ids):
            requested = set(game_ids)
            return tuple(
                event for event in events if event["nba_game_id"] in requested
            )

    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider="dabble", query=query),
        clock=lambda: OBSERVED_AT + timedelta(minutes=10),
        event_reader=ScopedEvents(),
    )

    pool = reader.get_pool(
        season=SEASON,
        game_ids=(GAME_ID, live_game_id, missing_game_id),
    )

    assert pool.game_states == {
        GAME_ID: {"state": "closing", "observed_at": OBSERVED_AT.isoformat()},
        live_game_id: {"state": "live", "observed_at": live_at.isoformat()},
        missing_game_id: {"state": "missing", "observed_at": None},
    }
    assert pool.freshness == {
        "state": "live",
        "observed_at": live_at.isoformat(),
        "retrieved_at": live_at.isoformat(),
        "providers": {
            "dabble": {
                "status": "fresh",
                "retrieved_at": live_at.isoformat(),
            }
        },
    }
    assert pool.players
def test_replay_athlete_mapping_recovers_unresolved_evidence_without_mutating_source(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping-replay.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="unresolved-athlete-market",
        athlete=AthleteEvidence(
            provider_id="athlete-unresolved",
            name="Provider Player",
            team=TeamEvidence(canonical_id=None),
        ),
        event=EventEvidence(provider_id="event-1", canonical_id=GAME_ID),
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
    snapshot = ProviderSnapshot(
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
    )
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    ingested = archive.ingest_snapshot(snapshot, query=query, accepted_at=OBSERVED_AT)
    source_before = archive.load_source_snapshot(ingested.snapshot_id)

    with engine.connect() as connection:
        observation_before = connection.execute(
            select(ProjectionObservation.__table__).where(
                ProjectionObservation.__table__.c.generation_id
                == ingested.generation_id
            )
        ).mappings().one()
    assert observation_before.resolution_state == "unresolved"
    assert observation_before.unresolved_identities == "athlete"
    assert _reader(engine).get_pool_for_game(season=SEASON, game_id=GAME_ID).players == ()

    replayed = archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-unresolved",
        canonical_player_id=7,
        canonical_player_name="Mapped Player",
        canonical_team_id=10,
        replayed_at=OBSERVED_AT - timedelta(minutes=1),
    )

    assert replayed.changed is True
    assert _reader(engine).get_pool_for_game(season=SEASON, game_id=GAME_ID).players[0].canonical_player_id == 7
    with engine.connect() as connection:
        counts = tuple(
            connection.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (
                ProjectionProviderSnapshot,
                ProviderPoll,
                ProjectionMaterializationGeneration,
                ProjectionObservation,
            )
        )
        latest = connection.execute(
            select(LatestPlayerProjection.__table__)
        ).mappings().one()
        original = connection.execute(
            select(ProjectionObservation.__table__).where(
                ProjectionObservation.__table__.c.generation_id
                == ingested.generation_id
            )
        ).mappings().one()
        replay_observation = connection.execute(
            select(ProjectionObservation.__table__).where(
                ProjectionObservation.__table__.c.generation_id
                == replayed.generation_id
            )
        ).mappings().one()
    assert counts == (1, 1, 2, 2)
    assert latest.generation_id == replayed.generation_id
    assert original.canonical_player_id is None
    assert original.observed_at == OBSERVED_AT.replace(tzinfo=None)
    assert replay_observation.canonical_player_id == 7
    assert replay_observation.ordinal == observation_before.ordinal
    assert replay_observation.observed_at == OBSERVED_AT.replace(tzinfo=None)
    assert archive.load_source_snapshot(ingested.snapshot_id) == source_before

    repeated = archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-unresolved",
        canonical_player_id=7,
        canonical_player_name="Mapped Player",
        canonical_team_id=10,
    )
    assert repeated.generation_id == replayed.generation_id
    assert repeated.changed is False
    assert repeated.materialization_outcome == "unchanged"
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ProjectionMaterializationGeneration)
        ).scalar_one() == 2


def test_replay_event_and_statistic_mappings_only_advances_affected_observations(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'multi-mapping-replay.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    query = NBAMarketQuery(season=SEASON)

    def market(index: int, *, unresolved: str | None = None) -> PlayerProjectionMarket:
        evidence = StatisticEvidence(
            provider_id=f"stat-{index}",
            canonical_id=None if unresolved == "statistic" else statistic.id,
        )
        return PlayerProjectionMarket(
            provider="dabble",
            market_id=f"market-{index}",
            athlete=AthleteEvidence(
                provider_id=f"athlete-{index}",
                canonical_id=100 + index,
                name=f"Player {index}",
                team=TeamEvidence(canonical_id=10),
            ),
            event=EventEvidence(
                provider_id=f"event-{index}",
                canonical_id=None if unresolved == "event" else GAME_ID,
            ),
            team=TeamEvidence(canonical_id=10),
            statistic=evidence,
            statistic_match=(
                None
                if unresolved == "statistic"
                else StatisticMatch(
                    state=MatchState.CANONICAL,
                    evidence=evidence,
                    scoring_period=ScoringPeriod.FULL_GAME,
                    canonical=statistic,
                    provider="dabble",
                )
            ),
            threshold=MarketThreshold("20.5", "count"),
            status=MarketStatus.AVAILABLE,
            variant=MarketVariant.STANDARD,
            scoring_period=ScoringPeriod.FULL_GAME,
        )

    snapshot = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(
            market(1, unresolved="event"),
            market(2, unresolved="statistic"),
            market(3),
        ),
        coverage=CoverageEvidence(
            fetched_count=3,
            eligible_count=3,
            normalized_count=3,
            expected_total=3,
        ),
        retrieved_at=OBSERVED_AT,
    )
    archive = ProjectionArchive(engine, catalog)
    archive.ingest_snapshot(snapshot, query=query, accepted_at=OBSERVED_AT)

    event_replay = archive.replay_event_mapping(
        provider="dabble",
        provider_event_id="event-1",
        canonical_game_id=GAME_ID,
    )
    assert event_replay is not None
    assert event_replay.observation_count == 1
    assert [player.canonical_player_id for player in _reader(engine).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    ).players] == [101, 103]

    statistic_replay = archive.replay_statistic_mapping(
        provider="dabble",
        provider_statistic_id="stat-2",
        canonical_statistic_id=statistic.id,
    )
    assert statistic_replay is not None
    assert statistic_replay.observation_count == 1
    assert [player.canonical_player_id for player in _reader(engine).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    ).players] == [101, 102, 103]
    repeated = archive.replay_event_mapping(
        provider="dabble",
        provider_event_id="event-1",
        canonical_game_id=GAME_ID,
    )
    assert repeated is not None
    assert repeated.changed is False
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ProjectionProviderSnapshot)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ProviderPoll)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ProjectionObservation)
        ).scalar_one() == 5


def test_replay_mapping_can_return_to_an_existing_materialization(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping-revert.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    market = PlayerProjectionMarket(
        provider="dabble",
        market_id="mapping-revert-market",
        athlete=AthleteEvidence(
            provider_id="athlete-revert",
            name="Provider Player",
        ),
        event=EventEvidence(provider_id="event-1", canonical_id=GAME_ID),
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
    archive.ingest_snapshot(
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

    first = archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-revert",
        canonical_player_id=7,
        canonical_player_name="Mapped Player One",
        canonical_team_id=10,
        replayed_at=OBSERVED_AT + timedelta(minutes=1),
    )
    second = archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-revert",
        canonical_player_id=8,
        canonical_player_name="Mapped Player Two",
        canonical_team_id=10,
        replayed_at=OBSERVED_AT + timedelta(minutes=2),
    )
    reverted = archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-revert",
        canonical_player_id=7,
        canonical_player_name="Mapped Player One",
        canonical_team_id=10,
        replayed_at=OBSERVED_AT + timedelta(minutes=3),
    )

    assert first is not None and second is not None and reverted is not None
    assert reverted.changed is True
    assert reverted.generation_id == first.generation_id
    pool = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(
            provider="dabble", query=NBAMarketQuery(season=SEASON)
        ),
        clock=lambda: OBSERVED_AT + timedelta(minutes=3),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert [player.canonical_player_id for player in pool.players] == [7]


def test_replay_densely_numbers_affected_rows_carried_across_snapshots(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping-carried.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    statistic = catalog.by_id["points"]

    def market(market_id: str) -> PlayerProjectionMarket:
        evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
        return PlayerProjectionMarket(
            provider="dabble",
            market_id=market_id,
            athlete=AthleteEvidence(
                provider_id="athlete-carried",
                canonical_id=7,
                name="Provider Player",
                team=TeamEvidence(canonical_id=10),
            ),
            event=EventEvidence(provider_id="event-1", canonical_id=GAME_ID),
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
    archive.ingest_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.COMPLETE,
            markets=(market("carried-a"),),
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
    partial_at = OBSERVED_AT + timedelta(minutes=1)
    archive.ingest_snapshot(
        ProviderSnapshot(
            provider="dabble",
            status=SnapshotStatus.PARTIAL,
            markets=(market("carried-b"),),
            coverage=CoverageEvidence(
                fetched_count=1,
                eligible_count=1,
                normalized_count=1,
                expected_total=2,
            ),
            retrieved_at=partial_at,
        ),
        query=query,
        accepted_at=partial_at,
    )

    replay = archive.replay_athlete_mapping(
        provider="dabble",
        provider_athlete_id="athlete-carried",
        canonical_player_id=8,
        canonical_player_name="Catalog Player",
        canonical_team_id=10,
        replayed_at=partial_at + timedelta(minutes=1),
    )

    assert replay is not None
    assert replay.observation_count == 2
    with engine.connect() as connection:
        ordinals = connection.execute(
            select(ProjectionObservation.ordinal)
            .where(ProjectionObservation.generation_id == replay.generation_id)
            .order_by(ProjectionObservation.ordinal)
        ).scalars().all()
    assert ordinals == [0, 1]


@pytest.mark.parametrize(
    ("canonical_name", "canonical_team_id"),
    [(None, 10), ("Catalog Player", None)],
)
def test_teamless_or_nameless_athlete_decision_skips_projection_replay(
    tmp_path,
    canonical_name,
    canonical_team_id,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping-incomplete.sqlite3'}")
    run_migrations(engine)
    archive = ProjectionArchive(engine, StatisticCatalog.load_default())
    decision = SimpleNamespace(
        persisted=True,
        mapping=SimpleNamespace(
            provider="dabble",
            provider_athlete_id="athlete-incomplete",
            canonical_player_id=7,
            canonical_name=canonical_name,
            canonical_team_id=canonical_team_id,
        ),
    )

    assert archive.replay_athlete_decision(decision) is None
