"""Durable player-game-log ingestion and request-time read parity.

The #66 contract amendment removes plus/minus from the game-log contract
entirely. An ingestion-complete, valid publication serves the Log Workspace
from the same canonical facts produced by durable PBP ingestion.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect, insert, text
from sqlalchemy.exc import InvalidRequestError

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.migrations import run_migrations
from app.models.collection_control import (
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.models.game_logs import GameLogQuery
from app.providers.pbp_game_logs import PBP_GAME_LOG_COLUMNS
from app.services.athlete_catalog_service import AthleteCatalogService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationRead,
    PublicationReadSnapshot,
)
from app.services import database_first_activation
from app.services.event_catalog_service import EventCatalogService
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.player_game_log_ingest import PlayerGameLogIngestService
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_player_game_logs import (
    RETRIEVED_AT,
    SEASON,
    _record,
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

    def fetch_game_player_logs(self, game_id, season, *, season_type):
        return _frame(self.games[game_id])


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


def _stored_service(engine, repository):
    return _service(
        engine,
        StoredGameLogsSource(repository),
    )


def test_complete_publication_serves_the_log_workspace(durable_world):
    engine, repository, _provider, _games = durable_world

    assert repository.get_freshness(SEASON).publication_status == "complete"
    assert repository.has_complete_publication(SEASON) is True

    route_service = _stored_service(engine, repository)
    query = GameLogQuery(season_filter=SEASON)
    route_doc = route_service.get_filtered_logs("Player One", query)

    assert len(route_doc["game_logs"]) == 2
    assert route_doc["game_logs"][0]["MATCHUP"] == "AAA vs. BBB"


def test_stored_path_applies_filters(durable_world):
    engine, repository, _provider, _games = durable_world
    query = GameLogQuery(
        season_filter=SEASON,
        minutes_filter="15,48",
        location_filter="Home",
        self_filters={"PTS": "10,30"},
    )

    stored = _stored_service(engine, repository).get_filtered_logs(
        "Player One", query
    )
    assert stored["game_logs"]


def test_stored_path_applies_recent_game_filter(durable_world):
    engine, repository, _provider, _games = durable_world
    query = GameLogQuery(season_filter=SEASON, game_filter=1)

    stored = _stored_service(engine, repository).get_filtered_logs(
        "Player One", query
    )
    # game_filter keeps the leading newest-first rows, matching the legacy
    # NBA ordering the head() filter depends on.
    assert stored["game_logs"][0]["GAME_DATE"] == "2026-01-11"


def test_stored_path_serves_regular_season_only(tmp_path):
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
    # The playoff game remains outside the Regular-Season-only wire contract.
    assert len(stored["game_logs"]) == 2


def test_stored_source_serves_complete_season(durable_world):
    _engine, repository, _provider, _games = durable_world
    frame = StoredGameLogsSource(repository).get_player_logs(101, SEASON)
    assert len(frame) == 2
    assert frame.iloc[0]["GAME_ID"] == "0022500004"  # newest first


def test_game_service_reads_an_active_player_log_publication_once(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'single-read.sqlite3'}")
    run_migrations(engine)
    publication = PublicationRead(
        stream_key="player_game_logs",
        publication_id="publication-1",
        season=SEASON,
        cutoff=RETRIEVED_AT.isoformat(),
        version=1,
        status="active",
        freshness="fresh",
        age_seconds=0,
        payload={"rows": []},
        retrieved_at=RETRIEVED_AT,
        checksum="a" * 64,
        fence=1,
        decoded=(_record(),),
    )
    snapshot = PublicationReadSnapshot(
        season=SEASON,
        reads={"player_game_logs": publication},
        generation=(("player_game_logs", "publication-1", 1, 1),),
    )

    class CountingPublicationReader:
        def __init__(self):
            self.read_calls = 0
            self.snapshot_calls = 0

        def read(self, stream_key, *, season):
            assert stream_key == "player_game_logs"
            assert season == SEASON
            self.read_calls += 1
            return publication

        def snapshot(self, stream_keys, *, season):
            assert tuple(stream_keys) == ("player_game_logs",)
            assert season == SEASON
            self.snapshot_calls += 1
            return snapshot

    reader = CountingPublicationReader()
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )

    source = StoredGameLogsSource(repository)
    service = _service(engine, source)
    monkeypatch.setattr(service, "get_player_id", lambda _name: 101)

    frame, next_team = service._get_game_logs("Player 101", SEASON)

    assert len(frame) == 1
    assert next_team is None
    assert reader.snapshot_calls == 1
    assert reader.read_calls == 0


def test_active_player_log_read_uses_indexed_publication_projection(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'projected-read.sqlite3'}")
    run_migrations(engine)
    records = (
        _record(),
        _record(player_id=202, game_id="0022500002"),
    )
    payload_rows = []
    for record in records:
        row = asdict(record)
        row["game_date"] = record.game_date.isoformat()
        payload_rows.append(row)
    encoded = json.dumps({"rows": payload_rows}, sort_keys=True)

    with engine.begin() as connection:
        connection.execute(insert(PublicationStream).values(
            stream_key="player_game_logs",
            provider="ledger",
            owner="railway",
            required_observations="[]",
            publication_strategy="ledger_compose",
            supported_windows='["season"]',
            schema_versions="[1]",
            completeness_rule="league_complete",
            freshness_rule="cutoff_current",
            enabled=True,
            created_at=RETRIEVED_AT,
        ))
        connection.execute(insert(PublicationVersion).values(
            publication_id="publication-1",
            stream_key="player_game_logs",
            season=SEASON,
            cutoff=RETRIEVED_AT,
            version=1,
            status="active",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=RETRIEVED_AT,
            reason="projection regression",
            fence=1,
        ))
        connection.execute(insert(PublicationPointer).values(
            stream_key="player_game_logs",
            active_publication_id="publication-1",
            previous_publication_id=None,
            fence=1,
            updated_at=RETRIEVED_AT,
        ))
        connection.execute(
            text(
                "INSERT INTO publication_player_game_logs "
                "(publication_id, player_id, game_id, game_date, "
                "opponent_team_id, row_payload) "
                "VALUES (:publication_id, :player_id, :game_id, :game_date, "
                ":opponent_team_id, :row_payload)"
            ),
            [
                {
                    "publication_id": "publication-1",
                    "player_id": row["player_id"],
                    "game_id": row["game_id"],
                    "game_date": row["game_date"],
                    "opponent_team_id": row["opponent_team_id"],
                    "row_payload": json.dumps(row, sort_keys=True),
                }
                for row in payload_rows
            ],
        )

    reader = DatabaseFirstPublicationReader(
        engine, clock=lambda: RETRIEVED_AT
    )
    original_read_row = reader._read_row

    def assert_payload_is_raiseloaded(stream_key, **kwargs):
        publication = kwargs["publication"]
        assert "payload" in inspect(publication).unloaded
        with pytest.raises(InvalidRequestError):
            publication.payload
        return original_read_row(stream_key, **kwargs)

    monkeypatch.setattr(reader, "_read_row", assert_payload_is_raiseloaded)
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )

    original_decoder = database_first_activation.decode_player_game_logs
    decoded_batch_sizes = []

    def record_decode_size(payload, **kwargs):
        payload_rows = payload.get("rows") if isinstance(payload, dict) else payload
        decoded_batch_sizes.append(len(payload_rows))
        return original_decoder(payload, **kwargs)

    monkeypatch.setattr(
        database_first_activation,
        "decode_player_game_logs",
        record_decode_size,
    )
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )

    snapshot = repository.read_publication_snapshot(SEASON)
    assert snapshot.read("player_game_logs").checksum == hashlib.sha256(
        encoded.encode()
    ).hexdigest()
    rows = repository.list_player_rows(
        SEASON, 101, publication_snapshot=snapshot
    )

    assert [row.player_id for row in rows] == [101]
    assert decoded_batch_sizes == [1]
    assert any("publication_player_game_logs" in statement for statement in statements)
    assert not any(
        "publication_versions.payload" in statement for statement in statements
    )
    with engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN "
            "SELECT row_payload FROM publication_player_game_logs "
            "WHERE publication_id = ? AND player_id = ? "
            "ORDER BY game_date DESC, game_id DESC",
            ("publication-1", 101),
        ).all()
    assert any(
        "ix_publication_player_game_logs_player_date" in str(step)
        for step in plan
    )


def test_stored_source_returns_empty_before_complete_publication():
    class IncompleteRepository:
        def has_complete_publication(self, season):
            assert season == SEASON
            return False

        def list_player_rows(self, season, player_id, *, publication_snapshot=None):
            assert season == SEASON
            assert player_id == 101
            assert publication_snapshot is None
            return (_record(),)

    repository = IncompleteRepository()
    frame = StoredGameLogsSource(repository).get_player_logs(101, SEASON)

    assert frame.empty


def _seed_player_log_projection(engine, records):
    """Write one active publication and its indexed rows for the given facts."""

    payload_rows = []
    for record in records:
        row = asdict(record)
        row["game_date"] = record.game_date.isoformat()
        payload_rows.append(row)
    encoded = json.dumps({"rows": payload_rows}, sort_keys=True)
    with engine.begin() as connection:
        connection.execute(insert(PublicationStream).values(
            stream_key="player_game_logs",
            provider="ledger",
            owner="railway",
            required_observations="[]",
            publication_strategy="ledger_compose",
            supported_windows='["season"]',
            schema_versions="[1]",
            completeness_rule="league_complete",
            freshness_rule="cutoff_current",
            enabled=True,
            created_at=RETRIEVED_AT,
        ))
        connection.execute(insert(PublicationVersion).values(
            publication_id="publication-1",
            stream_key="player_game_logs",
            season=SEASON,
            cutoff=RETRIEVED_AT,
            version=1,
            status="active",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=RETRIEVED_AT,
            reason="summaries projection regression",
            fence=1,
        ))
        connection.execute(insert(PublicationPointer).values(
            stream_key="player_game_logs",
            active_publication_id="publication-1",
            previous_publication_id=None,
            fence=1,
            updated_at=RETRIEVED_AT,
        ))
        connection.execute(
            text(
                "INSERT INTO publication_player_game_logs "
                "(publication_id, player_id, game_id, game_date, "
                "opponent_team_id, row_payload) "
                "VALUES (:publication_id, :player_id, :game_id, :game_date, "
                ":opponent_team_id, :row_payload)"
            ),
            [
                {
                    "publication_id": "publication-1",
                    "player_id": row["player_id"],
                    "game_id": row["game_id"],
                    "game_date": row["game_date"],
                    "opponent_team_id": row["opponent_team_id"],
                    "row_payload": json.dumps(row, sort_keys=True),
                }
                for row in payload_rows
            ],
        )


def _summaries_repository(engine, reader):
    return PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: RETRIEVED_AT,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )


def test_player_summaries_projection_matches_the_hydrated_payload_path(tmp_path):
    """The pool's summaries are identical whichever read shape supplies them."""

    engine = create_engine(f"sqlite:///{tmp_path / 'summaries.sqlite3'}")
    run_migrations(engine)
    records = (
        _record(),
        _record(game_id="0022500002", game_date=date(2026, 1, 5), minutes=28.0,
                points=19),
        _record(player_id=202, game_id="0022500003",
                game_date=date(2026, 1, 6), points=11),
    )
    _seed_player_log_projection(engine, records)
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)
    repository = _summaries_repository(engine, reader)

    hydrated = repository.get_player_summaries(
        SEASON,
        (101, 202),
        publication_snapshot=reader.snapshot(
            ("player_game_logs",), season=SEASON
        ),
    )

    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )
    projected = repository.get_player_summaries(
        SEASON,
        (101, 202),
        publication_snapshot=reader.snapshot(
            ("player_game_logs",),
            season=SEASON,
            projection_only_keys=frozenset({"player_game_logs"}),
        ),
    )

    assert projected == hydrated
    assert projected[101].last_ten_minutes == (32.5, 28.0)
    assert any(
        "publication_player_game_logs" in statement for statement in statements
    )
    assert not any(
        "publication_versions.payload" in statement for statement in statements
    )
    assert not any("player_game_logs.season" in statement for statement in statements)


def test_corrupt_player_summaries_projection_fails_closed(tmp_path):
    """A malformed projected row degrades instead of serving legacy facts."""

    engine = create_engine(f"sqlite:///{tmp_path / 'summaries-corrupt.sqlite3'}")
    run_migrations(engine)
    _seed_player_log_projection(engine, (_record(),))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE publication_player_game_logs SET row_payload = 'not-json'"
            )
        )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)
    repository = _summaries_repository(engine, reader)
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )

    summaries = repository.get_player_summaries(
        SEASON,
        (101,),
        publication_snapshot=reader.snapshot(
            ("player_game_logs",),
            season=SEASON,
            projection_only_keys=frozenset({"player_game_logs"}),
        ),
    )

    assert summaries[101].season_rate is None
    assert summaries[101].last_ten_minutes == ()
    assert not any(
        "FROM player_game_logs" in statement for statement in statements
    )
