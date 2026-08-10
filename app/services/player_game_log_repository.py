"""Transactional persistence and deterministic reads for player game logs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.player_game_log import PlayerGameLog, PlayerGameLogRefresh
from app.models.stats_freshness import StatsRefresh
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import (
    PLAYER_GAME_LOG_SURFACE,
    StatsFreshnessRepository,
)


@dataclass(frozen=True, slots=True)
class PlayerGameLogRecord:
    season: str
    player_id: int
    game_id: str
    player_name: str
    game_date: date
    team_id: int
    team_tricode: str
    opponent_team_id: int
    opponent_team_tricode: str
    is_home: bool
    minutes: float
    points: int
    rebounds: int
    assists: int
    field_goals_made: int
    field_goals_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    turnovers: int
    steals: int
    blocks: int


@dataclass(frozen=True, slots=True)
class PlayerGameLogFreshness:
    season: str
    source_provider: str | None
    retrieved_at: datetime | None
    row_count: int


@dataclass(frozen=True, slots=True)
class PlayerSeasonRate:
    season: str
    player_id: int
    game_count: int
    total_minutes: float
    per_game: dict[str, float]
    per_minute: dict[str, float]


class PlayerGameLogRepository:
    """Replace one season atomically and derive read models from its rows."""

    def __init__(self, engine: Engine, *, statistic_catalog: StatisticCatalog) -> None:
        self.engine = engine
        self._surface_freshness = StatsFreshnessRepository(
            engine, surface=PLAYER_GAME_LOG_SURFACE
        )
        self._market_statistics = tuple(
            statistic
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        )
        required_components = {
            component
            for statistic in self._market_statistics
            for component in statistic.components
        }
        stored_components = set(PlayerGameLogRecord.__dataclass_fields__)
        unsupported = {
            component
            for component in required_components
            if component not in stored_components
            and component != "two_pointers_attempted"
        }
        if unsupported:
            raise ValueError(
                "player game logs cannot derive governed statistic components: "
                f"{sorted(unsupported)}"
            )

    def publish(
        self,
        season: str,
        records: Iterable[PlayerGameLogRecord],
        *,
        retrieved_at: datetime,
        source_provider: str,
        allow_empty: bool = False,
    ) -> int:
        canonical_season = validate_canonical_season(season)
        if not source_provider or source_provider != source_provider.strip():
            raise ValueError("source_provider must be a non-empty canonical value")

        unique: dict[tuple[int, str], PlayerGameLogRecord] = {}
        for record in records:
            if record.season != canonical_season:
                raise ValueError("every player game log must belong to the publication season")
            key = (record.player_id, record.game_id)
            existing = unique.get(key)
            if existing is not None and existing != record:
                raise ValueError("conflicting player game log facts share one identity")
            unique[key] = record

        if not unique and not allow_empty:
            raise ValueError("empty player game log snapshot cannot be published")

        retrieved = assume_utc(retrieved_at)
        log_table = PlayerGameLog.__table__
        refresh_table = PlayerGameLogRefresh.__table__
        with self.engine.begin() as connection:
            existing_row_count = connection.execute(
                select(refresh_table.c.row_count).where(
                    refresh_table.c.season == canonical_season
                )
            ).scalar_one_or_none()
            if not unique and existing_row_count is not None and existing_row_count > 0:
                raise ValueError(
                    "empty player game log snapshot cannot replace a valid publication"
                )
            if (
                unique
                and existing_row_count is not None
                and len(unique) < existing_row_count
            ):
                raise ValueError(
                    "a smaller player game log snapshot cannot replace cumulative facts"
                )
            connection.execute(
                delete(log_table).where(log_table.c.season == canonical_season)
            )
            if unique:
                connection.execute(
                    insert(log_table), [asdict(record) for record in unique.values()]
                )
            values = {
                "source_provider": source_provider,
                "retrieved_at": retrieved,
                "row_count": len(unique),
            }
            result = connection.execute(
                update(refresh_table)
                .where(refresh_table.c.season == canonical_season)
                .values(**values)
            )
            if result.rowcount == 0:
                connection.execute(
                    insert(refresh_table).values(season=canonical_season, **values)
                )
            self._surface_freshness.record_success(
                retrieved, connection=connection
            )
        return len(unique)

    def list_player_rows(
        self, season: str, player_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_season = validate_canonical_season(season)
        log_table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                self._published_rows_statement()
                .where(
                    log_table.c.season == canonical_season,
                    log_table.c.player_id == player_id,
                )
                .order_by(log_table.c.game_date.desc(), log_table.c.game_id.desc())
            ).mappings()
            return tuple(PlayerGameLogRecord(**dict(row)) for row in rows)

    def get_freshness(self, season: str) -> PlayerGameLogFreshness:
        canonical_season = validate_canonical_season(season)
        refresh_table = PlayerGameLogRefresh.__table__
        surface_table = StatsRefresh.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(refresh_table)
                .join(
                    surface_table,
                    and_(
                        surface_table.c.surface == PLAYER_GAME_LOG_SURFACE,
                        surface_table.c.last_success_at
                        == refresh_table.c.retrieved_at,
                    ),
                )
                .where(refresh_table.c.season == canonical_season)
            ).mappings().one_or_none()
        if row is None:
            return PlayerGameLogFreshness(canonical_season, None, None, 0)
        return PlayerGameLogFreshness(
            season=canonical_season,
            source_provider=row["source_provider"],
            retrieved_at=assume_utc(row["retrieved_at"]),
            row_count=int(row["row_count"]),
        )

    def get_season_rate(
        self, season: str, player_id: int
    ) -> PlayerSeasonRate | None:
        rows = self.list_player_rows(season, player_id)
        total_minutes = sum(row.minutes for row in rows)
        if not rows or total_minutes <= 0:
            return None
        row_values = tuple(self._market_values(row) for row in rows)
        totals = {
            statistic.market_category: sum(
                values[statistic.market_category] for values in row_values
            )
            for statistic in self._market_statistics
            if statistic.market_category is not None
        }
        return PlayerSeasonRate(
            season=validate_canonical_season(season),
            player_id=player_id,
            game_count=len(rows),
            total_minutes=total_minutes,
            per_game={market: value / len(rows) for market, value in totals.items()},
            per_minute={
                market: value / total_minutes for market, value in totals.items()
            },
        )

    def get_last_ten_minutes(
        self, season: str, player_id: int
    ) -> tuple[float, ...]:
        recent_first = self.list_player_rows(season, player_id)[:10]
        return tuple(row.minutes for row in reversed(recent_first))

    def list_h2h_rows(
        self, season: str, player_id: int, opponent_team_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        return self._list_rows(
            season,
            player_ids=(player_id,),
            opponent_team_id=opponent_team_id,
        )

    def list_archetype_rows(
        self, season: str, player_ids: Iterable[int], opponent_team_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_ids = tuple(sorted(set(player_ids)))
        if not canonical_ids:
            return ()
        return self._list_rows(
            season,
            player_ids=canonical_ids,
            opponent_team_id=opponent_team_id,
        )

    def _list_rows(
        self,
        season: str,
        *,
        player_ids: tuple[int, ...],
        opponent_team_id: int,
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_season = validate_canonical_season(season)
        log_table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                self._published_rows_statement()
                .where(
                    log_table.c.season == canonical_season,
                    log_table.c.player_id.in_(player_ids),
                    log_table.c.opponent_team_id == opponent_team_id,
                )
                .order_by(
                    log_table.c.game_date.desc(),
                    log_table.c.player_id.asc(),
                    log_table.c.game_id.desc(),
                )
            ).mappings()
            return tuple(PlayerGameLogRecord(**dict(row)) for row in rows)

    @staticmethod
    def _published_rows_statement():
        log_table = PlayerGameLog.__table__
        refresh_table = PlayerGameLogRefresh.__table__
        surface_table = StatsRefresh.__table__
        return (
            select(log_table)
            .join(
                refresh_table,
                refresh_table.c.season == log_table.c.season,
            )
            .join(
                surface_table,
                and_(
                    surface_table.c.surface == PLAYER_GAME_LOG_SURFACE,
                    surface_table.c.last_success_at == refresh_table.c.retrieved_at,
                ),
            )
            .where(refresh_table.c.row_count > 0)
        )

    def _market_values(self, record: PlayerGameLogRecord) -> dict[str, float]:
        return {
            statistic.market_category: sum(
                self._component_value(record, component)
                for component in statistic.components
            )
            for statistic in self._market_statistics
            if statistic.market_category is not None
        }

    @staticmethod
    def _component_value(record: PlayerGameLogRecord, component: str) -> float:
        if component == "two_pointers_attempted":
            return record.field_goals_attempted - record.three_pointers_attempted
        return float(getattr(record, component))


__all__ = [
    "PlayerGameLogFreshness",
    "PlayerGameLogRecord",
    "PlayerGameLogRepository",
    "PlayerSeasonRate",
]
