"""Persistence and reads for stored player-tracking values (crf04/statsplus#349).

A game is written whole in one transaction, so collecting it twice leaves
identical rows (a rerun that finds the same values does not even touch
``fetched_at``) and a crash never leaves half a game.  Reads return the four
nullable counts by (game, player); the caller joins them onto game-log rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.engine import Connection, Engine

from app.models.player_tracking import PlayerGameTracking, PlayerTrackingAttempt
from app.services.request_reads import read_connection

_TABLE = PlayerGameTracking.__table__
_ATTEMPTS = PlayerTrackingAttempt.__table__
VALUE_COLUMNS = ("reb_chances", "oreb_chances", "dreb_chances", "potential_ast")


class PlayerTrackingRepository:
    """Own tracking writes (whole-game replace) and the game-log join's reads."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def replace_game(
        self,
        season: str,
        game_id: str,
        game_date: date,
        rows: Sequence[Mapping[str, Any]],
        fetched_at: datetime,
    ) -> bool:
        """Store ``rows`` (``player_id`` plus the four counts) for one game.

        Returns ``False`` and writes nothing when the game already holds
        exactly these values.
        """

        wanted = {int(row["player_id"]): {name: row[name] for name in VALUE_COLUMNS} for row in rows}
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                # Serialize writers of one game (cron against a backfill): the
                # second waits, then sees the first's rows and finds them equal.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"tracking:{game_id}"}
                )
            existing = {
                row["player_id"]: {name: row[name] for name in VALUE_COLUMNS}
                for row in connection.execute(select(_TABLE).where(_TABLE.c.game_id == game_id)).mappings()
            }
            connection.execute(delete(_ATTEMPTS).where(_ATTEMPTS.c.game_id == game_id))
            if existing == wanted:
                return False
            connection.execute(delete(_TABLE).where(_TABLE.c.game_id == game_id))
            if wanted:
                connection.execute(
                    insert(_TABLE),
                    [
                        {
                            "game_id": game_id,
                            "player_id": player_id,
                            "season": season,
                            "game_date": game_date,
                            "fetched_at": fetched_at,
                            **values,
                        }
                        for player_id, values in wanted.items()
                    ],
                )
        return True

    def record_attempt(self, season: str, game_id: str, outcome: str, at: datetime) -> None:
        """Remember an attempt that stored nothing (``not_published`` or ``failed``)."""

        values = {"season": season, "outcome": outcome, "last_attempt_at": at}
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"tracking:{game_id}"}
                )
            changed = connection.execute(
                update(_ATTEMPTS).where(_ATTEMPTS.c.game_id == game_id).values(**values)
            )
            if not changed.rowcount:
                connection.execute(insert(_ATTEMPTS).values(game_id=game_id, **values))

    def attempts(self, season: str) -> dict[str, datetime]:
        """Last attempt time of each unstored game already tried this season."""

        with self.engine.connect() as connection:
            return {
                row[0]: row[1]
                for row in connection.execute(
                    select(_ATTEMPTS.c.game_id, _ATTEMPTS.c.last_attempt_at).where(
                        _ATTEMPTS.c.season == season
                    )
                )
            }

    def stored_game_ids(self, season: str) -> set[str]:
        with self.engine.connect() as connection:
            return {
                row[0]
                for row in connection.execute(
                    select(_TABLE.c.game_id).where(_TABLE.c.season == season).distinct()
                )
            }

    def for_players(
        self,
        season: str,
        player_ids: Iterable[int],
        *,
        connection: Connection | None = None,
    ) -> dict[int, dict[str, dict[str, int | None]]]:
        """``{player_id: {game_id: {reb_chances, ...}}}``; absent games are not listed."""

        ids = sorted({int(player_id) for player_id in player_ids})
        found: dict[int, dict[str, dict[str, int | None]]] = {}
        if not ids:
            return found
        statement = select(
            _TABLE.c.player_id, _TABLE.c.game_id, *(_TABLE.c[name] for name in VALUE_COLUMNS)
        ).where(_TABLE.c.season == season, _TABLE.c.player_id.in_(ids))
        with read_connection(self.engine, connection) as read:
            for row in read.execute(statement).mappings():
                found.setdefault(row["player_id"], {})[row["game_id"]] = {
                    name: row[name] for name in VALUE_COLUMNS
                }
        return found

    def for_player(
        self, season: str, player_id: int, *, connection: Connection | None = None
    ) -> dict[str, dict[str, int | None]]:
        return self.for_players(season, [player_id], connection=connection).get(int(player_id), {})


__all__ = ["VALUE_COLUMNS", "PlayerTrackingRepository"]
