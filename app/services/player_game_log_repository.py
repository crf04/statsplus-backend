"""Transactional persistence and deterministic reads for player game logs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.player_game_log import PlayerGameLog, PlayerGameLogRefresh
from app.services.nba_stats_adapter import validate_canonical_season


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

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def publish(
        self,
        season: str,
        records: Iterable[PlayerGameLogRecord],
        *,
        retrieved_at: datetime,
        source_provider: str,
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

        retrieved = assume_utc(retrieved_at)
        log_table = PlayerGameLog.__table__
        refresh_table = PlayerGameLogRefresh.__table__
        with self.engine.begin() as connection:
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
        return len(unique)

    def list_player_rows(
        self, season: str, player_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_season = validate_canonical_season(season)
        if not self._has_complete_publication(canonical_season):
            return ()
        table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.season == canonical_season,
                    table.c.player_id == player_id,
                )
                .order_by(table.c.game_date.desc(), table.c.game_id.desc())
            ).mappings()
            return tuple(PlayerGameLogRecord(**dict(row)) for row in rows)

    def get_freshness(self, season: str) -> PlayerGameLogFreshness:
        canonical_season = validate_canonical_season(season)
        table = PlayerGameLogRefresh.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table).where(table.c.season == canonical_season)
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
        totals = {
            market: sum(self._market_values(row)[market] for row in rows)
            for market in self._market_values(rows[0])
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
        if not self._has_complete_publication(canonical_season):
            return ()
        table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.season == canonical_season,
                    table.c.player_id.in_(player_ids),
                    table.c.opponent_team_id == opponent_team_id,
                )
                .order_by(
                    table.c.game_date.desc(),
                    table.c.player_id.asc(),
                    table.c.game_id.desc(),
                )
            ).mappings()
            return tuple(PlayerGameLogRecord(**dict(row)) for row in rows)

    def _has_complete_publication(self, season: str) -> bool:
        table = PlayerGameLogRefresh.__table__
        with self.engine.connect() as connection:
            return connection.execute(
                select(table.c.season).where(table.c.season == season)
            ).scalar_one_or_none() is not None

    @staticmethod
    def _market_values(record: PlayerGameLogRecord) -> dict[str, float]:
        return {
            "PTS": record.points,
            "REB": record.rebounds,
            "AST": record.assists,
            "3PM": record.three_pointers_made,
            "TOV": record.turnovers,
            "STL": record.steals,
            "BLK": record.blocks,
            "PRA": record.points + record.rebounds + record.assists,
            "PA": record.points + record.assists,
            "PR": record.points + record.rebounds,
            "RA": record.rebounds + record.assists,
            "STKS": record.steals + record.blocks,
            "FGA": record.field_goals_attempted,
            "FG3A": record.three_pointers_attempted,
            "FG2A": (
                record.field_goals_attempted - record.three_pointers_attempted
            ),
        }


__all__ = [
    "PlayerGameLogFreshness",
    "PlayerGameLogRecord",
    "PlayerGameLogRepository",
    "PlayerSeasonRate",
]
