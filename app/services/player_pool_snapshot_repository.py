"""Atomic persistence and cross-process refresh leases for Player Pools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.domain.utc import assume_utc
from app.domain.freshness import exact_timedelta, time_window_seconds
from app.models.player_pool_snapshot import PlayerPoolSnapshot
from app.utils.db import is_demo_database_url


@dataclass(frozen=True, slots=True)
class PlayerPoolSnapshotScope:
    """The single canonical identity of a persisted pool request."""

    season: str
    game_ids: tuple[str, ...]

    @classmethod
    def create(cls, season: str, game_ids: Iterable[str]) -> PlayerPoolSnapshotScope:
        return cls(str(season), tuple(sorted({str(game_id) for game_id in game_ids})))

    @property
    def storage_game_ids(self) -> str:
        return json.dumps(self.game_ids, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class StoredPlayerPoolSnapshot:
    payload: Mapping[str, Any]
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class StoredPlayerPoolSnapshotCandidate:
    scope: PlayerPoolSnapshotScope
    retrieved_at: datetime
    refresh_outcome: str | None


@dataclass(frozen=True, slots=True)
class PlayerPoolRefreshResult:
    version: int
    outcome: str | None
    lease_expires_at: datetime | None


class PlayerPoolSnapshotRepository:
    """Store canonical pool documents and coordinate their lazy refresh."""

    # The legacy request-time Player Pool is published through the
    # ``dfs_boards`` stream.  Activating that stream fences every write here so
    # the empty legacy ``player_pool_snapshots`` table can never be refreshed
    # after the #110 database-only cutover, ahead of the #111 table drop.
    LEGACY_WRITE_STREAM_KEY = "dfs_boards"

    def __init__(self, engine: Engine, *, write_fence: Any | None = None) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store Player Pool snapshots")
        self.engine = engine
        self._write_fence = write_fence

    def _assert_writable(self, *, connection: Any | None = None) -> None:
        """Refuse a legacy pool write once the cutover fence is activated."""

        if self._write_fence is not None:
            checker = getattr(self._write_fence, "assert_writable", None)
            if callable(checker):
                checker(self.LEGACY_WRITE_STREAM_KEY, connection=connection)

    @staticmethod
    def _identity(table: Any, scope: PlayerPoolSnapshotScope) -> Any:
        return and_(
            table.c.season == scope.season,
            table.c.game_ids == scope.storage_game_ids,
        )

    def get(self, scope: PlayerPoolSnapshotScope) -> StoredPlayerPoolSnapshot | None:
        table = PlayerPoolSnapshot.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(table.c.payload, table.c.retrieved_at).where(
                    self._identity(table, scope)
                )
            ).mappings().one_or_none()
        if row is None or row["payload"] is None or row["retrieved_at"] is None:
            return None
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError("stored Player Pool snapshot must be an object")
        return StoredPlayerPoolSnapshot(payload, assume_utc(row["retrieved_at"]))

    def list_containing_game(
        self, season: str, game_id: str
    ) -> tuple[StoredPlayerPoolSnapshotCandidate, ...]:
        """Read candidate metadata without loading season-wide snapshot payloads."""

        table = PlayerPoolSnapshot.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    table.c.game_ids,
                    table.c.retrieved_at,
                    table.c.refresh_outcome,
                )
                .where(
                    table.c.season == str(season),
                    table.c.payload.is_not(None),
                    table.c.retrieved_at.is_not(None),
                )
                .order_by(table.c.retrieved_at.desc(), table.c.game_ids.asc())
            ).mappings()
            candidates = []
            for row in rows:
                try:
                    game_ids = json.loads(row["game_ids"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(game_ids, list) or not all(
                    isinstance(value, str) for value in game_ids
                ):
                    continue
                scope = PlayerPoolSnapshotScope.create(str(season), game_ids)
                if scope.storage_game_ids != row["game_ids"]:
                    continue
                if str(game_id) in scope.game_ids:
                    candidates.append(
                        StoredPlayerPoolSnapshotCandidate(
                            scope,
                            assume_utc(row["retrieved_at"]),
                            row["refresh_outcome"],
                        )
                    )
        return tuple(candidates)

    def get_refresh_result(self, scope: PlayerPoolSnapshotScope) -> PlayerPoolRefreshResult:
        table = PlayerPoolSnapshot.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    table.c.refresh_version,
                    table.c.refresh_outcome,
                    table.c.lease_expires_at,
                ).where(
                    self._identity(table, scope)
                )
            ).one_or_none()
        if row is None:
            return PlayerPoolRefreshResult(0, None, None)
        return PlayerPoolRefreshResult(
            int(row.refresh_version),
            row.refresh_outcome,
            assume_utc(row.lease_expires_at) if row.lease_expires_at is not None else None,
        )

    def try_acquire_refresh(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        now: datetime,
        lease_seconds: object,
    ) -> bool:
        table = PlayerPoolSnapshot.__table__
        observed_at = assume_utc(now)
        expires_at = observed_at + exact_timedelta(
            time_window_seconds(lease_seconds, field="Player Pool refresh lease"),
            field="Player Pool refresh lease",
        )
        with self.engine.begin() as connection:
            self._assert_writable(connection=connection)
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(table).values(
                            season=scope.season,
                            game_ids=scope.storage_game_ids,
                            lease_owner=owner,
                            lease_expires_at=expires_at,
                            refresh_version=0,
                        )
                    )
                return True
            except IntegrityError:
                pass
            claimed = connection.execute(
                update(table)
                .where(
                    self._identity(table, scope),
                    or_(
                        table.c.lease_owner.is_(None),
                        table.c.lease_expires_at.is_(None),
                        table.c.lease_expires_at <= observed_at,
                    ),
                )
                .values(lease_owner=owner, lease_expires_at=expires_at)
            )
            return claimed.rowcount == 1

    def replace_owned(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        payload: Mapping[str, Any],
        retrieved_at: datetime,
        now: datetime,
    ) -> bool:
        table = PlayerPoolSnapshot.__table__
        document = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        observed_at = assume_utc(now)
        result = None
        with self.engine.begin() as connection:
            self._assert_writable(connection=connection)
            result = connection.execute(
                update(table)
                .where(
                    self._identity(table, scope),
                    table.c.lease_owner == owner,
                    table.c.lease_expires_at > observed_at,
                )
                .values(
                    payload=document,
                    retrieved_at=assume_utc(retrieved_at),
                    updated_at=observed_at,
                    lease_owner=None,
                    lease_expires_at=None,
                    refresh_version=table.c.refresh_version + 1,
                    refresh_outcome="success",
                )
            )
        return result.rowcount == 1

    def finish_failure_owned(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        now: datetime,
    ) -> bool:
        table = PlayerPoolSnapshot.__table__
        observed_at = assume_utc(now)
        with self.engine.begin() as connection:
            self._assert_writable(connection=connection)
            result = connection.execute(
                update(table)
                .where(
                    self._identity(table, scope),
                    table.c.lease_owner == owner,
                    table.c.lease_expires_at > observed_at,
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    refresh_version=table.c.refresh_version + 1,
                    refresh_outcome="failure",
                )
            )
        return result.rowcount == 1

    def release_refresh(self, scope: PlayerPoolSnapshotScope, *, owner: str) -> None:
        table = PlayerPoolSnapshot.__table__
        with self.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(self._identity(table, scope), table.c.lease_owner == owner)
                .values(lease_owner=None, lease_expires_at=None)
            )


__all__ = [
    "PlayerPoolSnapshotRepository",
    "PlayerPoolSnapshotScope",
    "PlayerPoolRefreshResult",
    "StoredPlayerPoolSnapshotCandidate",
    "StoredPlayerPoolSnapshot",
]
