"""Durable canonical player-game-log refresh and query behavior."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, delete, event, insert, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog, AthleteCatalogFreshness
from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh
from app.models.player_game_log import PlayerGameLog
from app.models.stats_freshness import StatsRefresh
from app.providers.nba_stats import normalize_season_player_game_logs
from app.services.player_game_log_repository import (
    PlayerGameLogFreshness,
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)
from app.services.player_game_log_service import PlayerGameLogIdentityError, PlayerGameLogService
from app.services.nba_stats_adapter import parse_recorded_game_logs
from app.services.athlete_catalog_service import AthleteCatalogService
from app.services.event_catalog_service import EventCatalogService
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import (
    PLAYER_GAME_LOG_SURFACE,
    StatsFreshness,
    StatsFreshnessRepository,
)
from app.utils.telemetry import PlayerGameLogTelemetryEvent


SEASON = "2025-26"
HISTORICAL_SEASON = "2024-25"
RETRIEVED_AT = datetime(2026, 1, 20, 10, tzinfo=timezone.utc)


def _record(
    *,
    season: str = SEASON,
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
        season=season,
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


def _repository(
    tmp_path,
    *,
    stats_surface_season: str = SEASON,
    clock=lambda: RETRIEVED_AT,
    stats_surface_max_age: timedelta = timedelta(hours=30),
) -> PlayerGameLogRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'player-logs.sqlite3'}")
    run_migrations(engine)
    return PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=stats_surface_season,
        clock=clock,
        stats_surface_max_age=stats_surface_max_age,
    )


def test_repository_requires_an_explicit_stats_surface_season(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'player-logs.sqlite3'}")
    run_migrations(engine)

    with pytest.raises(TypeError, match="stats_surface_season"):
        PlayerGameLogRepository(
            engine,
            statistic_catalog=StatisticCatalog.load_default(),
            stats_surface_max_age=timedelta(hours=30),
        )


@pytest.mark.parametrize("stats_surface_season", [None, "", "   ", "2025-27"])
def test_repository_rejects_an_invalid_stats_surface_season(
    tmp_path, stats_surface_season
):
    engine = create_engine(f"sqlite:///{tmp_path / 'player-logs.sqlite3'}")
    run_migrations(engine)

    with pytest.raises(ValueError, match="season must be an explicit canonical"):
        PlayerGameLogRepository(
            engine,
            statistic_catalog=StatisticCatalog.load_default(),
            stats_surface_season=stats_surface_season,
            stats_surface_max_age=timedelta(hours=30),
        )


def _recorded_season_frame():
    path = Path(__file__).parents[1] / "fixtures/nba_stats/player_game_logs.season.json"
    return normalize_season_player_game_logs(
        parse_recorded_game_logs(json.loads(path.read_text()))
    )


def _seed_identities(
    repository: PlayerGameLogRepository,
    *,
    completed: bool = True,
    event_id_prefix: str = "002",
    classification: str = "Regular Season",
) -> None:
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
                    (103, "Canonical 103", 1, "AAA"),
                    (104, "Canonical 104", 1, "AAA"),
                    (105, "Canonical 105", 1, "AAA"),
                    (106, "Canonical 106", 1, "AAA"),
                    (107, "Canonical 107", 2, "BBB"),
                    (108, "Canonical 108", 2, "BBB"),
                    (109, "Canonical 109", 2, "BBB"),
                    (110, "Canonical 110", 2, "BBB"),
                    (111, "Canonical 111", 1, "AAA"),
                    (112, "Canonical 112", 1, "AAA"),
                    (113, "Canonical 113", 1, "AAA"),
                )
            ],
        )
        connection.execute(
            insert(AthleteCatalogFreshness.__table__).values(
                season=SEASON,
                last_success_at=RETRIEVED_AT,
                last_success_row_count=13,
                last_failure_at=None,
                last_failure_summary=None,
                updated_at=RETRIEVED_AT,
            )
        )
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [
                {
                    "nba_game_id": f"{event_id_prefix}{game_id[3:]}",
                    "season": SEASON,
                    "home_team_id": home_id,
                    "home_team_name": home_code,
                    "home_team_tricode": home_code,
                    "away_team_id": away_id,
                    "away_team_name": away_code,
                    "away_team_tricode": away_code,
                    "scheduled_at": datetime.combine(game_date, datetime.min.time(), timezone.utc),
                    "status_text": (
                        "Final"
                        if completed and game_id in {"0022500001", "0022500004"}
                        else "Scheduled"
                    ),
                    "status_code": (
                        3
                        if completed and game_id in {"0022500001", "0022500004"}
                        else 1
                    ),
                    "postponed_status": None,
                    "postponement_evidence": None,
                    "classification": classification,
                    "first_seen_at": published_at,
                    "last_seen_at": published_at,
                }
                for game_id, game_date, home_id, home_code, away_id, away_code in (
                    ("0022500001", date(2026, 1, 2), 1, "AAA", 2, "BBB"),
                    ("0022500002", date(2026, 1, 5), 4, "DDD", 3, "CCC"),
                    ("0022500003", date(2026, 1, 8), 2, "BBB", 3, "CCC"),
                    ("0022500004", date(2026, 1, 11), 1, "AAA", 2, "BBB"),
                )
            ],
        )
        connection.execute(
            insert(EventCatalogRefresh.__table__).values(
                season=SEASON,
                last_attempt_at=RETRIEVED_AT,
                last_success_at=RETRIEVED_AT,
                last_failure_at=None,
                failure_summary=None,
                event_count=4,
            )
        )


def _remove_player_from_fresh_catalog(
    repository: PlayerGameLogRepository, player_id: int
) -> None:
    refreshed_at = RETRIEVED_AT + timedelta(days=1)
    with repository.engine.begin() as connection:
        connection.execute(
            delete(AthleteCatalog.__table__).where(
                AthleteCatalog.season == SEASON,
                AthleteCatalog.player_id == player_id,
            )
        )
        connection.execute(
            update(AthleteCatalogFreshness.__table__)
            .where(AthleteCatalogFreshness.season == SEASON)
            .values(
                last_success_at=refreshed_at,
                last_success_row_count=12,
                updated_at=refreshed_at,
            )
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
        source_row_count=2,
    ).row_count == 1

    assert repository.list_player_rows(SEASON, 101) == (record,)
    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=RETRIEVED_AT,
        row_count=1,
        source_row_count=2,
    )
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get() == StatsFreshness(
        last_successful_completion=RETRIEVED_AT,
        surface=PLAYER_GAME_LOG_SURFACE,
    )


@pytest.mark.parametrize(
    ("source_row_count", "message"),
    [
        pytest.param(-1, "non-negative integer", id="negative"),
        pytest.param(True, "non-negative integer", id="boolean"),
        pytest.param(0, "smaller than canonical input", id="below-canonical-count"),
    ],
)
def test_publish_rejects_invalid_source_row_coverage(
    tmp_path, source_row_count, message
):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match=message):
        repository.publish(
            SEASON,
            [_record()],
            retrieved_at=RETRIEVED_AT,
            source_provider="nba_stats",
            source_row_count=source_row_count,
        )

    assert repository.get_freshness(SEASON).retrieved_at is None


def test_publish_rejects_source_count_below_canonical_input_coverage(tmp_path):
    repository = _repository(tmp_path)
    record = _record()

    with pytest.raises(ValueError, match="smaller than canonical input"):
        repository.publish(
            SEASON,
            [record, record],
            retrieved_at=RETRIEVED_AT,
            source_provider="nba_stats",
            source_row_count=1,
        )

    assert repository.get_freshness(SEASON).retrieved_at is None


def test_publish_rejects_a_record_from_another_season(tmp_path):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="belong to the publication season"):
        repository.publish(
            SEASON,
            [_record(season=HISTORICAL_SEASON)],
            retrieved_at=RETRIEVED_AT,
            source_provider="nba_stats",
            source_row_count=1,
        )

    assert repository.get_freshness(SEASON).retrieved_at is None


def test_historical_backfill_after_current_does_not_hide_either_season(tmp_path):
    repository = _repository(tmp_path)
    current = _record()
    historical = _record(
        season=HISTORICAL_SEASON,
        game_id="0022400001",
        game_date=date(2025, 1, 2),
    )
    current_retrieved_at = RETRIEVED_AT
    historical_retrieved_at = RETRIEVED_AT + timedelta(hours=1)

    repository.publish(
        SEASON,
        [current],
        retrieved_at=current_retrieved_at,
        source_provider="nba_stats",
        source_row_count=1,
    )
    repository.publish(
        HISTORICAL_SEASON,
        [historical],
        retrieved_at=historical_retrieved_at,
        source_provider="nba_stats",
        source_row_count=1,
    )

    assert repository.list_player_rows(SEASON, current.player_id) == (current,)
    assert repository.list_player_rows(
        HISTORICAL_SEASON, historical.player_id
    ) == (historical,)
    assert repository.get_freshness(SEASON).retrieved_at == current_retrieved_at
    assert (
        repository.get_freshness(HISTORICAL_SEASON).retrieved_at
        == historical_retrieved_at
    )
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion == current_retrieved_at


def test_current_publication_after_historical_keeps_both_seasons_consistent(tmp_path):
    repository = _repository(tmp_path)
    historical = _record(
        season=HISTORICAL_SEASON,
        game_id="0022400001",
        game_date=date(2025, 1, 2),
    )
    current = _record()
    historical_retrieved_at = RETRIEVED_AT - timedelta(hours=1)
    current_retrieved_at = RETRIEVED_AT

    repository.publish(
        HISTORICAL_SEASON,
        [historical],
        retrieved_at=historical_retrieved_at,
        source_provider="nba_stats",
        source_row_count=1,
    )
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion is None
    repository.publish(
        SEASON,
        [current],
        retrieved_at=current_retrieved_at,
        source_provider="nba_stats",
        source_row_count=1,
    )

    assert repository.list_player_rows(
        HISTORICAL_SEASON, historical.player_id
    ) == (historical,)
    assert repository.list_player_rows(SEASON, current.player_id) == (current,)
    assert (
        repository.get_freshness(HISTORICAL_SEASON).retrieved_at
        == historical_retrieved_at
    )
    assert repository.get_freshness(SEASON).retrieved_at == current_retrieved_at
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion == current_retrieved_at


def test_failed_replacement_rolls_back_rows_and_freshness(tmp_path):
    repository = _repository(tmp_path)
    original = _record()
    repository.publish(
        SEASON,
        [original],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )

    invalid = replace(original, player_name=None)
    with pytest.raises(IntegrityError):
        repository.publish(
            SEASON,
            [invalid],
            retrieved_at=RETRIEVED_AT + timedelta(days=1),
            source_provider="nba_stats",
            source_row_count=1,
        )

    assert repository.list_player_rows(SEASON, 101) == (original,)
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion == RETRIEVED_AT


@pytest.mark.parametrize("allow_empty", [False, True])
def test_empty_replacement_preserves_last_valid_rows_and_freshness(
    tmp_path, allow_empty
):
    repository = _repository(tmp_path)
    original = _record()
    repository.publish(
        SEASON,
        [original],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )

    with pytest.raises(ValueError, match="empty player game log snapshot"):
        repository.publish(
            SEASON,
            [],
            retrieved_at=RETRIEVED_AT + timedelta(days=1),
            source_provider="nba_stats",
            source_row_count=0,
            allow_empty=allow_empty,
        )

    assert repository.list_player_rows(SEASON, 101) == (original,)
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_repository_defensively_rejects_conflicting_duplicate_identity(tmp_path):
    repository = _repository(tmp_path)
    original = _record()
    repository.publish(
        SEASON,
        [original],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )

    with pytest.raises(ValueError, match="conflicting player game log"):
        repository.publish(
            SEASON,
            [original, replace(original, points=99)],
            retrieved_at=RETRIEVED_AT + timedelta(days=1),
            source_provider="nba_stats",
            source_row_count=2,
        )

    assert repository.list_player_rows(SEASON, 101) == (original,)
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_truncated_cumulative_snapshot_preserves_larger_publication(tmp_path):
    repository = _repository(tmp_path)
    complete = [
        replace(
            _record(),
            player_id=1000 + index,
            game_id=f"002250{index:04d}",
            player_name=f"Player {index}",
        )
        for index in range(25)
    ]
    repository.publish(
        SEASON,
        complete,
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=25,
    )

    with pytest.raises(ValueError, match="remove.*cumulative"):
        repository.publish(
            SEASON,
            complete[:1],
            retrieved_at=RETRIEVED_AT + timedelta(days=1),
            source_provider="nba_stats",
            source_row_count=1,
        )

    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=RETRIEVED_AT,
        row_count=25,
        source_row_count=25,
    )


def test_same_size_and_growing_cumulative_snapshots_can_replace(tmp_path):
    repository = _repository(tmp_path)
    first = _record()
    second = replace(first, player_id=202, game_id="0022500002")
    third = replace(first, player_id=303, game_id="0022500003")
    repository.publish(
        SEASON,
        [first, second],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=2,
    )

    assert repository.publish(
        SEASON,
        [replace(first, points=26), second],
        retrieved_at=RETRIEVED_AT + timedelta(days=1),
        source_provider="nba_stats",
        source_row_count=2,
    ).row_count == 2
    assert repository.publish(
        SEASON,
        [replace(first, points=27), second, third],
        retrieved_at=RETRIEVED_AT + timedelta(days=2),
        source_provider="nba_stats",
        source_row_count=3,
    ).row_count == 3
    assert repository.get_freshness(SEASON).row_count == 3
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT + timedelta(
        days=2
    )


def test_rows_without_complete_publication_metadata_fail_closed(tmp_path):
    repository = _repository(tmp_path)
    with repository.engine.begin() as connection:
        connection.execute(insert(PlayerGameLog.__table__).values(**asdict(_record())))

    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider=None,
        retrieved_at=None,
        row_count=0,
        source_row_count=0,
    )
    assert repository.list_player_rows(SEASON, 101) == ()


def test_current_season_reads_require_global_surface_observation(tmp_path):
    repository = _repository(tmp_path)
    record = _record()
    historical = replace(
        record,
        season=HISTORICAL_SEASON,
        game_id="0022400001",
        game_date=date(2025, 1, 2),
    )
    repository.publish(
        SEASON,
        [record],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )
    repository.publish(
        HISTORICAL_SEASON,
        [historical],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )
    with repository.engine.begin() as connection:
        connection.execute(
            delete(StatsRefresh.__table__).where(
                StatsRefresh.surface == PLAYER_GAME_LOG_SURFACE
            )
        )

    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=RETRIEVED_AT,
        row_count=1,
        source_row_count=1,
    )
    assert repository.list_player_rows(SEASON, 101) == ()
    assert repository.list_player_rows(HISTORICAL_SEASON, 101) == (historical,)
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion is None


def test_stale_current_surface_fails_closed_without_hiding_historical_rows(tmp_path):
    repository = _repository(
        tmp_path, clock=lambda: RETRIEVED_AT + timedelta(hours=31)
    )
    current = _record()
    historical = replace(
        current,
        season=HISTORICAL_SEASON,
        game_id="0022400001",
        game_date=date(2025, 1, 2),
    )
    repository.publish(
        SEASON,
        [current],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )
    repository.publish(
        HISTORICAL_SEASON,
        [historical],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )

    assert repository.list_player_rows(SEASON, 101) == ()
    assert repository.list_player_rows(HISTORICAL_SEASON, 101) == (historical,)
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_configured_current_surface_max_age_is_enforced(tmp_path):
    repository = _repository(
        tmp_path,
        clock=lambda: RETRIEVED_AT + timedelta(hours=2),
        stats_surface_max_age=timedelta(hours=1),
    )
    record = _record()
    repository.publish(
        SEASON,
        [record],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=1,
    )

    assert repository.list_player_rows(SEASON, 101) == ()
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


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
        source_row_count=13,
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
    assert set(rate.per_game) == {
        statistic.market_category
        for statistic in StatisticCatalog.load_default().statistics
        if statistic.market_category is not None
    }
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


class _RecordingTelemetry:
    def __init__(self):
        self.events: list[PlayerGameLogTelemetryEvent] = []

    def record(self, event):
        self.events.append(event)


def _service(
    repository: PlayerGameLogRepository,
    provider: _RecordedSeasonProvider,
    *,
    clock=lambda: RETRIEVED_AT,
    telemetry_recorder=None,
    minimum_active_players_per_team_game: int = 5,
) -> PlayerGameLogService:
    return PlayerGameLogService(
        nba_stats_provider=provider,
        repository=repository,
        athlete_catalog=AthleteCatalogService(
            repository.engine, nba_stats_provider=provider, freshness_days=7
        ),
        event_catalog=EventCatalogService(
            repository.engine, nba_stats_provider=provider
        ),
        clock=clock,
        telemetry_recorder=telemetry_recorder,
        minimum_active_players_per_team_game=(
            minimum_active_players_per_team_game
        ),
    )


def test_refresh_canonicalizes_recorded_season_rows_without_per_player_calls(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = _service(repository, provider)

    result = service.refresh(SEASON)

    assert result.row_count == 22
    assert result.retrieved_at == RETRIEVED_AT.isoformat()
    assert provider.calls == [(SEASON, "Regular Season")]
    player_rows = repository.list_player_rows(SEASON, 101)
    assert [row.team_id for row in player_rows] == [1, 3, 1]
    assert [row.opponent_team_id for row in player_rows] == [2, 4, 2]
    assert [row.player_name for row in player_rows] == ["Canonical One"] * 3
    assert player_rows[1].minutes == 34.25
    assert player_rows[1].field_goals_attempted == 21
    assert player_rows[1].three_pointers_made == 4
    assert [
        row.game_id for row in repository.list_h2h_rows(SEASON, 101, 2)
    ] == ["0022500004", "0022500001"]
    assert [
        (row.game_id, row.player_id)
        for row in repository.list_archetype_rows(SEASON, [202, 101], 2)
    ] == [
        ("0022500004", 101),
        ("0022500004", 202),
        ("0022500001", 101),
    ]


def test_first_publication_rejects_a_truncated_completed_game_snapshot(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[:1].copy())
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="completed game coverage"):
        _service(repository, provider, telemetry_recorder=telemetry).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None
    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=1,
            published_row_count=0,
            unjoined_athlete_count=0,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
        )
    ]


@pytest.mark.parametrize("missing_coverage", ["game", "team", "player"])
def test_first_publication_requires_exact_completed_game_coverage(
    tmp_path, missing_coverage
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    frame = _recorded_season_frame()
    if missing_coverage == "game":
        frame = frame[frame["GAME_ID"] != "0022500004"]
    elif missing_coverage == "team":
        frame = frame[
            ~(
                (frame["GAME_ID"] == "0022500001")
                & (frame["TEAM_ID"] == 2)
            )
        ]
    else:
        missing_player = (
            (frame["GAME_ID"] == "0022500001")
            & (frame["TEAM_ID"] == 1)
            & (frame["PLAYER_ID"] == 106)
        )
        frame.loc[missing_player, "MIN"] = 0
    provider = _RecordedSeasonProvider(frame.reset_index(drop=True))
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="completed game coverage"):
        _service(repository, provider, telemetry_recorder=telemetry).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None
    assert telemetry.events[0].source_row_count == len(frame.index)
    assert telemetry.events[0].rejected_publication_count == 1


def test_completed_game_coverage_uses_the_injected_sport_minimum(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="completed game coverage"):
        _service(
            repository,
            _RecordedSeasonProvider(),
            telemetry_recorder=telemetry,
            minimum_active_players_per_team_game=6,
        ).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None
    assert telemetry.events[0].rejected_publication_count == 1


@pytest.mark.parametrize("minimum", [0, True, 1.5])
def test_service_rejects_an_invalid_sport_minimum(tmp_path, minimum):
    with pytest.raises(ValueError, match="positive integer"):
        _service(
            _repository(tmp_path),
            _RecordedSeasonProvider(),
            minimum_active_players_per_team_game=minimum,
        )


def test_eligible_game_removals_reject_even_when_additions_create_net_growth(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    extras = provider.frame.iloc[[6]].copy()
    provider.frame = pd.concat(
        [
            provider.frame,
            *[extras.assign(PLAYER_ID=player_id) for player_id in (111, 112, 113)],
        ],
        ignore_index=True,
    )
    _service(repository, provider).refresh(SEASON)
    prior = repository.get_freshness(SEASON)
    base = _recorded_season_frame()
    addition = base.iloc[[2]].copy()
    provider.frame = pd.concat(
        [
            base,
            *[
                addition.assign(PLAYER_ID=player_id)
                for player_id in (202, *range(103, 114))
            ],
        ],
        ignore_index=True,
    )
    telemetry = _RecordingTelemetry()

    with pytest.raises(ValueError, match="remove.*cumulative"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(hours=1))

    assert len(provider.frame.index) == 34
    assert repository.get_freshness(SEASON) == prior
    assert {row.game_id for row in repository.list_player_rows(SEASON, 111)} == {
        "0022500001"
    }
    assert telemetry.events[0].rejected_publication_count == 1


def test_voided_game_removals_with_an_addition_report_actual_recovery(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    game_two = provider.frame.iloc[[2]].copy()
    provider.frame = pd.concat(
        [
            provider.frame,
            game_two.assign(PLAYER_ID=111),
            game_two.assign(PLAYER_ID=112),
        ],
        ignore_index=True,
    )
    _service(repository, provider).refresh(SEASON)
    with repository.engine.begin() as connection:
        connection.execute(
            delete(EventCatalogEntry.__table__).where(
                EventCatalogEntry.nba_game_id == "0022500002"
            )
        )
        connection.execute(
            update(EventCatalogRefresh.__table__).values(event_count=3)
        )
    provider.frame = provider.frame[
        provider.frame["GAME_ID"] != "0022500002"
    ].reset_index(drop=True)
    added = provider.frame.iloc[[2]].copy()
    added["PLAYER_ID"] = 111
    provider.frame = pd.concat([provider.frame, added], ignore_index=True)
    telemetry = _RecordingTelemetry()

    result = _service(
        repository, provider, telemetry_recorder=telemetry
    ).refresh(SEASON, now=RETRIEVED_AT + timedelta(hours=1))

    assert result.row_count == 22
    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=RETRIEVED_AT + timedelta(hours=1),
        row_count=22,
        source_row_count=22,
    )
    assert telemetry.events[0].recovered_shrink_row_count == 3
    assert telemetry.events[0].rejected_publication_count == 0


def test_cumulative_growth_without_removed_identities_succeeds(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    added = provider.frame.iloc[[2]].copy()
    added["PLAYER_ID"] = 111
    provider.frame = pd.concat([provider.frame, added], ignore_index=True)
    telemetry = _RecordingTelemetry()

    result = _service(
        repository, provider, telemetry_recorder=telemetry
    ).refresh(SEASON, now=RETRIEVED_AT + timedelta(hours=1))

    assert result.row_count == 23
    assert repository.get_freshness(SEASON).source_row_count == 23
    assert telemetry.events[0].recovered_shrink_row_count == 0


def test_refresh_timestamp_is_observed_after_provider_fetch(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    observations: list[str] = []

    class ObservedProvider(_RecordedSeasonProvider):
        def get_season_player_game_logs(self, **kwargs):
            observations.append("provider")
            return super().get_season_player_game_logs(**kwargs)

    provider = ObservedProvider()

    def clock():
        observations.append("clock")
        return RETRIEVED_AT

    _service(repository, provider, clock=clock).refresh(SEASON)

    assert observations == ["provider", "clock"]


def test_non_dataframe_provider_result_records_rejected_publication(tmp_path):
    repository = _repository(tmp_path)
    provider = _RecordedSeasonProvider([{"PLAYER_ID": 101}])
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="data frame"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON)

    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=0,
            published_row_count=0,
            unjoined_athlete_count=0,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
        )
    ]


def test_refresh_collapses_and_counts_exact_duplicate_source_rows(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    provider.frame = pd.concat(
        [provider.frame, provider.frame.iloc[[0]].copy()], ignore_index=True
    )
    telemetry = _RecordingTelemetry()

    result = _service(
        repository, provider, telemetry_recorder=telemetry
    ).refresh(SEASON)

    assert result.row_count == 22
    assert telemetry.events[0].source_row_count == 23
    assert telemetry.events[0].published_row_count == 22
    assert telemetry.events[0].duplicate_row_count == 1
    assert telemetry.events[0].rejected_publication_count == 0


def test_preseason_empty_snapshot_can_record_honest_zero_row_freshness(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository, completed=False)
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[0:0])

    result = _service(repository, provider).refresh(SEASON)

    assert result.row_count == 0
    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=RETRIEVED_AT,
        row_count=0,
        source_row_count=0,
    )


@pytest.mark.parametrize("catalog_state", ["missing", "stale", "count_mismatch"])
def test_empty_snapshot_requires_complete_fresh_event_catalog(tmp_path, catalog_state):
    repository = _repository(tmp_path)
    _seed_identities(repository, completed=False)
    observed_at = RETRIEVED_AT + timedelta(hours=1)
    if catalog_state == "missing":
        with repository.engine.begin() as connection:
            connection.execute(
                delete(EventCatalogRefresh.__table__).where(
                    EventCatalogRefresh.season == SEASON
                )
            )
    elif catalog_state == "stale":
        observed_at = RETRIEVED_AT + timedelta(hours=73)
    else:
        with repository.engine.begin() as connection:
            connection.execute(
                update(EventCatalogRefresh.__table__)
                .where(EventCatalogRefresh.season == SEASON)
                .values(event_count=2)
            )
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[0:0])
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="Event Catalog.*fresh"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=observed_at)

    assert repository.get_freshness(SEASON).retrieved_at is None
    assert StatsFreshnessRepository(
        repository.engine, surface=PLAYER_GAME_LOG_SURFACE
    ).get().last_successful_completion is None
    assert telemetry.events[0].rejected_publication_count == 1


def test_preseason_final_before_opening_night_allows_empty_regular_season_snapshot(
    tmp_path,
):
    repository = _repository(tmp_path)
    _seed_identities(
        repository,
        completed=True,
        event_id_prefix="001",
        classification="Preseason",
    )
    with repository.engine.begin() as connection:
        connection.execute(
            update(EventCatalogEntry.__table__).values(
                scheduled_at=datetime(2025, 10, 5, tzinfo=timezone.utc)
            )
        )
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[0:0])

    result = _service(repository, provider).refresh(SEASON)

    assert result.row_count == 0
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_completed_all_star_game_does_not_block_empty_regular_season_snapshot(
    tmp_path,
):
    repository = _repository(tmp_path)
    _seed_identities(
        repository,
        completed=True,
        event_id_prefix="003",
        classification="All-Star",
    )
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[0:0])

    result = _service(repository, provider).refresh(SEASON)

    assert result.row_count == 0
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_postponed_regular_season_event_with_final_code_allows_empty_snapshot(
    tmp_path,
):
    repository = _repository(tmp_path)
    _seed_identities(repository, completed=True)
    with repository.engine.begin() as connection:
        connection.execute(
            update(EventCatalogEntry.__table__).values(
                postponed_status="postponed"
            )
        )
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[0:0])

    result = _service(repository, provider).refresh(SEASON)

    assert result.row_count == 0
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT


def test_empty_snapshot_without_event_catalog_rows_fails_closed(tmp_path):
    repository = _repository(tmp_path)
    provider = _RecordedSeasonProvider(_recorded_season_frame().iloc[0:0])
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="Event Catalog.*present"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON)

    assert repository.get_freshness(SEASON).retrieved_at is None
    assert telemetry.events[0].source_row_count == 0
    assert telemetry.events[0].rejected_publication_count == 1


def test_empty_snapshot_with_completed_regular_season_games_preserves_publication(
    tmp_path,
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = _service(repository, provider)
    service.refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)

    provider.frame = provider.frame.iloc[0:0]
    telemetry = _RecordingTelemetry()
    with pytest.raises(PlayerGameLogIdentityError, match="empty snapshot"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(days=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events[0].source_row_count == 0
    assert telemetry.events[0].rejected_publication_count == 1


def test_service_records_repository_rejection_for_empty_over_nonempty(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    with repository.engine.begin() as connection:
        connection.execute(
            update(EventCatalogEntry.__table__).values(
                status_text="Scheduled", status_code=1
            )
        )
    provider.frame = provider.frame.iloc[0:0]
    telemetry = _RecordingTelemetry()

    with pytest.raises(ValueError, match="empty.*cannot replace"):
        _service(repository, provider, telemetry_recorder=telemetry).refresh(
            SEASON, now=RETRIEVED_AT + timedelta(hours=1)
        )

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events[0].source_row_count == 0
    assert telemetry.events[0].rejected_publication_count == 1


def test_catalog_shrink_preserves_publication_until_owner_recovers(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = _service(repository, provider)
    service.refresh(SEASON)
    prior_player_rows = repository.list_player_rows(SEASON, 101)
    _remove_player_from_fresh_catalog(repository, 101)
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="completed game coverage"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(days=1))

    assert repository.list_player_rows(SEASON, 101) == prior_player_rows
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events[0].source_row_count == 22
    assert telemetry.events[0].unjoined_athlete_count == 3
    assert telemetry.events[0].rejected_publication_count == 1


def test_new_unjoined_athlete_cannot_hide_cumulative_source_growth(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    unknown = provider.frame.iloc[[0]].copy()
    unknown["PLAYER_ID"] = 999
    provider.frame = pd.concat([provider.frame, unknown], ignore_index=True)
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="incomplete canonical"):
        _service(repository, provider, telemetry_recorder=telemetry).refresh(
            SEASON, now=RETRIEVED_AT + timedelta(hours=1)
        )

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).source_row_count == 22
    assert telemetry.events[0].source_row_count == 23
    assert telemetry.events[0].published_row_count == 0
    assert telemetry.events[0].unjoined_athlete_count == 1
    assert telemetry.events[0].rejected_publication_count == 1


@pytest.mark.parametrize("catalog_state", ["missing", "stale"])
def test_unusable_athlete_catalog_preserves_last_valid_publication(
    tmp_path, catalog_state
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = _service(repository, provider)
    service.refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    if catalog_state == "missing":
        with repository.engine.begin() as connection:
            connection.execute(
                delete(AthleteCatalogFreshness.__table__).where(
                    AthleteCatalogFreshness.season == SEASON
                )
            )
        observed_at = RETRIEVED_AT + timedelta(days=1)
    else:
        observed_at = RETRIEVED_AT + timedelta(days=8)
        with repository.engine.begin() as connection:
            connection.execute(
                update(EventCatalogRefresh.__table__)
                .where(EventCatalogRefresh.season == SEASON)
                .values(
                    last_attempt_at=observed_at,
                    last_success_at=observed_at,
                )
            )
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="Athlete Catalog.*fresh"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=observed_at)

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=22,
            published_row_count=0,
            unjoined_athlete_count=0,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
        )
    ]


@pytest.mark.parametrize("catalog_state", ["missing", "stale", "partial"])
def test_unusable_event_catalog_preserves_last_valid_publication(
    tmp_path, catalog_state
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = _service(repository, provider)
    service.refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    if catalog_state == "missing":
        with repository.engine.begin() as connection:
            connection.execute(
                delete(EventCatalogRefresh.__table__).where(
                    EventCatalogRefresh.season == SEASON
                )
            )
        observed_at = RETRIEVED_AT + timedelta(days=1)
    elif catalog_state == "stale":
        observed_at = RETRIEVED_AT + timedelta(hours=73)
    else:
        with repository.engine.begin() as connection:
            connection.execute(
                delete(EventCatalogEntry.__table__).where(
                    EventCatalogEntry.nba_game_id == "0022500003"
                )
            )
        observed_at = RETRIEVED_AT + timedelta(hours=1)
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="Event Catalog.*fresh"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=observed_at)

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=22,
            published_row_count=0,
            unjoined_athlete_count=0,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
        )
    ]


def test_incomplete_event_catalog_cannot_hide_cumulative_log_growth(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    provider.frame.loc[2, "GAME_ID"] = "0022500005"
    provider.frame.loc[2, "GAME_DATE"] = "2026-01-11"
    service = _service(repository, provider)
    service.refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    assert repository.get_freshness(SEASON).source_row_count == 22
    new_game = provider.frame.iloc[[3]].copy()
    new_game["GAME_ID"] = "0022500006"
    new_game["GAME_DATE"] = "2026-01-21"
    provider.frame = pd.concat([provider.frame, new_game], ignore_index=True)
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match="incomplete canonical"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(hours=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events[0].source_row_count == 23
    assert telemetry.events[0].published_row_count == 0
    assert telemetry.events[0].unjoined_event_count == 2
    assert telemetry.events[0].rejected_publication_count == 1

    recovery_at = RETRIEVED_AT + timedelta(hours=2)
    with repository.engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [
                {
                    "nba_game_id": game_id,
                    "season": SEASON,
                    "home_team_id": home_team_id,
                    "home_team_name": home_team_tricode,
                    "home_team_tricode": home_team_tricode,
                    "away_team_id": away_team_id,
                    "away_team_name": away_team_tricode,
                    "away_team_tricode": away_team_tricode,
                    "scheduled_at": scheduled_at,
                    "status_text": "Scheduled",
                    "status_code": 1,
                    "postponed_status": None,
                    "postponement_evidence": None,
                    "classification": "Regular Season",
                    "first_seen_at": recovery_at,
                    "last_seen_at": recovery_at,
                }
                for (
                    game_id,
                    home_team_id,
                    home_team_tricode,
                    away_team_id,
                    away_team_tricode,
                    scheduled_at,
                ) in (
                    (
                        "0022500005",
                        4,
                        "DDD",
                        3,
                        "CCC",
                        datetime(2026, 1, 11, tzinfo=timezone.utc),
                    ),
                    (
                        "0022500006",
                        2,
                        "BBB",
                        3,
                        "CCC",
                        datetime(2026, 1, 21, tzinfo=timezone.utc),
                    ),
                )
            ],
        )
        connection.execute(
            update(EventCatalogRefresh.__table__)
            .where(EventCatalogRefresh.season == SEASON)
            .values(last_success_at=recovery_at, event_count=6)
        )

    recovered = _service(repository, provider).refresh(SEASON, now=recovery_at)

    assert recovered.row_count == 23
    assert repository.get_freshness(SEASON).source_row_count == 23


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("GAME_ID", "0022599999", id="event"),
        pytest.param("TEAM_ID", 999, id="team"),
    ],
)
def test_stable_partially_unjoined_snapshot_can_republish(
    tmp_path, column, value
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    provider.frame.loc[2, column] = value

    first = _service(repository, provider).refresh(SEASON)
    second_at = RETRIEVED_AT + timedelta(hours=1)
    second = _service(repository, provider).refresh(SEASON, now=second_at)

    assert first.row_count == second.row_count == 21
    assert repository.get_freshness(SEASON) == PlayerGameLogFreshness(
        season=SEASON,
        source_provider="nba_stats",
        retrieved_at=second_at,
        row_count=21,
        source_row_count=22,
    )


@pytest.mark.parametrize(
    ("column", "value", "telemetry_field"),
    [
        pytest.param("PLAYER_ID", 999, "unjoined_athlete_count", id="athlete"),
        pytest.param("GAME_ID", "0022599999", "unjoined_event_count", id="game"),
        pytest.param("TEAM_ID", 999, "team_mismatch_count", id="team-not-in-game"),
    ],
)
def test_individual_unjoined_rows_are_excluded_and_counted(
    tmp_path, column, value, telemetry_field
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    provider.frame.loc[2, column] = value
    telemetry = _RecordingTelemetry()

    result = _service(
        repository, provider, telemetry_recorder=telemetry
    ).refresh(SEASON)

    assert result.row_count == 21
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event.source_row_count == 22
    assert event.published_row_count == 21
    assert getattr(event, telemetry_field) == 1
    assert (
        event.unjoined_athlete_count
        + event.unjoined_event_count
        + event.team_mismatch_count
    ) == 1


def test_wholly_unjoined_snapshot_preserves_last_valid_publication(tmp_path):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    service = _service(repository, provider)
    service.refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)

    provider.frame["PLAYER_ID"] = 999
    telemetry = _RecordingTelemetry()
    with pytest.raises(PlayerGameLogIdentityError, match="no canonical rows"):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(days=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=22,
            published_row_count=0,
            unjoined_athlete_count=22,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
        )
    ]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("MIN", float("nan"), id="nonfinite"),
        pytest.param("FGM", 99, id="logically-inconsistent"),
        pytest.param("FGM", 1, id="three-pointers-exceed-field-goals"),
    ],
)
def test_malformed_row_aborts_publication_and_records_safe_telemetry(
    tmp_path, column, value
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    provider.frame.loc[0, "PLAYER_ID"] = 999
    provider.frame.loc[1, column] = value
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(days=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert len(telemetry.events) == 1
    assert telemetry.events[0] == PlayerGameLogTelemetryEvent(
        source_row_count=22,
        published_row_count=0,
        unjoined_athlete_count=1,
        unjoined_event_count=0,
        team_mismatch_count=0,
        malformed_row_count=1,
        rejected_publication_count=1,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        pytest.param("missing-column", "missing required", id="missing-column"),
        pytest.param("invalid-date", "invalid game date", id="invalid-date"),
        pytest.param("empty-game-id", "no canonical game", id="empty-game-id"),
        pytest.param("nonnumeric-stat", "invalid points", id="nonnumeric-stat"),
    ],
)
def test_structurally_malformed_snapshot_preserves_publication_with_telemetry(
    tmp_path, mutation, message
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    if mutation == "missing-column":
        provider.frame = provider.frame.drop(columns=["PTS"])
    elif mutation == "invalid-date":
        provider.frame.loc[2, "GAME_DATE"] = "not-a-date"
    elif mutation == "empty-game-id":
        provider.frame.loc[2, "GAME_ID"] = ""
    else:
        provider.frame["PTS"] = provider.frame["PTS"].astype(object)
        provider.frame.loc[2, "PTS"] = "not-a-number"
    telemetry = _RecordingTelemetry()

    with pytest.raises(PlayerGameLogIdentityError, match=message):
        _service(repository, provider, telemetry_recorder=telemetry).refresh(
            SEASON, now=RETRIEVED_AT + timedelta(hours=1)
        )

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert len(telemetry.events) == 1
    assert telemetry.events[0].source_row_count == 22
    assert telemetry.events[0].malformed_row_count == 1
    assert telemetry.events[0].rejected_publication_count == 1


@pytest.mark.parametrize(
    ("rejection", "source_row_count"),
    [
        pytest.param("shrink", 22, id="cumulative-shrink"),
        pytest.param("conflicting_duplicate", 23, id="conflicting-duplicate"),
    ],
)
def test_rejected_publication_records_safe_telemetry(
    tmp_path, rejection, source_row_count
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    if rejection == "shrink":
        extra_player = provider.frame.iloc[[6]].copy()
        extra_player["PLAYER_ID"] = 111
        provider.frame = pd.concat(
            [provider.frame, extra_player], ignore_index=True
        )
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    if rejection == "shrink":
        provider.frame = _recorded_season_frame()
    else:
        duplicate = provider.frame.iloc[[0]].copy()
        duplicate["PTS"] += 1
        provider.frame = pd.concat([provider.frame, duplicate], ignore_index=True)
    telemetry = _RecordingTelemetry()

    with pytest.raises(ValueError):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(days=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert len(telemetry.events) == 1
    assert telemetry.events[0].source_row_count == source_row_count
    assert telemetry.events[0].published_row_count == 0
    assert telemetry.events[0].malformed_row_count == 0
    assert telemetry.events[0].rejected_publication_count == 1


def test_database_publication_failure_preserves_snapshot_and_records_rejection(
    tmp_path,
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    duplicate = provider.frame.iloc[[0]].copy()
    provider.frame = pd.concat([provider.frame, duplicate], ignore_index=True)
    with repository.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TRIGGER reject_player_game_log_insert
                BEFORE INSERT ON player_game_logs
                BEGIN
                    SELECT RAISE(FAIL, 'forced publication failure');
                END
                """
            )
        )
    telemetry = _RecordingTelemetry()

    with pytest.raises(SQLAlchemyError):
        _service(
            repository, provider, telemetry_recorder=telemetry
        ).refresh(SEASON, now=RETRIEVED_AT + timedelta(hours=1))

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=23,
            published_row_count=0,
            unjoined_athlete_count=0,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
            duplicate_row_count=1,
        )
    ]


@pytest.mark.parametrize(
    "statement_fragment",
    [
        "FROM event_catalog_refreshes",
        "FROM athlete_catalog_freshness",
    ],
)
def test_prerequisite_database_read_failure_records_rejection_once(
    tmp_path, statement_fragment
):
    repository = _repository(tmp_path)
    _seed_identities(repository)
    provider = _RecordedSeasonProvider()
    _service(repository, provider).refresh(SEASON)
    before = repository.list_player_rows(SEASON, 101)
    telemetry = _RecordingTelemetry()

    def fail_identity_read(
        connection, cursor, statement, parameters, context, executemany
    ):
        del connection, cursor, context, executemany
        if statement_fragment in statement:
            raise OperationalError(statement, parameters, RuntimeError("forced read"))

    event.listen(repository.engine, "before_cursor_execute", fail_identity_read)
    try:
        with pytest.raises(SQLAlchemyError):
            _service(
                repository, provider, telemetry_recorder=telemetry
            ).refresh(SEASON, now=RETRIEVED_AT + timedelta(hours=1))
    finally:
        event.remove(repository.engine, "before_cursor_execute", fail_identity_read)

    assert repository.list_player_rows(SEASON, 101) == before
    assert repository.get_freshness(SEASON).retrieved_at == RETRIEVED_AT
    assert telemetry.events == [
        PlayerGameLogTelemetryEvent(
            source_row_count=22,
            published_row_count=0,
            unjoined_athlete_count=0,
            unjoined_event_count=0,
            team_mismatch_count=0,
            rejected_publication_count=1,
        )
    ]
