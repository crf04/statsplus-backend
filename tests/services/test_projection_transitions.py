"""Truthful provider-scoped projection transition behavior (#105)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select

from app.config.settings import ProviderSettings
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.migrations import run_migrations
from app.models.projection_archive import (
    LatestPlayerProjection,
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


SEASON = "2025-26"
GAME_ID = "0022500501"
OBSERVED_AT = datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc)
QUERY = NBAMarketQuery(season=SEASON)


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-transitions.sqlite3'}")
    run_migrations(engine)
    return engine


def _market(
    catalog: StatisticCatalog,
    *,
    provider: str = "dabble",
    market_id: str = "market-1",
    player_id: int = 7,
    status: MarketStatus = MarketStatus.AVAILABLE,
) -> PlayerProjectionMarket:
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(provider_id="pts", canonical_id=statistic.id)
    return PlayerProjectionMarket(
        provider=provider,
        market_id=market_id,
        athlete=AthleteEvidence(
            canonical_id=player_id,
            name=f"Player {player_id}",
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
            provider=provider,
        ),
        status=status,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
    )


def _snapshot(
    provider: str,
    status: SnapshotStatus,
    markets: tuple[PlayerProjectionMarket, ...],
    retrieved_at: datetime,
) -> ProviderSnapshot:
    expected_total = len(markets) if status is SnapshotStatus.COMPLETE else len(markets) + 1
    return ProviderSnapshot(
        provider=provider,
        status=status,
        markets=markets,
        coverage=CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            expected_total=expected_total,
            warning_codes=(() if status is SnapshotStatus.COMPLETE else ("page_fetch_failed",)),
        ),
        retrieved_at=retrieved_at,
    )


def _reader(engine, providers, now, *, required_providers=None):
    return LatestProjectionPlayerPoolReader(
        engine,
        tuple(
            ProjectionArchiveReadScope(provider=provider, query=QUERY)
            for provider in providers
        ),
        clock=lambda: now,
        required_providers=required_providers,
    )


def test_partial_updates_present_offerings_without_retiring_omissions(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    first = _market(catalog, market_id="first", player_id=7)
    omitted = _market(catalog, market_id="omitted", player_id=8)
    archive.ingest_snapshot(
        _snapshot("dabble", SnapshotStatus.COMPLETE, (first, omitted), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )

    partial_at = OBSERVED_AT + timedelta(minutes=1)
    archive.ingest_snapshot(
        _snapshot(
            "dabble",
            SnapshotStatus.PARTIAL,
            (replace(first, status=MarketStatus.SUSPENDED),),
            partial_at,
        ),
        query=QUERY,
        accepted_at=partial_at,
    )

    pool = _reader(engine, ("dabble",), partial_at).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert [player.canonical_player_id for player in pool.players] == [8]
    with engine.connect() as connection:
        latest = connection.execute(select(LatestPlayerProjection)).all()
        generations = connection.execute(
            select(LatestPlayerProjection.generation_id).distinct()
        ).scalars().all()
        assert len(latest) == 1
        assert len(generations) == 1


@pytest.mark.parametrize(
    ("targetability", "expected_ordinal"),
    (((True, False), 0), ((False, True), 1)),
)
def test_partial_repeated_reference_uses_one_transition_plan_for_checksum_and_write(
    tmp_path,
    monkeypatch,
    targetability,
    expected_ordinal,
):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    market = _market(catalog)
    partial = _snapshot(
        "dabble",
        SnapshotStatus.PARTIAL,
        (market, market),
        OBSERVED_AT,
    )
    base_row = archive._observation_rows(partial)[0]
    rows = [dict(base_row, ordinal=0), dict(base_row, ordinal=1)]
    reference = rows[0]["market_reference"]
    for row, targetable in zip(rows, targetability, strict=True):
        row["market_reference"] = reference
        row["targetable"] = targetable
    monkeypatch.setattr(archive, "_observation_rows", lambda _snapshot: rows)

    archive.ingest_snapshot(partial, query=QUERY, accepted_at=OBSERVED_AT)

    with engine.connect() as connection:
        selected_ordinal = connection.execute(
            select(ProjectionObservation.ordinal)
            .join(
                LatestPlayerProjection,
                LatestPlayerProjection.observation_id
                == ProjectionObservation.observation_id,
            )
        ).scalar_one()
    assert selected_ordinal == expected_ordinal


def test_failure_preserves_latest_for_six_hours_then_disabled_provider_expires(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    market = _market(catalog)
    archive.ingest_snapshot(
        _snapshot("dabble", SnapshotStatus.COMPLETE, (market,), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    failed_at = OBSERVED_AT + timedelta(minutes=16)
    archive.record_failed_poll(
        provider="dabble",
        query=QUERY,
        completed_at=failed_at,
        failure_reason="access_denied",
    )

    stale = _reader(engine, ("dabble",), failed_at).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert stale.freshness["providers"]["dabble"]["status"] == "stale-served"
    assert [player.canonical_player_id for player in stale.players] == [7]

    expired = _reader(
        engine, ("dabble",), OBSERVED_AT + timedelta(hours=6, microseconds=1)
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert expired.players == ()
    assert expired.freshness["state"] == "missing"
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ProjectionObservation)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(LatestPlayerProjection)).scalar_one() == 1


def test_unchanged_poll_refreshes_health_without_duplicate_evidence(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    market = _market(catalog)
    snapshot = _snapshot("dabble", SnapshotStatus.COMPLETE, (market,), OBSERVED_AT)
    archive.ingest_snapshot(snapshot, query=QUERY, accepted_at=OBSERVED_AT)
    repeated_at = OBSERVED_AT + timedelta(minutes=20)
    archive.ingest_snapshot(
        replace(snapshot, retrieved_at=repeated_at),
        query=QUERY,
        accepted_at=repeated_at,
    )

    pool = _reader(engine, ("dabble",), repeated_at).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert pool.freshness["providers"]["dabble"] == {
        "status": "fresh",
        "retrieved_at": repeated_at.isoformat(),
    }
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one() == 2
        assert connection.execute(select(func.count()).select_from(ProjectionProviderSnapshot)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(ProjectionObservation)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(ProjectionMaterializationGeneration)).scalar_one() == 1


def test_late_unchanged_and_changed_polls_do_not_mask_a_newer_failure(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    market = _market(catalog)
    snapshot = _snapshot("dabble", SnapshotStatus.COMPLETE, (market,), OBSERVED_AT)
    archive.ingest_snapshot(snapshot, query=QUERY, accepted_at=OBSERVED_AT)
    confirmed_at = OBSERVED_AT + timedelta(minutes=10)
    archive.ingest_snapshot(
        replace(snapshot, retrieved_at=confirmed_at),
        query=QUERY,
        accepted_at=confirmed_at,
    )
    failed_at = OBSERVED_AT + timedelta(minutes=20)
    archive.record_failed_poll(
        provider="dabble",
        query=QUERY,
        completed_at=failed_at,
        failure_reason="access_denied",
    )

    late_at = OBSERVED_AT - timedelta(minutes=1)
    archive.ingest_snapshot(
        replace(snapshot, retrieved_at=late_at),
        query=QUERY,
        accepted_at=failed_at + timedelta(seconds=1),
    )
    changed_market = replace(market, market_id="late-changed")
    changed_late_at = OBSERVED_AT + timedelta(minutes=5)
    archive.ingest_snapshot(
        _snapshot(
            "dabble",
            SnapshotStatus.COMPLETE,
            (changed_market,),
            changed_late_at,
        ),
        query=QUERY,
        accepted_at=failed_at + timedelta(seconds=2),
    )

    pool = _reader(engine, ("dabble",), failed_at + timedelta(seconds=2)).get_pool_for_game(
        season=SEASON,
        game_id=GAME_ID,
    )
    assert pool.freshness["status"] == "stale-served"
    assert pool.freshness["observed_at"] == confirmed_at.isoformat()
    with engine.connect() as connection:
        confirmations = connection.execute(
            select(LatestPlayerProjection.confirmed_at)
        ).scalars().all()
        polls = connection.execute(
            select(ProviderPoll.outcome, ProviderPoll.promoted).order_by(
                ProviderPoll.completed_at
            )
        ).all()
    assert confirmations == [confirmed_at.replace(tzinfo=None)]
    assert polls == [
        ("changed", True),
        ("unchanged", True),
        ("failed", False),
        ("unchanged", False),
        ("changed", False),
    ]


def test_equal_time_conflicting_snapshots_choose_the_same_winner_in_any_order(tmp_path):
    catalog = StatisticCatalog.load_default()
    first = _snapshot(
        "dabble",
        SnapshotStatus.COMPLETE,
        (_market(catalog, market_id="first", player_id=7),),
        OBSERVED_AT,
    )
    second = _snapshot(
        "dabble",
        SnapshotStatus.COMPLETE,
        (_market(catalog, market_id="second", player_id=8),),
        OBSERVED_AT,
    )
    winners = []
    promoted_counts = []
    for index, snapshots in enumerate(((first, second), (second, first))):
        engine = create_engine(f"sqlite:///{tmp_path / f'equal-time-{index}.sqlite3'}")
        run_migrations(engine)
        archive = ProjectionArchive(engine, catalog)
        for snapshot in snapshots:
            archive.ingest_snapshot(snapshot, query=QUERY, accepted_at=OBSERVED_AT)
        pool = _reader(engine, ("dabble",), OBSERVED_AT).get_pool_for_game(
            season=SEASON, game_id=GAME_ID
        )
        winners.append(tuple(player.canonical_player_id for player in pool.players))
        with engine.connect() as connection:
            promoted_counts.append(
                connection.execute(
                    select(func.count()).select_from(ProviderPoll).where(
                        ProviderPoll.promoted.is_(True)
                    )
                ).scalar_one()
            )

    assert winners[0] == winners[1]
    assert sorted(promoted_counts) == [1, 2]


def test_provider_scopes_union_and_an_unpolled_provider_expires_independently(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    for provider, player_id in (("dabble", 7), ("prizepicks", 8)):
        archive.ingest_snapshot(
            _snapshot(
                provider,
                SnapshotStatus.COMPLETE,
                (_market(catalog, provider=provider, player_id=player_id),),
                OBSERVED_AT,
            ),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    refreshed_at = OBSERVED_AT + timedelta(minutes=16)
    dabble = _snapshot(
        "dabble", SnapshotStatus.COMPLETE, (_market(catalog, player_id=7),), refreshed_at
    )
    archive.ingest_snapshot(dabble, query=QUERY, accepted_at=refreshed_at)

    pool = _reader(engine, ("dabble", "prizepicks"), refreshed_at).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert [player.canonical_player_id for player in pool.players] == [7]
    assert "status" not in pool.freshness
    assert pool.freshness["providers"] == {
        "dabble": {
            "status": "fresh",
            "retrieved_at": refreshed_at.isoformat(),
        },
        "prizepicks": {"status": "missing", "retrieved_at": None},
    }
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(LatestPlayerProjection).where(
                LatestPlayerProjection.provider == "prizepicks"
            )
        ).scalar_one() == 1


def test_disabled_provider_does_not_receive_the_failure_fallback_window(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    for provider, player_id in (("dabble", 7), ("prizepicks", 8)):
        archive.ingest_snapshot(
            _snapshot(
                provider,
                SnapshotStatus.COMPLETE,
                (_market(catalog, provider=provider, player_id=player_id),),
                OBSERVED_AT,
            ),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    archive.record_failed_poll(
        provider="prizepicks",
        query=QUERY,
        completed_at=OBSERVED_AT + timedelta(minutes=1),
        failure_reason="access_denied",
    )
    refreshed_at = OBSERVED_AT + timedelta(minutes=16)
    archive.ingest_snapshot(
        _snapshot(
            "dabble",
            SnapshotStatus.COMPLETE,
            (_market(catalog, player_id=7),),
            refreshed_at,
        ),
        query=QUERY,
        accepted_at=refreshed_at,
    )

    pool = _reader(
        engine,
        ("dabble", "prizepicks"),
        refreshed_at,
        required_providers=("dabble",),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert [player.canonical_player_id for player in pool.players] == [7]
    assert pool.freshness["status"] == "fresh"


def test_archive_market_limit_is_independent_and_rejects_before_persistence(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    markets = (
        _market(catalog, market_id="one", player_id=7),
        _market(catalog, market_id="two", player_id=8),
    )
    snapshot = _snapshot("dabble", SnapshotStatus.COMPLETE, markets, OBSERVED_AT)

    limits = ProviderSettings(
        dfs_comparison_max_markets=1,
        projection_archive_max_markets=2,
    )
    assert len(snapshot.markets) > limits.dfs_comparison_max_markets
    archive = ProjectionArchive(
        engine,
        catalog,
        max_markets=limits.projection_archive_max_markets,
    )
    archive.ingest_snapshot(snapshot, query=QUERY, accepted_at=OBSERVED_AT)
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(ProjectionObservation)
        ).scalar_one() == 2

    rejected_engine = create_engine(
        f"sqlite:///{tmp_path / 'projection-archive-limit-rejected.sqlite3'}"
    )
    run_migrations(rejected_engine)
    rejected_archive = ProjectionArchive(rejected_engine, catalog, max_markets=1)

    with pytest.raises(ValueError, match="market limit"):
        rejected_archive.ingest_snapshot(snapshot, query=QUERY, accepted_at=OBSERVED_AT)

    with rejected_engine.connect() as connection:
        for model in (
            ProviderPoll,
            ProjectionProviderSnapshot,
            ProjectionObservation,
            ProjectionMaterializationGeneration,
            LatestPlayerProjection,
        ):
            assert connection.execute(select(func.count()).select_from(model)).scalar_one() == 0


def test_document_bound_and_malformed_failure_are_rejected_without_state(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog, max_document_bytes=1)

    with pytest.raises(ValueError, match="document limit"):
        archive.ingest_snapshot(
            _snapshot(
                "dabble",
                SnapshotStatus.COMPLETE,
                (_market(catalog),),
                OBSERVED_AT,
            ),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="bounded code"):
        archive.record_failed_poll(
            provider="dabble",
            query=QUERY,
            completed_at=OBSERVED_AT,
            failure_reason="provider secret: do not persist",
        )

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ProviderPoll)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(LatestPlayerProjection)).scalar_one() == 0


def test_complete_empty_retires_only_its_provider_scope(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    for provider, player_id in (("dabble", 7), ("prizepicks", 8)):
        archive.ingest_snapshot(
            _snapshot(
                provider,
                SnapshotStatus.COMPLETE,
                (_market(catalog, provider=provider, player_id=player_id),),
                OBSERVED_AT,
            ),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    empty_at = OBSERVED_AT + timedelta(minutes=1)
    archive.ingest_snapshot(
        _snapshot("dabble", SnapshotStatus.COMPLETE, (), empty_at),
        query=QUERY,
        accepted_at=empty_at,
    )

    pool = _reader(engine, ("dabble", "prizepicks"), empty_at).get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    )
    assert [player.canonical_player_id for player in pool.players] == [8]
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(LatestPlayerProjection).where(
                LatestPlayerProjection.provider == "dabble"
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(ProjectionObservation).where(
                ProjectionObservation.provider == "dabble"
            )
        ).scalar_one() == 1
