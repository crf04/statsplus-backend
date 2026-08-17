"""Behavioral coverage for durable projection evidence and live Player Pools."""

from datetime import datetime, timezone

from sqlalchemy import create_engine

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.migrations import run_migrations
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
)
from app.services.statistic_catalog import StatisticCatalog


OBSERVED_AT = datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500501"


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
    result = archive.ingest_complete_snapshot(
        snapshot,
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT,
    )
    pool = LatestProjectionPlayerPoolReader(
        engine, clock=lambda: OBSERVED_AT
    ).get_pool_for_game(
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

    repeated = archive.ingest_complete_snapshot(
        snapshot,
        query=NBAMarketQuery(season=SEASON),
        accepted_at=OBSERVED_AT.replace(minute=31),
    )
    assert repeated.changed is False
    assert repeated.snapshot_id == result.snapshot_id


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
    pool = LatestProjectionPlayerPoolReader(
        engine, clock=lambda: OBSERVED_AT
    ).get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert result.observation_count == 1
    assert archive.load_source_snapshot(result.snapshot_id).markets == (market,)
    assert pool.players == ()
    assert pool.freshness["state"] == "missing"
