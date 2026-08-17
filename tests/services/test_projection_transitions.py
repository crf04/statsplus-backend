"""Truthful provider-scoped projection transition behavior (#105)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select

from app.config.settings import ConfigurationError, ProviderSettings
from app.domain.comparisons import market_reference
from app.domain.freshness import MAX_TIME_WINDOW_SECONDS
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
    ProjectionSelectionPlayerPoolReader,
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


@pytest.mark.parametrize(
    ("override", "field"),
    (
        ({"live_max_age": timedelta(seconds=-1)}, "PROJECTION_LIVE_MAX_AGE"),
        (
            {"failure_fallback_max_age": timedelta(seconds=-1)},
            "PROJECTION_FAILURE_FALLBACK_MAX_AGE",
        ),
        (
            {"live_max_age": timedelta(seconds=1_000_000_001)},
            "PROJECTION_LIVE_MAX_AGE",
        ),
        (
            {"failure_fallback_max_age": timedelta(seconds=1_000_000_001)},
            "PROJECTION_FAILURE_FALLBACK_MAX_AGE",
        ),
    ),
)
def test_projection_reader_refuses_out_of_domain_direct_freshness_overrides(
    tmp_path,
    override,
    field,
):
    engine = _engine(tmp_path)

    with pytest.raises(ConfigurationError, match=field):
        LatestProjectionPlayerPoolReader(
            engine,
            ProjectionArchiveReadScope(provider="dabble", query=QUERY),
            **override,
        )


def test_projection_reader_accepts_the_shared_maximum_direct_window(tmp_path):
    engine = _engine(tmp_path)
    maximum = timedelta(seconds=int(MAX_TIME_WINDOW_SECONDS))

    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider="dabble", query=QUERY),
        live_max_age=maximum,
        failure_fallback_max_age=maximum,
    )

    assert reader.live_max_age == maximum
    assert reader.failure_fallback_max_age == maximum


@pytest.mark.parametrize(
    ("required_providers", "cutoff"),
    (
        (("dabble", "prizepicks"), timedelta(hours=6)),
        ((), timedelta(minutes=15)),
    ),
)
def test_populated_and_complete_empty_evidence_share_inclusive_provider_cutoffs(
    tmp_path,
    required_providers,
    cutoff,
):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    archive.ingest_snapshot(
        _snapshot(
            "dabble",
            SnapshotStatus.COMPLETE,
            (_market(catalog, player_id=7),),
            OBSERVED_AT,
        ),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    archive.ingest_snapshot(
        _snapshot("prizepicks", SnapshotStatus.COMPLETE, (), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    for provider in ("dabble", "prizepicks"):
        archive.record_failed_poll(
            provider=provider,
            query=QUERY,
            completed_at=OBSERVED_AT + timedelta(seconds=1),
            failure_reason="access_denied",
        )

    at_cutoff = _reader(
        engine,
        ("dabble", "prizepicks"),
        OBSERVED_AT + cutoff,
        required_providers=required_providers,
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert [player.canonical_player_id for player in at_cutoff.players] == [7]
    assert at_cutoff.freshness["providers"] == {
        provider: {
            "status": "stale-served",
            "retrieved_at": OBSERVED_AT.isoformat(),
        }
        for provider in ("dabble", "prizepicks")
    }

    after_cutoff = _reader(
        engine,
        ("dabble", "prizepicks"),
        OBSERVED_AT + cutoff + timedelta(microseconds=1),
        required_providers=required_providers,
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert after_cutoff.players == ()
    assert after_cutoff.freshness["state"] == "missing"


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
        accepted_at=repeated_at + timedelta(seconds=2),
        poll_started_at=repeated_at + timedelta(seconds=1),
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


def test_delayed_retry_of_exact_evidence_is_one_idempotent_poll(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    snapshot = _snapshot(
        "dabble",
        SnapshotStatus.COMPLETE,
        (_market(catalog, player_id=7),),
        OBSERVED_AT,
    )
    archive = ProjectionArchive(engine, catalog)
    first = archive.ingest_snapshot(
        snapshot,
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    with engine.connect() as connection:
        first_poll_id = connection.execute(select(ProviderPoll.poll_id)).scalar_one()

    remapped_retry = ProjectionArchive(engine, catalog)
    remapped_retry.market_categories["points"] = "PRA"
    retry = remapped_retry.ingest_snapshot(
        snapshot,
        query=QUERY,
        accepted_at=OBSERVED_AT + timedelta(minutes=10),
        poll_started_at=OBSERVED_AT + timedelta(minutes=9),
    )

    assert retry == first
    with engine.connect() as connection:
        assert connection.execute(select(ProviderPoll.poll_id)).scalars().all() == [
            first_poll_id
        ]
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


@pytest.mark.parametrize("identity_kind", ("supplied", "idless_variants"))
def test_market_order_is_canonical_for_exact_replay_and_new_health_confirmation(
    tmp_path,
    identity_kind,
):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    if identity_kind == "supplied":
        markets = (
            _market(catalog, market_id="market-z", player_id=7),
            _market(catalog, market_id="market-a", player_id=8),
        )
    else:
        base = _market(catalog, market_id=None, player_id=7)
        markets = (base, replace(base, variant=MarketVariant.ALTERNATE))
    snapshot = _snapshot("dabble", SnapshotStatus.COMPLETE, markets, OBSERVED_AT)
    reversed_snapshot = replace(snapshot, markets=tuple(reversed(markets)))
    archive = ProjectionArchive(engine, catalog)

    first = archive.ingest_snapshot(
        snapshot,
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    replay = archive.ingest_snapshot(
        reversed_snapshot,
        query=QUERY,
        accepted_at=OBSERVED_AT + timedelta(seconds=1),
        poll_started_at=OBSERVED_AT + timedelta(microseconds=1),
    )
    newer_retrieved_at = OBSERVED_AT + timedelta(minutes=1)
    newer = archive.ingest_snapshot(
        replace(reversed_snapshot, retrieved_at=newer_retrieved_at),
        query=QUERY,
        accepted_at=newer_retrieved_at + timedelta(seconds=1),
    )

    assert replay == first
    assert newer.changed is False
    assert newer.materialization_outcome == "unchanged"
    archived = archive.load_source_snapshot(first.snapshot_id)
    assert archived is not None
    assert tuple(market_reference(market) for market in archived.markets) == tuple(
        sorted(market_reference(market) for market in markets)
    )
    assert {market.variant for market in archived.markets} == {
        market.variant for market in markets
    }
    assert all(market.statistic_match is None for market in archived.markets)
    with engine.connect() as connection:
        polls = connection.execute(
            select(ProviderPoll.outcome, ProviderPoll.retrieved_at).order_by(
                ProviderPoll.retrieved_at
            )
        ).all()
        observations = connection.execute(
            select(
                ProjectionObservation.ordinal,
                ProjectionObservation.market_reference,
            ).order_by(ProjectionObservation.ordinal)
        ).all()
        assert connection.execute(
            select(func.count()).select_from(ProjectionProviderSnapshot)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(ProjectionMaterializationGeneration)
        ).scalar_one() == 1
    assert [poll.outcome for poll in polls] == ["changed", "unchanged"]
    assert polls[1].retrieved_at == newer_retrieved_at.replace(tzinfo=None)
    assert observations == [
        (ordinal, reference)
        for ordinal, reference in enumerate(
            sorted(market_reference(market) for market in markets)
        )
    ]


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


def test_first_fenced_equal_time_snapshot_is_the_only_promoted_winner(tmp_path):
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
    outcomes = []
    for index, snapshots in enumerate(((first, second), (second, first))):
        engine = create_engine(f"sqlite:///{tmp_path / f'equal-time-{index}.sqlite3'}")
        run_migrations(engine)
        archive = ProjectionArchive(engine, catalog)
        outcomes.append(
            tuple(
                archive.ingest_snapshot(
                    snapshot, query=QUERY, accepted_at=OBSERVED_AT
                ).materialization_outcome
                for snapshot in snapshots
            )
        )
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

    assert winners == [(7,), (8,)]
    assert promoted_counts == [1, 1]
    assert outcomes == [
        ("advanced", "same_time_not_promoted"),
        ("advanced", "same_time_not_promoted"),
    ]


def test_same_evidence_mapping_change_replays_the_first_materialization(tmp_path):
    catalog = StatisticCatalog.load_default()
    snapshot = _snapshot(
        "dabble",
        SnapshotStatus.COMPLETE,
        (_market(catalog, player_id=7),),
        OBSERVED_AT,
    )
    latest_categories = []
    for index, first_category in enumerate(("PTS", "PRA")):
        engine = create_engine(
            f"sqlite:///{tmp_path / f'same-time-materialization-{index}.sqlite3'}"
        )
        run_migrations(engine)
        first = ProjectionArchive(engine, catalog)
        second = ProjectionArchive(engine, catalog)
        first.market_categories["points"] = first_category
        second.market_categories["points"] = (
            "PRA" if first_category == "PTS" else "PTS"
        )

        first_result = first.ingest_snapshot(
            snapshot,
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
        second_result = second.ingest_snapshot(
            snapshot,
            query=QUERY,
            accepted_at=OBSERVED_AT + timedelta(seconds=1),
        )

        assert first_result.materialization_outcome == "advanced"
        assert second_result == first_result
        with engine.connect() as connection:
            polls = connection.execute(
                select(ProviderPoll.outcome, ProviderPoll.promoted).order_by(
                    ProviderPoll.completed_at
                )
            ).all()
            generations = connection.execute(
                select(ProjectionMaterializationGeneration.outcome).order_by(
                    ProjectionMaterializationGeneration.created_at
                )
            ).scalars().all()
            latest_categories.append(
                connection.execute(
                    select(LatestPlayerProjection.market_category)
                ).scalar_one()
            )
        assert polls == [("changed", True)]
        assert generations == ["advanced"]

    assert latest_categories == ["PTS", "PRA"]


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
    assert pool.freshness == {
        "status": "fresh",
        "state": "live",
        "observed_at": OBSERVED_AT.isoformat(),
        "retrieved_at": OBSERVED_AT.isoformat(),
        "providers": {
            "dabble": {
                "status": "fresh",
                "retrieved_at": empty_at.isoformat(),
            },
            "prizepicks": {
                "status": "fresh",
                "retrieved_at": OBSERVED_AT.isoformat(),
            },
        },
    }
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


def test_complete_empty_is_fresh_live_evidence_not_a_missing_pool(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    archive.ingest_snapshot(
        _snapshot("dabble", SnapshotStatus.COMPLETE, (), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    reader = _reader(engine, ("dabble",), OBSERVED_AT)

    pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.players == ()
    assert pool.team_counts == {}
    assert pool.game_states == {
        GAME_ID: {
            "state": "live",
            "observed_at": OBSERVED_AT.isoformat(),
        }
    }
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
    assert ProjectionSelectionPlayerPoolReader(reader).get_pool_for_game(
        season=SEASON,
        game_id=GAME_ID,
    ) is not None


@pytest.mark.parametrize(
    "required_providers",
    (("dabble",), ("dabble", "prizepicks")),
)
def test_nonrequired_or_mixed_complete_empty_cannot_cover_missing_required_providers(
    tmp_path,
    required_providers,
):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    ProjectionArchive(engine, catalog).ingest_snapshot(
        _snapshot("prizepicks", SnapshotStatus.COMPLETE, (), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )

    pool = _reader(
        engine,
        ("dabble", "prizepicks"),
        OBSERVED_AT,
        required_providers=required_providers,
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.players == ()
    assert pool.freshness == {
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {
            "dabble": {"status": "missing", "retrieved_at": None},
            "prizepicks": {
                "status": "fresh",
                "retrieved_at": OBSERVED_AT.isoformat(),
            },
        },
    }
    assert pool.game_states[GAME_ID] == {"state": "missing", "observed_at": None}


def test_all_disabled_complete_empty_expires_at_the_inclusive_live_boundary(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    ProjectionArchive(engine, catalog).ingest_snapshot(
        _snapshot("prizepicks", SnapshotStatus.COMPLETE, (), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )

    at_cutoff = _reader(
        engine,
        ("dabble", "prizepicks"),
        OBSERVED_AT + timedelta(minutes=15),
        required_providers=(),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert at_cutoff.freshness["state"] == "live"
    assert at_cutoff.game_states[GAME_ID] == {
        "state": "live",
        "observed_at": OBSERVED_AT.isoformat(),
    }

    expired = _reader(
        engine,
        ("dabble", "prizepicks"),
        OBSERVED_AT + timedelta(minutes=15, microseconds=1),
        required_providers=(),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert expired.freshness == {
        "status": "unavailable",
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {},
    }
    assert expired.game_states[GAME_ID] == {
        "state": "missing",
        "observed_at": None,
    }


def test_older_complete_empty_does_not_cover_a_newer_nonempty_generation(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    archive.ingest_snapshot(
        _snapshot("dabble", SnapshotStatus.COMPLETE, (), OBSERVED_AT),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    nonempty_at = OBSERVED_AT + timedelta(minutes=1)
    other_game_market = replace(
        _market(catalog, player_id=7),
        event=EventEvidence(canonical_id="other-game"),
    )
    archive.ingest_snapshot(
        _snapshot(
            "dabble",
            SnapshotStatus.COMPLETE,
            (other_game_market,),
            nonempty_at,
        ),
        query=QUERY,
        accepted_at=nonempty_at,
    )
    archive.record_failed_poll(
        provider="dabble",
        query=QUERY,
        completed_at=nonempty_at + timedelta(minutes=1),
        failure_reason="access_denied",
    )

    pool = _reader(
        engine,
        ("dabble",),
        nonempty_at + timedelta(minutes=2),
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.players == ()
    assert pool.freshness["state"] == "missing"
    assert pool.game_states[GAME_ID] == {"state": "missing", "observed_at": None}


def test_failed_poll_without_successful_evidence_remains_missing(tmp_path):
    engine = _engine(tmp_path)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    reader = _reader(engine, ("dabble",), OBSERVED_AT)
    assert reader.get_pool_for_game(
        season=SEASON, game_id=GAME_ID
    ).freshness["state"] == "missing"

    archive.record_failed_poll(
        provider="dabble",
        query=QUERY,
        completed_at=OBSERVED_AT,
        failure_reason="access_denied",
    )
    failed_pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert failed_pool.freshness == {
        "status": "unavailable",
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {
            "dabble": {"status": "missing", "retrieved_at": None}
        },
    }
    assert ProjectionSelectionPlayerPoolReader(reader).get_pool_for_game(
        season=SEASON,
        game_id=GAME_ID,
    ) is None
