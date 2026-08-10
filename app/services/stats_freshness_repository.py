"""Persistence boundary for stats-table completion freshness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.domain.utc import assume_utc
from app.models.stats_freshness import StatsRefresh


STATS_SURFACE = "stats_tables"
PLAYER_GAME_LOG_SURFACE = "player_game_logs"
STATS_SURFACES = frozenset({STATS_SURFACE, PLAYER_GAME_LOG_SURFACE})


@dataclass(frozen=True, slots=True)
class StatsFreshness:
    """Stored fact about the latest complete publication of one stats surface."""

    last_successful_completion: datetime | None


class StatsFreshnessWriter(Protocol):
    def record_success(
        self, completed_at: datetime, *, connection: Connection
    ) -> None: ...


class StatsFreshnessRepository:
    def __init__(self, engine: Engine, *, surface: str = STATS_SURFACE) -> None:
        if surface not in STATS_SURFACES:
            raise ValueError("stats freshness surface must use the governed vocabulary")
        self.engine = engine
        self.surface = surface

    def record_success(
        self, completed_at: datetime, *, connection: Connection | None = None
    ) -> None:
        """Upsert success, optionally inside the stats publication transaction."""

        if connection is None:
            with self.engine.begin() as owned_connection:
                self._record(owned_connection, completed_at)
            return
        self._record(connection, completed_at)

    def _record(self, connection: Connection, completed_at: datetime) -> None:
        table = StatsRefresh.__table__
        value = assume_utc(completed_at)
        result = connection.execute(
            update(table)
            .where(table.c.surface == self.surface)
            .values(last_success_at=value)
        )
        if result.rowcount == 0:
            connection.execute(
                insert(table).values(surface=self.surface, last_success_at=value)
            )

    def get(self) -> StatsFreshness:
        table = StatsRefresh.__table__
        with self.engine.connect() as connection:
            value = connection.execute(
                select(table.c.last_success_at).where(
                    table.c.surface == self.surface
                )
            ).scalar_one_or_none()
        return StatsFreshness(
            last_successful_completion=(
                assume_utc(value) if value is not None else None
            ),
        )
