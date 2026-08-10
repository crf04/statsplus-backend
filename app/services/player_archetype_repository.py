"""Narrow stored query for legacy player-cluster membership."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


class PlayerArchetypeRepository:
    """Read canonical peer IDs from the published ``player_clusters`` table."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def list_peer_ids(self, player_id: int) -> tuple[int, ...]:
        if not inspect(self.engine).has_table("player_clusters"):
            return ()
        statement = text(
            'SELECT DISTINCT peers."PlayerID" '
            "FROM player_clusters AS selected "
            "JOIN player_clusters AS peers "
            'ON peers."ClusterID" = selected."ClusterID" '
            'WHERE selected."PlayerID" = :player_id '
            'AND peers."PlayerID" <> :player_id '
            'ORDER BY peers."PlayerID"'
        )
        with self.engine.connect() as connection:
            return tuple(
                int(value)
                for value in connection.execute(
                    statement, {"player_id": player_id}
                ).scalars()
            )


__all__ = ["PlayerArchetypeRepository"]
