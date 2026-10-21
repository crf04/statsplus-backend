"""Which lines a closing set freezes after partial polls (crf04/statsplus#279)."""

from datetime import timedelta

from sqlalchemy import create_engine

from app.domain.statistics import ScoringPeriod
from app.migrations import run_migrations
from app.providers.dfs import (
    CoverageEvidence,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    ProviderSnapshot,
    SnapshotStatus,
)
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
)
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_projection_archive import GAME_ID, OBSERVED_AT, SEASON, _line_market

PROVIDER = "novig"
OTHER_GAME_ID = "0022500502"
TIP = OBSERVED_AT + timedelta(minutes=30)


def _freeze(tmp_path, polls):
    engine = create_engine(f"sqlite:///{tmp_path / 'closing-fold.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    query = NBAMarketQuery(season=SEASON)
    scope = ProjectionArchiveReadScope(provider=PROVIDER, query=query)
    service = ProjectionRecordingService(ProjectionArchive(engine, catalog), scope)
    for minutes, complete, offers in polls:
        retrieved_at = OBSERVED_AT + timedelta(minutes=minutes)
        markets = tuple(
            _line_market(
                catalog,
                provider=PROVIDER,
                market_id=market_id,
                statistic_id=statistic_id,
                threshold=threshold,
                game_id=game_id,
                status=status,
                variant=variant,
                scoring_period=scoring_period,
            )
            for market_id, statistic_id, threshold, game_id, status, variant, scoring_period in offers
        )
        service.record_snapshot(
            ProviderSnapshot(
                provider=PROVIDER,
                status=SnapshotStatus.COMPLETE if complete else SnapshotStatus.PARTIAL,
                markets=markets,
                coverage=CoverageEvidence(
                    fetched_count=len(markets),
                    eligible_count=len(markets),
                    normalized_count=len(markets),
                    fanout_complete=complete,
                ),
                retrieved_at=retrieved_at,
            ),
            query=query,
            accepted_at=retrieved_at,
        )
    frozen = ProjectionArchive(engine, catalog).freeze_closing_projection_set(
        provider=PROVIDER, query=query, canonical_game_id=GAME_ID, started_at=TIP, created_at=TIP
    )
    event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": TIP.isoformat(),
        "status_text": "Q1",
        "status_code": 2,
        "first_observed_started_at": TIP.isoformat(),
    }

    class Events:
        def get_events_by_ids(self, _season, game_ids):
            return (event,) if GAME_ID in game_ids else ()

    reader = LatestProjectionPlayerPoolReader(
        engine, scope, clock=lambda: TIP + timedelta(minutes=1), event_reader=Events()
    )
    pool = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert pool.freshness["state"] == "closing"
    return frozen, pool.players


def _offer(
    market_id,
    statistic_id,
    threshold,
    game_id=GAME_ID,
    status=MarketStatus.AVAILABLE,
    variant=MarketVariant.STANDARD,
    scoring_period=ScoringPeriod.FULL_GAME,
):
    return (market_id, statistic_id, threshold, game_id, status, variant, scoring_period)


def test_a_market_re_posted_under_a_new_reference_freezes_one_line(tmp_path):
    frozen, players = _freeze(
        tmp_path,
        (
            (0, True, (_offer("pts-a", "points", "29.5"),)),
            (10, False, (_offer("pts-b", "points", "30.5"),)),
        ),
    )

    assert frozen.observation_count == 1
    (player,) = players
    assert player.lines == {PROVIDER: {"PTS": 30.5}}


def test_a_market_the_later_partial_names_as_untargetable_is_dropped(tmp_path):
    frozen, players = _freeze(
        tmp_path,
        (
            (0, True, (_offer("pts-a", "points", "29.5"), _offer("reb-a", "rebounds", "7.5"))),
            (10, False, (_offer("pts-b", "points", "30.5", status=MarketStatus.SUSPENDED),)),
        ),
    )

    assert frozen.observation_count == 1
    (player,) = players
    assert player.lines == {PROVIDER: {"REB": 7.5}}


def test_a_market_a_failed_fixture_partial_omits_is_carried(tmp_path):
    frozen, players = _freeze(
        tmp_path,
        (
            (0, True, (_offer("pts-a", "points", "29.5"), _offer("reb-a", "rebounds", "7.5"))),
            (10, False, (_offer("pts-other", "points", "18.5", game_id=OTHER_GAME_ID),)),
        ),
    )

    assert frozen.observation_count == 2
    (player,) = players
    assert player.lines == {PROVIDER: {"PTS": 29.5, "REB": 7.5}}


def test_a_partial_naming_only_an_alternate_keeps_the_carried_standard(tmp_path):
    frozen, players = _freeze(
        tmp_path,
        (
            (0, True, (_offer("pts-a", "points", "29.5"),)),
            (10, False, (_offer("pts-alt", "points", "40.5", variant=MarketVariant.ALTERNATE),)),
        ),
    )

    assert frozen.observation_count == 1
    (player,) = players
    assert player.lines == {PROVIDER: {"PTS": 29.5}}


def test_a_partial_naming_only_a_first_half_market_keeps_the_carried_full_game_line(tmp_path):
    frozen, players = _freeze(
        tmp_path,
        (
            (0, True, (_offer("pts-a", "points", "29.5"),)),
            (10, False, (_offer("pts-1h", "points", "14.5", scoring_period=ScoringPeriod.FIRST_HALF),)),
        ),
    )

    assert frozen.observation_count == 1
    (player,) = players
    assert player.lines == {PROVIDER: {"PTS": 29.5}}
