"""Narrow stored query for legacy player-cluster membership."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from app.services.request_reads import read_connection


class PlayerArchetypeRepository:
    """Read canonical peer IDs from the published ``player_clusters`` table."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def list_peer_ids(
        self, player_id: int, *, connection: Connection | None = None
    ) -> tuple[int, ...]:
        statement = text(
            'SELECT DISTINCT peers."PlayerID" '
            "FROM player_clusters AS selected "
            "JOIN player_clusters AS peers "
            'ON peers."ClusterID" = selected."ClusterID" '
            'WHERE selected."PlayerID" = :player_id '
            'AND peers."PlayerID" <> :player_id '
            'ORDER BY peers."PlayerID"'
        )
        with read_connection(self.engine, connection) as scoped:
            # Reflection is done on the request's own connection so the legacy
            # table probe does not cost a second checkout.
            if not inspect(scoped).has_table("player_clusters"):
                return ()
            return tuple(
                int(value)
                for value in scoped.execute(
                    statement, {"player_id": player_id}
                ).scalars()
            )


__all__ = ["PlayerArchetypeRepository"]
