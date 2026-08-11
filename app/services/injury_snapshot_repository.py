"""Atomic persistence for matchup injury evidence snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.injury_snapshot import InjurySnapshot
from app.providers.nba_stats import validate_canonical_season
from app.utils.db import is_demo_database_url


@dataclass(frozen=True, slots=True)
class InjurySnapshotScope:
    season: str
    game_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "season", validate_canonical_season(self.season))
        game_id = str(self.game_id)
        if not game_id or game_id != game_id.strip():
            raise ValueError("injury snapshot game_id must be non-empty")
        object.__setattr__(self, "game_id", game_id)


@dataclass(frozen=True, slots=True)
class StoredInjurySnapshot:
    raw_payload: list[Mapping[str, Any]]
    normalized_entries: tuple[Mapping[str, Any], ...]
    retrieved_at: datetime


class InjurySnapshotRepository:
    """Store the evidence and reconciled document in one database row."""

    def __init__(self, engine: Engine) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store injury snapshots")
        self.engine = engine

    @staticmethod
    def _identity(scope: InjurySnapshotScope):
        table = InjurySnapshot.__table__
        return and_(
            table.c.season == scope.season,
            table.c.game_id == scope.game_id,
        )

    def get(self, scope: InjurySnapshotScope) -> StoredInjurySnapshot | None:
        table = InjurySnapshot.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    table.c.raw_payload,
                    table.c.normalized_entries,
                    table.c.retrieved_at,
                ).where(self._identity(scope))
            ).mappings().one_or_none()
        if row is None:
            return None
        try:
            raw = json.loads(row["raw_payload"])
            normalized = json.loads(row["normalized_entries"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("stored injury snapshot JSON is invalid") from error
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("stored raw injury snapshot must be a list of objects")
        if not isinstance(normalized, list) or not all(
            isinstance(item, dict) for item in normalized
        ):
            raise ValueError("stored normalized injuries must be a list of objects")
        return StoredInjurySnapshot(
            raw_payload=raw,
            normalized_entries=tuple(normalized),
            retrieved_at=assume_utc(row["retrieved_at"]),
        )

    def replace(
        self,
        scope: InjurySnapshotScope,
        *,
        raw_payload: Sequence[Mapping[str, Any]],
        normalized_entries: Sequence[Mapping[str, Any]],
        retrieved_at: datetime,
    ) -> None:
        table = InjurySnapshot.__table__
        observed_at = assume_utc(retrieved_at)
        values = {
            "raw_payload": json.dumps(
                list(raw_payload), sort_keys=True, separators=(",", ":"), allow_nan=False
            ),
            "normalized_entries": json.dumps(
                list(normalized_entries),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "retrieved_at": observed_at,
            "updated_at": observed_at,
        }
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table).where(self._identity(scope)).values(**values)
            )
            if result.rowcount == 0:
                connection.execute(
                    insert(table).values(
                        season=scope.season,
                        game_id=scope.game_id,
                        **values,
                    )
                )


__all__ = [
    "InjurySnapshotRepository",
    "InjurySnapshotScope",
    "StoredInjurySnapshot",
]
