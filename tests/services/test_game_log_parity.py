"""Stage 3 parity between the live PBP path and the durable stored path.

The #66 contract amendment removes plus/minus from the game-log contract
entirely, so an ingestion-complete, valid durable publication is again
database-first, and live and stored documents compare strictly.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.migrations import run_migrations
from app.models.game_logs import GameLogQuery
from app.providers.pbp_game_logs import PBP_GAME_LOG_COLUMNS
from app.services.athlete_catalog_service import AthleteCatalogService
from app.services.event_catalog_service import EventCatalogService
from app.services.game_logs_source import (
    DatabaseFirstGameLogsSource,
    LivePBPGameLogsSource,
    StoredGameLogsSource,
)
from app.services.game_service import GameService
from app.services.player_game_log_ingest import PlayerGameLogIngestService
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_player_game_logs import (
    RETRIEVED_AT,
    SEASON,
    _seed_completed_playoff_event,
    _seed_identities,
)

AAA_PLAYERS = (101, 103, 104, 105, 106)
BBB_PLAYERS = (202, 107, 108, 109, 110)


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
    return pd.DataFrame(
        [
            {column: row.get(column) for column in PBP_GAME_LOG_COLUMNS}
            for row in rows
        ]
    )


class PlayerLogProvider:
    """Serves the per-player and per-game views of the same game facts."""

    def __init__(self, games, regular_season_game_ids=None):
        self.games = games
        self.regular_season_game_ids = (
            regular_season_game_ids if regular_season_game_ids is not None
            else frozenset(games)
        )

    def fetch_player_game_logs(self, player_id, season, *, season_type, cache_status):
        rows = [
            row
            for game_rows in self.games.values()
            for row in game_rows
            if row["EntityId"] == player_id
            and row["GameId"] in self.regular_season_game_ids
        ]
        return _frame(rows)

    def fetch_game_player_logs(self, game_id, season, *, season_type):
        return _frame(self.games[game_id])

    def record_cache_hit(self, operation):
        return None


class _EventsFromDb:
    """Read the seeded governed events through the owner's read shape."""

    def __init__(self, engine):
        self.engine = engine

    def get_events(self, season):
        from sqlalchemy import select

        from app.models.event_catalog import EventCatalogEntry

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(EventCatalogEntry.__table__)
            ).mappings().all()
        return [dict(row) for row in rows]


@pytest.fixture
def durable_world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parity.sqlite3'}")
    run_migrations(engine)
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
    )
    _seed_identities(repository)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE player_information ("id" INTEGER, "full_name" TEXT)'
        )
        connection.exec_driver_sql(
            "INSERT INTO player_information VALUES (101, 'Player One')"
        )
    games = {
        "0022500001": _game_rows("0022500001", "2026-01-02", 1, 2),
        "0022500004": _game_rows("0022500004", "2026-01-11", 1, 2),
    }
    provider = PlayerLogProvider(games)
    ingest = PlayerGameLogIngestService(
        pbp_provider=provider,
        repository=repository,
        athlete_catalog=AthleteCatalogService(
            engine, nba_stats_provider=provider, freshness_days=7
        ),
        event_catalog=EventCatalogService(engine, nba_stats_provider=provider),
        minimum_active_players_per_team_game=5,
        reconciliation_days=3,
        clock=lambda: RETRIEVED_AT,
    )
    ingest.refresh(SEASON)
    return engine, repository, provider, games


def _settings():
    return RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings(current_season=SEASON),
    )


def _service(engine, source):
    return GameService(
        engine,
        Mock(),
        settings=_settings(),
        game_logs_source=source,
    )


def _live_service(engine, provider):
    return _service(
        engine,
        LivePBPGameLogsSource(provider, _EventsFromDb(engine)),
    )


def _stored_service(engine, repository):
    return _service(
        engine,
        StoredGameLogsSource(repository),
    )


def test_complete_publication_is_database_first_with_strict_parity(durable_world):
    engine, repository, provider, _games = durable_world

    assert repository.get_freshness(SEASON).publication_status == "complete"
    assert repository.has_complete_publication(SEASON) is True

    router = DatabaseFirstGameLogsSource(
        LivePBPGameLogsSource(provider, _EventsFromDb(engine)),
        StoredGameLogsSource(repository),
        repository,
    )
    route_service = _service(engine, router)
    query = GameLogQuery(season_filter=SEASON)
    route_doc = route_service.get_filtered_logs("Player One", query)
    stored_doc = _stored_service(engine, repository).get_filtered_logs(
        "Player One", query
    )
    live_doc = _live_service(engine, provider).get_filtered_logs(
        "Player One", query
    )

    # An ingestion-complete valid publication serves database-first, and the
    # stored document strictly equals the live document (no plus/minus at all).
    assert router.cached(SEASON) is False
    assert route_doc == stored_doc == live_doc
    assert len(stored_doc["game_logs"]) == 2
    assert stored_doc["game_logs"][0]["MATCHUP"] == "AAA vs. BBB"


def test_stored_and_live_paths_agree_under_filters(durable_world):
    engine, repository, provider, _games = durable_world
    query = GameLogQuery(
        season_filter=SEASON,
        minutes_filter="15,48",
        location_filter="Home",
        self_filters={"PTS": "10,30"},
    )

    stored = _stored_service(engine, repository).get_filtered_logs(
        "Player One", query
    )
    live = _live_service(engine, provider).get_filtered_logs("Player One", query)

    assert stored == live
    assert stored["game_logs"]


def test_stored_and_live_paths_agree_on_recent_game_filter(durable_world):
    engine, repository, provider, _games = durable_world
    query = GameLogQuery(season_filter=SEASON, game_filter=1)

    stored = _stored_service(engine, repository).get_filtered_logs(
        "Player One", query
    )
    live = _live_service(engine, provider).get_filtered_logs("Player One", query)

    assert stored == live
    # game_filter keeps the leading newest-first rows, matching the legacy
    # NBA ordering the head() filter depends on.
    assert stored["game_logs"][0]["GAME_DATE"] == "2026-01-11"


def test_stored_path_serves_regular_season_only_like_the_live_path(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parity-playoffs.sqlite3'}")
    run_migrations(engine)
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
    )
    _seed_identities(repository)
    _seed_completed_playoff_event(repository)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE player_information ("id" INTEGER, "full_name" TEXT)'
        )
        connection.exec_driver_sql(
            "INSERT INTO player_information VALUES (101, 'Player One')"
        )
    provider = PlayerLogProvider(
        {
            "0022500001": _game_rows("0022500001", "2026-01-02", 1, 2),
            "0022500004": _game_rows("0022500004", "2026-01-11", 1, 2),
            "0042500001": _game_rows("0042500001", "2026-01-15", 1, 2),
        },
        regular_season_game_ids={"0022500001", "0022500004"},
    )
    ingest = PlayerGameLogIngestService(
        pbp_provider=provider,
        repository=repository,
        athlete_catalog=AthleteCatalogService(
            engine, nba_stats_provider=provider, freshness_days=7
        ),
        event_catalog=EventCatalogService(engine, nba_stats_provider=provider),
        minimum_active_players_per_team_game=5,
        reconciliation_days=3,
        clock=lambda: RETRIEVED_AT,
    )
    ingest.refresh(SEASON)

    stored = _stored_service(engine, repository).get_filtered_logs(
        "Player One", GameLogQuery(season_filter=SEASON)
    )
    live = _live_service(engine, provider).get_filtered_logs(
        "Player One", GameLogQuery(season_filter=SEASON)
    )

    assert stored == live
    # The playoff game is excluded from both paths, matching the legacy
    # Regular-Season-only request-time contract.
    assert len(stored["game_logs"]) == 2


def test_database_first_router_serves_complete_season_from_storage(durable_world):
    engine, repository, provider, _games = durable_world
    router = DatabaseFirstGameLogsSource(
        LivePBPGameLogsSource(provider, _EventsFromDb(engine)),
        StoredGameLogsSource(repository),
        repository,
    )

    assert router.cached(SEASON) is False
    frame = router.get_player_logs(101, SEASON)
    assert len(frame) == 2
    assert frame.iloc[0]["GAME_ID"] == "0022500004"  # newest first


def test_database_first_router_falls_back_to_live_before_complete_publication(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'parity-live.sqlite3'}")
    run_migrations(engine)
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
    )
    _seed_identities(repository)
    provider = PlayerLogProvider(
        {
            "0022500001": _game_rows("0022500001", "2026-01-02", 1, 2),
            "0022500004": _game_rows("0022500004", "2026-01-11", 1, 2),
        }
    )
    live = LivePBPGameLogsSource(provider, _EventsFromDb(engine))
    router = DatabaseFirstGameLogsSource(
        live,
        StoredGameLogsSource(repository),
        repository,
    )

    assert router.cached(SEASON) is True
    frame = router.get_player_logs(101, SEASON)
    assert len(frame) == 2
    assert frame.iloc[0]["GAME_ID"] == "0022500004"
