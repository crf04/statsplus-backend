"""Atomic persistence for matchup injury evidence snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from sqlalchemy import and_, delete, exists, insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.domain.utc import assume_utc
from app.models.injury_snapshot import InjurySnapshot, InjurySourceSnapshot
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
    normalized_entries: tuple[Mapping[str, Any], ...]
    retrieved_at: datetime
    unresolved_team_entry_count: int = 0


@dataclass(frozen=True, slots=True)
class StoredInjuryEvidence:
    """Raw provider evidence loaded only through the explicit audit seam."""

    source_snapshot_id: int | None
    source: str | None
    raw_payload: list[Mapping[str, Any]]
    source_entries: tuple[Mapping[str, Any], ...]
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class StoredInjurySourceSnapshot:
    source_snapshot_id: int
    source: str
    raw_payload: list[Mapping[str, Any]]
    normalized_entries: tuple[Mapping[str, Any], ...]
    retrieved_at: datetime


class InjurySnapshotRepository:
    """Store shared source evidence and per-game reconciled documents."""

    # Keep a small recovery/audit tail per provider after game rows stop
    # referencing a source. Referenced historical evidence is never pruned.
    UNREFERENCED_SOURCE_RETENTION_COUNT = 12

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
                    table.c.normalized_entries,
                    table.c.retrieved_at,
                    table.c.unresolved_team_entry_count,
                ).where(self._identity(scope))
            ).mappings().one_or_none()
            if row is None:
                return None
        normalized = self._decode_objects(
            row["normalized_entries"], "normalized injuries"
        )
        return StoredInjurySnapshot(
            normalized_entries=tuple(normalized),
            retrieved_at=assume_utc(row["retrieved_at"]),
            unresolved_team_entry_count=int(row["unresolved_team_entry_count"]),
        )

    def get_evidence(
        self, scope: InjurySnapshotScope
    ) -> StoredInjuryEvidence | None:
        """Load the durable raw evidence for an explicit audit request."""

        game = InjurySnapshot.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    game.c.source_snapshot_id,
                    game.c.raw_payload,
                    game.c.retrieved_at,
                ).where(self._identity(scope))
            ).mappings().one_or_none()
            if row is None:
                return None
            source_snapshot_id = row["source_snapshot_id"]
            if source_snapshot_id is None:
                return StoredInjuryEvidence(
                    source_snapshot_id=None,
                    source=None,
                    raw_payload=self._decode_objects(
                        row["raw_payload"], "raw injury snapshot"
                    ),
                    source_entries=(),
                    retrieved_at=assume_utc(row["retrieved_at"]),
                )
            source_row = self._get_source_row(connection, int(source_snapshot_id))
            if source_row is None:
                raise ValueError("stored injury source snapshot is missing")
        return StoredInjuryEvidence(
            source_snapshot_id=int(source_row["id"]),
            source=str(source_row["source"]),
            raw_payload=self._decode_objects(
                source_row["raw_payload"], "raw injury snapshot"
            ),
            source_entries=tuple(
                self._decode_objects(
                    source_row["normalized_entries"], "source injuries"
                )
            ),
            retrieved_at=assume_utc(source_row["retrieved_at"]),
        )

    def publish(
        self,
        scope: InjurySnapshotScope,
        *,
        source: str,
        raw_payload: Sequence[Mapping[str, Any]],
        source_entries: Sequence[Mapping[str, Any]],
        normalized_entries: Sequence[Mapping[str, Any]],
        retrieved_at: datetime,
        unresolved_team_entry_count: int,
    ) -> StoredInjurySourceSnapshot:
        observed_at = assume_utc(retrieved_at)
        raw_document = self._encode_objects(raw_payload)
        source_document = self._encode_objects(source_entries)
        with self.engine.begin() as connection:
            result = connection.execute(
                insert(InjurySourceSnapshot.__table__).values(
                    source=source,
                    raw_payload=raw_document,
                    normalized_entries=source_document,
                    retrieved_at=observed_at,
                )
            )
            source_snapshot_id = int(result.inserted_primary_key[0])
            self._replace_game(
                connection,
                scope,
                source_snapshot_id=source_snapshot_id,
                normalized_entries=normalized_entries,
                retrieved_at=observed_at,
                unresolved_team_entry_count=unresolved_team_entry_count,
            )
            self._prune_unreferenced_sources(connection, source)
        return StoredInjurySourceSnapshot(
            source_snapshot_id,
            source,
            list(raw_payload),
            tuple(source_entries),
            observed_at,
        )

    def replace_from_source(
        self,
        scope: InjurySnapshotScope,
        *,
        source_snapshot: StoredInjurySourceSnapshot,
        normalized_entries: Sequence[Mapping[str, Any]],
        unresolved_team_entry_count: int,
    ) -> None:
        with self.engine.begin() as connection:
            self._replace_game(
                connection,
                scope,
                source_snapshot_id=source_snapshot.source_snapshot_id,
                normalized_entries=normalized_entries,
                retrieved_at=source_snapshot.retrieved_at,
                unresolved_team_entry_count=unresolved_team_entry_count,
            )
            self._prune_unreferenced_sources(
                connection, source_snapshot.source
            )

    def get_latest_source(self, source: str) -> StoredInjurySourceSnapshot | None:
        table = InjurySourceSnapshot.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table)
                .where(table.c.source == source)
                .order_by(table.c.retrieved_at.desc(), table.c.id.desc())
                .limit(1)
            ).mappings().one_or_none()
        if row is None:
            return None
        return self._source_from_row(row)

    @staticmethod
    def _get_source_row(connection: Connection, source_snapshot_id: int):
        table = InjurySourceSnapshot.__table__
        return connection.execute(
            select(table).where(table.c.id == source_snapshot_id)
        ).mappings().one_or_none()

    @classmethod
    def _source_from_row(cls, row: Mapping[str, Any]) -> StoredInjurySourceSnapshot:
        return StoredInjurySourceSnapshot(
            int(row["id"]),
            str(row["source"]),
            cls._decode_objects(row["raw_payload"], "raw injury snapshot"),
            tuple(cls._decode_objects(row["normalized_entries"], "source injuries")),
            assume_utc(row["retrieved_at"]),
        )

    @classmethod
    def _prune_unreferenced_sources(
        cls, connection: Connection, source: str
    ) -> None:
        source_table = InjurySourceSnapshot.__table__
        game_table = InjurySnapshot.__table__
        is_referenced = exists(
            select(1).where(
                game_table.c.source_snapshot_id == source_table.c.id
            )
        )
        prunable_ids = (
            select(source_table.c.id)
            .where(source_table.c.source == source, ~is_referenced)
            .order_by(source_table.c.retrieved_at.desc(), source_table.c.id.desc())
            .offset(cls.UNREFERENCED_SOURCE_RETENTION_COUNT)
        )
        connection.execute(
            delete(source_table).where(source_table.c.id.in_(prunable_ids))
        )

    @classmethod
    def _replace_game(
        cls,
        connection: Connection,
        scope: InjurySnapshotScope,
        *,
        source_snapshot_id: int,
        normalized_entries: Sequence[Mapping[str, Any]],
        retrieved_at: datetime,
        unresolved_team_entry_count: int,
    ) -> None:
        table = InjurySnapshot.__table__
        values = {
            "source_snapshot_id": source_snapshot_id,
            "raw_payload": "[]",
            "normalized_entries": cls._encode_objects(normalized_entries),
            "unresolved_team_entry_count": unresolved_team_entry_count,
            "retrieved_at": retrieved_at,
            "updated_at": retrieved_at,
        }
        result = connection.execute(
            update(table)
            .where(
                and_(
                    table.c.season == scope.season,
                    table.c.game_id == scope.game_id,
                )
            )
            .values(**values)
        )
        if result.rowcount == 0:
            connection.execute(
                insert(table).values(
                    season=scope.season,
                    game_id=scope.game_id,
                    **values,
                )
            )

    @staticmethod
    def _encode_objects(values: Sequence[Mapping[str, Any]]) -> str:
        return json.dumps(
            list(values), sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    @staticmethod
    def _decode_objects(value: Any, label: str) -> list[Mapping[str, Any]]:
        try:
            document = json.loads(value)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError(f"stored {label} JSON is invalid") from error
        if not isinstance(document, list) or not all(
            isinstance(item, dict) for item in document
        ):
            raise ValueError(f"stored {label} must be a list of objects")
        return document


__all__ = [
    "InjurySnapshotRepository",
    "InjurySnapshotScope",
    "StoredInjuryEvidence",
    "StoredInjurySnapshot",
    "StoredInjurySourceSnapshot",
]
