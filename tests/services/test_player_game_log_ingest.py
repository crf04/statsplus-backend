"""Incremental PBP player-game-log ingestion and per-game sync evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app.errors import ProviderUnavailableError
from app.migrations import run_migrations
from app.providers.pbp_game_logs import PBP_GAME_LOG_COLUMNS
from app.services.athlete_catalog_service import AthleteCatalogService
from app.services.event_catalog_service import EventCatalogService
from app.services.player_game_log_ingest import (
    PlayerGameLogIngestError,
    PlayerGameLogIngestService,
)
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import (
    PLAYER_GAME_LOG_SURFACE,
    StatsFreshnessRepository,
)
from tests.services.test_player_game_logs import (
    RETRIEVED_AT,
    SEASON,
    _seed_identities,
)

AAA_PLAYERS = (101, 103, 104, 105, 106)
BBB_PLAYERS = (202, 107, 108, 109, 110)


def _repository(tmp_path) -> PlayerGameLogRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'ingest.sqlite3'}")
    run_migrations(engine)
    return PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
    )


def _game_rows(game_id: str, game_date: str, home_id: int, away_id: int):
    rows = []
    for index, player_id in enumerate((*AAA_PLAYERS, *BBB_PLAYERS)):
        rows.append(
            {
                "EntityId": player_id,
                "Name": f"Player {player_id}",
                "GameId": game_id,
                "Date": game_date,
                "Team": "AAA" if index < 5 else "BBB",
                "Opponent": "BBB" if index < 5 else "AAA",
                "Minutes": f"{20 + index}:00",
                "FG2M": 4 + index,
                "FG2A": 8 + index,
                "FG3M": index,
                "FG3A": index + 2,
                "FtPoints": 2,
                "FTA": 3,
                "OffRebounds": 1,
                "DefRebounds": 3,
                "Assists": 2 + index,
                "Turnovers": 1,
                "Steals": 1,
                "Blocks": 0,
                "Fouls": 1,
                "Points": 12 + 2 * index,
            }
        )
    return rows


def _frame(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=PBP_GAME_LOG_COLUMNS)
    return pd.DataFrame(
        [
            {column: row.get(column) for column in PBP_GAME_LOG_COLUMNS}
            for row in rows
        ]
    )


class FakeGameLogProvider:
    def __init__(self, games):
        self.games = {game_id: _frame(rows) for game_id, rows in games.items()}
        self.calls = []

    def fetch_game_player_logs(self, game_id, season, *, season_type):
        self.calls.append((game_id, season, season_type))
        return self.games[game_id].copy()


def _service(
    repository,
    provider,
    *,
    reconciliation_days=3,
    clock=lambda: RETRIEVED_AT,
    minimum_active_players_per_team_game=5,
):
    return PlayerGameLogIngestService(
        pbp_provider=provider,
        repository=repository,
        athlete_catalog=AthleteCatalogService(
            repository.engine,
            nba_stats_provider=provider,
            freshness_days=7,
        ),
        event_catalog=EventCatalogService(
            repository.engine, nba_stats_provider=provider
        ),
        minimum_active_players_per_team_game=minimum_active_players_per_team_game,
        reconciliation_days=reconciliation_days,
        clock=clock,
    )


def _complete_game_logs():
    return {
        "0022500001": _game_rows("0022500001", "2026-01-02", 1, 2),
        "0022500004": _game_rows("0022500004", "2026-01-11", 1, 2),
    }


def test_ingest_publishes_each_missing_completed_game_atomically(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = FakeGameLogProvider(_complete_game_logs())

    result = _service(repository, provider).refresh(SEASON)

    assert result.season == SEASON
    assert result.games_processed == 2
    assert result.row_count == 20
    assert result.retrieved_at == RETRIEVED_AT.isoformat()
    assert sorted(call[0] for call in provider.calls) == [
        "0022500001",
        "0022500004",
    ]
    rows = repository.list_player_rows(SEASON, 101)
    assert sorted(row.game_id for row in rows) == ["0022500001", "0022500004"]
    assert rows[0].field_goals_made == 4
    assert rows[0].free_throws_made == 2
    assert rows[0].offensive_rebounds == 1
    assert rows[0].personal_fouls == 1

    freshness = repository.get_freshness(SEASON)
    assert freshness.publication_status == "complete"
    assert repository.has_complete_publication(SEASON) is True

    sync = repository.get_sync_status(SEASON, "0022500001")
    assert sync.status == "complete"
    assert sync.row_count == 10
    assert sync.source_provider == "pbp_stats"
    assert len(sync.checksum) == 64


def test_failed_run_preserves_the_last_complete_publication(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    games = _complete_game_logs()
    provider = FakeGameLogProvider(games)
    service = _service(repository, provider, reconciliation_days=14)

    service.refresh(SEASON)
    assert repository.has_complete_publication(SEASON) is True
    surface_before = StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion

    class FailingProvider(FakeGameLogProvider):
        def fetch_game_player_logs(self, game_id, season, *, season_type):
            if game_id == "0022500004":
                raise ProviderUnavailableError("pbp stats down")
            return super().fetch_game_player_logs(
                game_id, season, season_type=season_type
            )

    failing = _service(repository, FailingProvider(games), reconciliation_days=14)
    with pytest.raises(ProviderUnavailableError):
        failing.refresh(SEASON)

    # The last complete publication is preserved and its freshness never moved.
    assert repository.has_complete_publication(SEASON) is True
    assert repository.get_freshness(SEASON).publication_status == "complete"
    surface_after = StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion
    assert surface_after == surface_before
    assert repository.list_player_rows(SEASON, 101)


def test_failed_reconcile_preserves_exact_prior_rows_and_complete_publication(
    tmp_path,
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    games = _complete_game_logs()
    provider = FakeGameLogProvider(games)
    service = _service(repository, provider, reconciliation_days=14)

    service.refresh(SEASON)
    rows_before = repository.list_player_rows(SEASON, 101)

    # Game 1's facts change upstream; game 2's provider call then fails.  The
    # staged correction must not leak into the published facts.
    corrected = games["0022500001"]
    corrected[0] = {**corrected[0], "Points": 99, "Minutes": "45:00"}
    provider.games["0022500001"] = _frame(corrected)

    class FailingProvider(FakeGameLogProvider):
        def fetch_game_player_logs(self, game_id, season, *, season_type):
            if game_id == "0022500004":
                raise ProviderUnavailableError("pbp stats down")
            return super().fetch_game_player_logs(
                game_id, season, season_type=season_type
            )

    failing = _service(repository, FailingProvider(games), reconciliation_days=14)
    with pytest.raises(ProviderUnavailableError):
        failing.refresh(SEASON)

    # Exact old row equality: game 1's rows are unchanged and the complete
    # publication still stands.
    assert repository.list_player_rows(SEASON, 101) == rows_before
    assert repository.has_complete_publication(SEASON) is True
    assert repository.get_freshness(SEASON).publication_status == "complete"


def test_failed_first_run_advances_no_freshness_and_stores_no_sidecar(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)

    class FailingProvider(FakeGameLogProvider):
        def fetch_game_player_logs(self, game_id, season, *, season_type):
            if game_id == "0022500004":
                raise ProviderUnavailableError("pbp stats down")
            return super().fetch_game_player_logs(
                game_id, season, season_type=season_type
            )

    provider = FailingProvider(_complete_game_logs())

    with pytest.raises(ProviderUnavailableError):
        _service(repository, provider).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion is None
    assert repository.has_complete_publication(SEASON) is False


def test_ingest_skips_unchanged_reconciliation_games_idempotently(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    games = _complete_game_logs()
    provider = FakeGameLogProvider(games)
    service = _service(repository, provider, reconciliation_days=14)

    first = service.refresh(SEASON)
    sync_before = repository.get_sync_status(SEASON, "0022500004")
    second = service.refresh(SEASON)
    sync_after = repository.get_sync_status(SEASON, "0022500004")

    assert first.games_processed == 2
    assert second.games_processed == 0
    assert sync_after == sync_before
    assert repository.list_player_rows(SEASON, 101)


def test_ingest_reconciles_corrected_recent_games_atomically(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    games = _complete_game_logs()
    provider = FakeGameLogProvider(games)
    service = _service(repository, provider, reconciliation_days=14)

    service.refresh(SEASON)
    sync_before = repository.get_sync_status(SEASON, "0022500004")

    corrected = games["0022500004"]
    corrected[0] = {**corrected[0], "Points": 30, "Minutes": "38:00"}
    provider.games["0022500004"] = _frame(corrected)

    result = service.refresh(SEASON)
    sync_after = repository.get_sync_status(SEASON, "0022500004")

    assert result.games_processed == 1
    rows = repository.list_player_rows(SEASON, 101)
    corrected_row = next(
        row for row in rows if row.game_id == "0022500004"
    )
    assert corrected_row.points == 30
    assert corrected_row.minutes == 38.0
    assert sync_after.checksum != sync_before.checksum


def test_ingest_rejects_a_game_that_cannot_join_canonical_identity(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    rows = _game_rows("0022500004", "2026-01-11", 1, 2)
    rows[0]["Team"] = "ZZZ"
    provider = FakeGameLogProvider(
        {
            "0022500001": _game_rows("0022500001", "2026-01-02", 1, 2),
            "0022500004": rows,
        }
    )

    with pytest.raises(ProviderUnavailableError, match="contradictory team identity"):
        _service(repository, provider).refresh(SEASON)

    # A failed run publishes nothing: no game rows and no season sidecar.
    assert repository.stored_game_ids(SEASON) == frozenset()
    assert repository.get_freshness(SEASON).retrieved_at is None


def test_ingest_rejects_a_game_with_an_unjoined_athlete(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    rows = _game_rows("0022500001", "2026-01-02", 1, 2)
    rows[5] = {**rows[5], "EntityId": 999999}
    provider = FakeGameLogProvider({"0022500001": rows})

    with pytest.raises(PlayerGameLogIngestError, match="Athlete Catalog"):
        _service(repository, provider).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None


def test_ingest_rejects_incomplete_team_coverage(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    rows = _game_rows("0022500001", "2026-01-02", 1, 2)
    rows = rows[:6]
    provider = FakeGameLogProvider({"0022500001": rows})

    with pytest.raises(PlayerGameLogIngestError, match="do not cover both teams"):
        _service(repository, provider).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None


def test_ingest_validates_constructor_arguments(tmp_path):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="positive integer"):
        _service(repository, FakeGameLogProvider({}), minimum_active_players_per_team_game=0)
    with pytest.raises(ValueError, match="non-negative integer"):
        _service(repository, FakeGameLogProvider({}), reconciliation_days=-1)


def test_empty_completed_season_records_honest_empty_publication(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    # No completed events exist in the seeded catalog outside the two finals,
    # so point the clock before the first completed game's tip.
    before = datetime(2025, 12, 1, tzinfo=timezone.utc)
    provider = FakeGameLogProvider({})

    result = _service(repository, provider, clock=lambda: before).refresh(SEASON)

    assert result.games_processed == 0
    assert result.row_count == 0
    freshness = repository.get_freshness(SEASON)
    assert freshness.publication_status == "complete"
    assert freshness.row_count == 0


def test_database_without_player_log_tables_reports_no_complete_publication(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'bare.sqlite3'}")
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
    )

    assert repository.has_complete_publication(SEASON) is False
