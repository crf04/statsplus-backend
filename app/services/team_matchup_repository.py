"""Atomic persistence for window-aware raw team matchup facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.team_matchup import (
    TeamMatchupFactRow,
    TeamMatchupSurfaceObservationRow,
)
from app.utils.db import is_demo_database_url


@dataclass(frozen=True, slots=True)
class TeamMatchupSnapshotScope:
    season: str
    as_of: date
    window_games: int | None = None

    def __post_init__(self) -> None:
        if self.window_games is not None and (
            not isinstance(self.window_games, int)
            or isinstance(self.window_games, bool)
            or self.window_games < 1
        ):
            raise ValueError("window_games must be a positive integer or None")

    @property
    def window_kind(self) -> str:
        return "season" if self.window_games is None else "rolling_games"

    @property
    def stored_window_games(self) -> int:
        return self.window_games or 0


@dataclass(frozen=True, slots=True)
class TeamMatchupFact:
    team_id: int
    base: str
    slice_key: str
    stat_key: str
    raw_value: float | None
    denominator_value: float | None
    denominator_unit: str | None
    provider: str
    status: str = "available"
    unavailable_reason: str | None = None
    window_start_date: date | None = None


@dataclass(frozen=True, slots=True)
class TeamMatchupObservation:
    surface: str
    status: str
    unavailable_reason: str | None = None
    retrieved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredTeamMatchupFact(TeamMatchupFact):
    retrieved_at: datetime = datetime.min


@dataclass(frozen=True, slots=True)
class StoredTeamMatchupObservation(TeamMatchupObservation):
    retrieved_at: datetime = datetime.min


@dataclass(frozen=True, slots=True)
class StoredTeamMatchupSnapshot:
    scope: TeamMatchupSnapshotScope
    facts: tuple[StoredTeamMatchupFact, ...]
    observations: tuple[StoredTeamMatchupObservation, ...]


class TeamMatchupRepository:
    """Replace or read one season/as-of/window snapshot transactionally."""

    def __init__(self, engine: Engine) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store team matchup facts")
        self.engine = engine

    @staticmethod
    def _scope(table, scope: TeamMatchupSnapshotScope):
        return (
            table.c.season == scope.season,
            table.c.as_of_date == scope.as_of,
            table.c.window_kind == scope.window_kind,
            table.c.window_games == scope.stored_window_games,
        )

    def replace_snapshot(
        self,
        scope: TeamMatchupSnapshotScope,
        *,
        facts: Iterable[TeamMatchupFact],
        observations: Iterable[TeamMatchupObservation],
        retrieved_at: datetime,
    ) -> None:
        self.replace_snapshots(
            ((scope, tuple(facts), tuple(observations)),),
            retrieved_at=retrieved_at,
        )

    def replace_snapshots(
        self,
        snapshots: Iterable[
            tuple[
                TeamMatchupSnapshotScope,
                Iterable[TeamMatchupFact],
                Iterable[TeamMatchupObservation],
            ]
        ],
        *,
        retrieved_at: datetime,
    ) -> None:
        """Replace related windows in one transaction after collection."""

        prepared = tuple(
            (scope, tuple(facts), tuple(observations))
            for scope, facts, observations in snapshots
        )
        if not prepared:
            raise ValueError("at least one team matchup snapshot is required")
        if any(not observations for _, _, observations in prepared):
            raise ValueError("a team matchup snapshot needs surface observations")
        observed_at = assume_utc(retrieved_at)
        fact_table = TeamMatchupFactRow.__table__
        observation_table = TeamMatchupSurfaceObservationRow.__table__
        with self.engine.begin() as connection:
            for scope, fact_rows, observation_rows in prepared:
                identity = {
                    "season": scope.season,
                    "as_of_date": scope.as_of,
                    "window_kind": scope.window_kind,
                    "window_games": scope.stored_window_games,
                }
                connection.execute(
                    delete(fact_table).where(*self._scope(fact_table, scope))
                )
                connection.execute(
                    delete(observation_table).where(
                        *self._scope(observation_table, scope)
                    )
                )
                if fact_rows:
                    connection.execute(
                        insert(fact_table),
                        [
                            {
                                **identity,
                                "team_id": fact.team_id,
                                "base": fact.base,
                                "slice_key": fact.slice_key,
                                "stat_key": fact.stat_key,
                                "raw_value": fact.raw_value,
                                "denominator_value": fact.denominator_value,
                                "denominator_unit": fact.denominator_unit,
                                "provider": fact.provider,
                                "status": fact.status,
                                "unavailable_reason": fact.unavailable_reason,
                                "window_start_date": fact.window_start_date,
                                "window_end_date": scope.as_of,
                                "retrieved_at": observed_at,
                            }
                            for fact in fact_rows
                        ],
                    )
                connection.execute(
                    insert(observation_table),
                    [
                        {
                            **identity,
                            "surface": observation.surface,
                            "status": observation.status,
                            "unavailable_reason": observation.unavailable_reason,
                            "retrieved_at": assume_utc(
                                observation.retrieved_at or observed_at
                            ),
                        }
                        for observation in observation_rows
                    ],
                )

    def get_snapshot(
        self, scope: TeamMatchupSnapshotScope
    ) -> StoredTeamMatchupSnapshot:
        fact_table = TeamMatchupFactRow.__table__
        observation_table = TeamMatchupSurfaceObservationRow.__table__
        with self.engine.connect() as connection:
            fact_rows = (
                connection.execute(
                    select(fact_table)
                    .where(*self._scope(fact_table, scope))
                    .order_by(
                        fact_table.c.team_id,
                        fact_table.c.base,
                        fact_table.c.slice_key,
                        fact_table.c.stat_key,
                    )
                )
                .mappings()
                .all()
            )
            observation_rows = (
                connection.execute(
                    select(observation_table)
                    .where(*self._scope(observation_table, scope))
                    .order_by(observation_table.c.surface)
                )
                .mappings()
                .all()
            )
        return StoredTeamMatchupSnapshot(
            scope=scope,
            facts=tuple(
                StoredTeamMatchupFact(
                    team_id=row["team_id"],
                    base=row["base"],
                    slice_key=row["slice_key"],
                    stat_key=row["stat_key"],
                    raw_value=row["raw_value"],
                    denominator_value=row["denominator_value"],
                    denominator_unit=row["denominator_unit"],
                    provider=row["provider"],
                    status=row["status"],
                    unavailable_reason=row["unavailable_reason"],
                    window_start_date=row["window_start_date"],
                    retrieved_at=assume_utc(row["retrieved_at"]),
                )
                for row in fact_rows
            ),
            observations=tuple(
                StoredTeamMatchupObservation(
                    surface=row["surface"],
                    status=row["status"],
                    unavailable_reason=row["unavailable_reason"],
                    retrieved_at=assume_utc(row["retrieved_at"]),
                )
                for row in observation_rows
            ),
        )

    def get_latest_scope(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date | None = None,
    ) -> TeamMatchupSnapshotScope | None:
        requested = TeamMatchupSnapshotScope(season, as_of or date.max, window_games)
        table = TeamMatchupSurfaceObservationRow.__table__
        conditions = (
            table.c.season == season,
            table.c.window_kind == requested.window_kind,
            table.c.window_games == requested.stored_window_games,
        )
        statement = select(func.max(table.c.as_of_date)).where(*conditions)
        if as_of is not None:
            statement = statement.where(table.c.as_of_date <= as_of)
        with self.engine.connect() as connection:
            latest_as_of = connection.execute(statement).scalar_one()
        if latest_as_of is None:
            return None
        return TeamMatchupSnapshotScope(season, latest_as_of, window_games)


__all__ = [
    "StoredTeamMatchupFact",
    "StoredTeamMatchupObservation",
    "StoredTeamMatchupSnapshot",
    "TeamMatchupFact",
    "TeamMatchupObservation",
    "TeamMatchupRepository",
    "TeamMatchupSnapshotScope",
]
