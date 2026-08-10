"""Atomic persistence for governed Player Pool snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.models.player_pool_snapshot import PlayerPoolSnapshot


@dataclass(frozen=True, slots=True)
class StoredPlayerPoolSnapshot:
    payload: Mapping[str, Any]
    retrieved_at: datetime


class PlayerPoolSnapshotRepository:
    """Store one canonical JSON document per exact Player Pool request scope."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _scope(game_ids: Iterable[str]) -> str:
        return json.dumps(sorted({str(game_id) for game_id in game_ids}), separators=(",", ":"))

    def get(self, *, season: str, game_ids: Iterable[str]) -> StoredPlayerPoolSnapshot | None:
        scope = self._scope(game_ids)
        with Session(self.engine) as session:
            row = session.scalar(
                select(PlayerPoolSnapshot).where(
                    PlayerPoolSnapshot.season == season,
                    PlayerPoolSnapshot.game_ids == scope,
                )
            )
            if row is None:
                return None
            retrieved_at = row.retrieved_at
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
            payload = json.loads(row.payload)
            if not isinstance(payload, dict):
                raise ValueError("stored Player Pool snapshot must be an object")
            return StoredPlayerPoolSnapshot(payload, retrieved_at.astimezone(timezone.utc))

    def replace(
        self,
        *,
        season: str,
        game_ids: Iterable[str],
        payload: Mapping[str, Any],
        retrieved_at: datetime,
    ) -> None:
        scope = self._scope(game_ids)
        document = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        observed_at = retrieved_at.astimezone(timezone.utc)
        values = {"payload": document, "retrieved_at": observed_at, "updated_at": func.now()}
        with Session(self.engine) as session:
            result = session.execute(
                update(PlayerPoolSnapshot)
                .where(
                    PlayerPoolSnapshot.season == season,
                    PlayerPoolSnapshot.game_ids == scope,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                session.add(
                    PlayerPoolSnapshot(
                        season=season,
                        game_ids=scope,
                        payload=document,
                        retrieved_at=observed_at,
                    )
                )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                session.execute(
                    update(PlayerPoolSnapshot)
                    .where(
                        PlayerPoolSnapshot.season == season,
                        PlayerPoolSnapshot.game_ids == scope,
                    )
                    .values(**values)
                )
                session.commit()

__all__ = ["PlayerPoolSnapshotRepository", "StoredPlayerPoolSnapshot"]
