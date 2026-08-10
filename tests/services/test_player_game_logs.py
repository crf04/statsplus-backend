"""Durable canonical player-game-log refresh and query behavior."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.event_catalog import EventCatalogEntry
from app.models.player_game_log import PlayerGameLog
from app.providers.nba_stats import normalize_season_player_game_logs
from app.services.player_game_log_repository import (
    PlayerGameLogFreshness,
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)
from app.services.player_game_log_service import PlayerGameLogIdentityError, PlayerGameLogService
from app.services.nba_stats_adapter import parse_recorded_game_logs


SEASON = "2025-26"
RETRIEVED_AT = datetime(2026, 1, 20, 10, tzinfo=timezone.utc)


def _record(
    *,
    player_id: int = 101,
    game_id: str = "0022500001",
    game_date: date = date(2026, 1, 2),
    team_id: int = 1,
    team_tricode: str = "AAA",
    opponent_team_id: int = 2,
    opponent_team_tricode: str = "BBB",
    minutes: float = 32.5,
    points: int = 25,
) -> PlayerGameLogRecord:
    return PlayerGameLogRecord(
        season=SEASON,
        player_id=player_id,
        game_id=game_id,
        player_name=f"Player {player_id}",
        game_date=game_date,
        team_id=team_id,
        team_tricode=team_tricode,
        opponent_team_id=opponent_team_id,
        opponent_team_tricode=opponent_team_tricode,
        is_home=True,
        minutes=minutes,
        points=points,
        rebounds=8,
        assists=7,
        field_goals_made=9,
        field_goals_attempted=18,
        three_pointers_made=3,
        three_pointers_attempted=7,
        turnovers=4,
        steals=2,
        blocks=1,
    )


def _repository(tmp_path) -> PlayerGameLogRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'player-logs.sqlite3'}")
    run_migrations(engine)
    return PlayerGameLogRepository(engine)


def _recorded_season_frame():
    path = Path(__file__).parents[1] / "fixtures/nba_stats/player_game_logs.season.json"
    return normalize_season_player_game_logs(
        parse_recorded_game_logs(json.loads(path.read_text()))
    )


def _seed_identities(repository: PlayerGameLogRepository) -> None:
    published_at = datetime(2025, 10, 1, tzinfo=timezone.utc)
    with repository.engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [
                {
                    "season": SEASON,
                    "player_id": player_id,
                    "display_name": name,
                    "roster_status": "active",
                    "is_active": True,
                    "is_active_for_season": True,
                    "team_id": team_id,
                    "team_name": team_tricode,
                    "team_abbreviation": team_tricode,
                    "published_at": published_at,
                }
                for player_id, name, team_id, team_tricode in (
                    (101, "Canonical One", 3, "CCC"),
                    (202, "Canonical Two", 2, "BBB"),
                )
            ],
        )
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [
                {
                    "nba_game_id": game_id,
                    "season": SEASON,
                    "home_team_id": home_id,
                    "home_team_name": home_code,
                    "home_team_tricode": home_code,
                    "away_team_id": away_id,
                    "away_team_name": away_code,
                    "away_team_tricode": away_code,
                    "scheduled_at": datetime.combine(game_date, datetime.min.time(), timezone.utc),
                    "status_text": "Final",
                    "status_code": 3,
                    "postponed_status": None,
                    "postponement_evidence": None,
                    "classification": "Regular Season",
                    "first_seen_at": published_at,
                    "last_seen_at": published_at,
                }
                for game_id, game_date, home_id, home_code, away_id, away_code in (
                    ("0022500001", date(2026, 1, 2), 1, "AAA", 2, "BBB"),
                    ("0022500002", date(2026, 1, 5), 4, "DDD", 3, "CCC"),
                    ("0022500003", date(2026, 1, 8), 2, "BBB", 3, "CCC"),
                )
            ],
        )


def test_publish_deduplicates_identical_player_game_facts_and_records_freshness(
    tmp_path,
):
    repository = _repository(tmp_path)
    record = _record()

    assert repository.publish(
        SEASON,
        [record, record],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
    ) == 1

    assert repository.list_player_rows(SEASON, 101) == (record,)
    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=RETRIEVED_AT,
        row_count=1,
    )


def test_failed_replacement_rolls_back_rows_and_freshness(tmp_path):
    repository = _repository(tmp_path)
    original = _record()
    repository.publish(
        SEASON,
        [original],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
    )

    invalid = replace(original, game_id="0022500999", player_name=None)
    with pytest.raises(IntegrityError):
        repository.publish(
            SEASON,
            [invalid],
            retrieved_at=RETRIEVED_AT + timedelta(days=1),
            source_provider="nba_stats",
        )

    assert repository.list_player_rows(SEASON, 101) == (original,)
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_conflicting_duplicate_identity_preserves_last_valid_publication(tmp_path):
    repository = _repository(tmp_path)
    original = _record()
    repository.publish(
        SEASON,
        [original],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
    )

    with pytest.raises(ValueError, match="conflicting player game log"):
        repository.publish(
            SEASON,
            [original, replace(original, points=99)],
            retrieved_at=RETRIEVED_AT + timedelta(days=1),
            source_provider="nba_stats",
        )

    assert repository.list_player_rows(SEASON, 101) == (original,)
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_rows_without_complete_publication_metadata_fail_closed(tmp_path):
    repository = _repository(tmp_path)
    with repository.engine.begin() as connection:
        connection.execute(insert(PlayerGameLog.__table__).values(**asdict(_record())))

    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider=None,
        retrieved_at=None,
        row_count=0,
    )
    assert repository.list_player_rows(SEASON, 101) == ()


def test_queries_derive_rates_last_ten_h2h_and_archetype_rows(tmp_path):
    repository = _repository(tmp_path)
    start = date(2026, 1, 1)
    player_rows = [
        replace(
            _record(),
            game_id=f"002250{i:04d}",
            game_date=start + timedelta(days=i - 1),
            minutes=float(i),
            points=i * 2,
            team_id=1 if i <= 6 else 3,
            team_tricode="AAA" if i <= 6 else "CCC",
        )
        for i in range(1, 13)
    ]
    archetype_row = replace(
        _record(player_id=202),
        game_id="0022500202",
        game_date=date(2026, 1, 7),
    )
    repository.publish(
        SEASON,
        [*reversed(player_rows), archetype_row],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
    )

    rate = repository.get_season_rate(SEASON, 101)
    assert rate is not None
    assert rate.game_count == 12
    assert rate.total_minutes == 78
    assert rate.per_game["PTS"] == 13
    assert rate.per_minute["PTS"] == 2
    assert rate.per_game["3PM"] == 3
    assert rate.per_game["FG2A"] == 11
    assert rate.per_game["PRA"] == 28
    assert repository.get_last_ten_minutes(SEASON, 101) == tuple(
        float(value) for value in range(3, 13)
    )
    assert [row.game_id for row in repository.list_h2h_rows(SEASON, 101, 2)] == [
        f"002250{i:04d}" for i in range(12, 0, -1)
    ]
    assert {row.team_id for row in repository.list_h2h_rows(SEASON, 101, 2)} == {
        1,
        3,
    }
    assert [
        (row.game_date, row.player_id)
        for row in repository.list_archetype_rows(SEASON, [202, 101], 2)
    ] == [
        *((start + timedelta(days=i - 1), 101) for i in range(12, 7, -1)),
        (date(2026, 1, 7), 101),
        (date(2026, 1, 7), 202),
        *((start + timedelta(days=i - 1), 101) for i in range(6, 0, -1)),
    ]


class _RecordedSeasonProvider:
    def __init__(self, frame=None):
        self.frame = frame if frame is not None else _recorded_season_frame()
        self.calls = []

    def get_season_player_game_logs(self, *, season, season_type="Regular Season"):
        self.calls.append((season, season_type))
        return self.frame.copy()


def test_refresh_canonicalizes_recorded_season_rows_without_per_player_calls(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = PlayerGameLogService(
        repository.engine,
        nba_stats_provider=provider,
        repository=repository,
        clock=lambda: RETRIEVED_AT,
    )

    result = service.refresh(SEASON)

    assert result.row_count == 4
    assert result.retrieved_at == RETRIEVED_AT.isoformat()
    assert provider.calls == [(SEASON, "Regular Season")]
    player_rows = repository.list_player_rows(SEASON, 101)
    assert [row.team_id for row in player_rows] == [3, 1]
    assert [row.opponent_team_id for row in player_rows] == [4, 2]
    assert [row.player_name for row in player_rows] == ["Canonical One"] * 2
    assert player_rows[0].minutes == 34.25
    assert player_rows[0].field_goals_attempted == 21
    assert player_rows[0].three_pointers_made == 4


def test_unjoined_player_identity_preserves_last_valid_publication(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = PlayerGameLogService(
        repository.engine,
        nba_stats_provider=provider,
        repository=repository,
        clock=lambda: RETRIEVED_AT,
    )
    service.refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)

    provider.frame.loc[0, "PLAYER_ID"] = 999
    with pytest.raises(PlayerGameLogIdentityError, match="athlete identity"):
        service.refresh(SEASON, now=RETRIEVED_AT + timedelta(days=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
