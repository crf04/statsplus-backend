"""Persistence for stored LiveData rotations (crf04/statsplus#223).

Writes replace one whole game inside a single transaction, so collecting the
same game twice leaves identical rows and a crash never leaves half a game.
Reads return plain dictionaries; every statistic is computed by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.models.rotation import RotationAttempt, RotationGame, RotationPlayerGame, RotationStint
from app.services.request_reads import read_connection
from app.services.rotation_derivation import GameRotation

_GAMES = RotationGame.__table__
_PLAYER_GAMES = RotationPlayerGame.__table__
_STINTS = RotationStint.__table__
_ATTEMPTS = RotationAttempt.__table__


class RotationRepository:
    """Own rotation writes (whole-game replace) and the route's reads."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- writes ---------------------------------------------------------

    def replace_game(
        self, season: str, rotation: GameRotation, collected_at: datetime
    ) -> None:
        """Store ``rotation``, replacing any rows already stored for the game."""

        with self.engine.begin() as connection:
            self._delete_game(connection, rotation.game_id)
            connection.execute(
                delete(_ATTEMPTS).where(_ATTEMPTS.c.game_id == rotation.game_id)
            )
            connection.execute(insert(_GAMES).values(
                game_id=rotation.game_id,
                season=season,
                game_date=date.fromisoformat(rotation.game_date),
                home_tricode=rotation.home_tricode,
                away_tricode=rotation.away_tricode,
                periods=rotation.periods,
                collected_at=collected_at,
            ))
            connection.execute(insert(_PLAYER_GAMES), [
                {
                    "game_id": rotation.game_id,
                    "player_id": player.player_id,
                    "team_tricode": player.team_tricode,
                    "player_name": player.name,
                    "started": player.started,
                    "minutes_cs": player.minutes_cs,
                    "closed": player.closed,
                    "q1_cs": player.period_cs["Q1"],
                    "q2_cs": player.period_cs["Q2"],
                    "q3_cs": player.period_cs["Q3"],
                    "q4_cs": player.period_cs["Q4"],
                    "ot_cs": player.period_cs["OT"],
                }
                for player in rotation.players
            ])
            stints = [
                {
                    "game_id": rotation.game_id,
                    "player_id": player.player_id,
                    "seq": seq,
                    "in_period": stint.in_period,
                    "in_clock_cs": stint.in_clock_cs,
                    "out_period": stint.out_period,
                    "out_clock_cs": stint.out_clock_cs,
                    "minutes_cs": stint.minutes_cs,
                }
                for player in rotation.players
                for seq, stint in enumerate(player.stints)
            ]
            if stints:
                connection.execute(insert(_STINTS), stints)

    def record_attempt(
        self, season: str, game_id: str, outcome: str, reason: str, at: datetime
    ) -> None:
        """Remember an attempt that stored nothing.

        ``outcome`` is ``"rejected"`` (adds to the game's rejection count) or
        ``"failed"`` (a fetch failure: refreshes the attempt time only).
        """

        with self.engine.begin() as connection:
            row = connection.execute(
                select(_ATTEMPTS.c.rejections).where(_ATTEMPTS.c.game_id == game_id)
            ).first()
            rejections = (row[0] if row else 0) + (outcome == "rejected")
            values = {
                "season": season, "rejections": rejections, "last_outcome": outcome,
                "last_reason": reason, "last_attempt_at": at,
            }
            if row is None:
                connection.execute(insert(_ATTEMPTS).values(game_id=game_id, **values))
            else:
                connection.execute(
                    update(_ATTEMPTS).where(_ATTEMPTS.c.game_id == game_id).values(**values)
                )

    def attempts(self, season: str) -> dict[str, dict[str, Any]]:
        """Unstored games already tried this season, by game id."""

        with self.engine.connect() as connection:
            return {
                row["game_id"]: dict(row)
                for row in connection.execute(
                    select(_ATTEMPTS).where(_ATTEMPTS.c.season == season)
                ).mappings()
            }

    @staticmethod
    def _delete_game(connection: Connection, game_id: str) -> None:
        for table in (_STINTS, _PLAYER_GAMES, _GAMES):
            connection.execute(delete(table).where(table.c.game_id == game_id))

    # -- reads ----------------------------------------------------------

    def stored_game_ids(self, season: str) -> set[str]:
        with self.engine.connect() as connection:
            return {
                row[0]
                for row in connection.execute(
                    select(_GAMES.c.game_id).where(_GAMES.c.season == season)
                )
            }

    def seasons(self) -> list[str]:
        """Seasons with stored rotations, newest first."""

        with self.engine.connect() as connection:
            return sorted(
                (row[0] for row in connection.execute(select(_GAMES.c.season).distinct())),
                reverse=True,
            )

    def team_games(
        self,
        season: str,
        tricode: str,
        *,
        connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        """A team's stored games in a season, newest first."""

        with read_connection(self.engine, connection) as connection:
            rows = connection.execute(
                select(_GAMES)
                .where(
                    _GAMES.c.season == season,
                    (_GAMES.c.home_tricode == tricode) | (_GAMES.c.away_tricode == tricode),
                )
                .order_by(_GAMES.c.game_date.desc(), _GAMES.c.game_id.desc())
            ).mappings()
            return [dict(row) for row in rows]

    def player_games(
        self,
        season: str,
        *,
        player_ids: Iterable[int] | None = None,
        game_ids: Iterable[str] | None = None,
        team: str | None = None,
        connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Box-score player rows of a season, newest game first.

        ``player_ids``, ``game_ids`` and ``team`` (the team on the row) narrow
        the read; an empty ``player_ids`` or ``game_ids`` selects nothing.
        """

        stint_count = (
            select(func.count())
            .select_from(_STINTS)
            .where(
                _STINTS.c.game_id == _PLAYER_GAMES.c.game_id,
                _STINTS.c.player_id == _PLAYER_GAMES.c.player_id,
            )
            .scalar_subquery()
        )
        query = (
            select(
                _PLAYER_GAMES,
                stint_count.label("stint_count"),
                _GAMES.c.game_date,
                _GAMES.c.home_tricode,
                _GAMES.c.away_tricode,
            )
            .join(_GAMES, _GAMES.c.game_id == _PLAYER_GAMES.c.game_id)
            .where(_GAMES.c.season == season)
            .order_by(_GAMES.c.game_date.desc(), _GAMES.c.game_id.desc(), _PLAYER_GAMES.c.player_id)
        )
        if player_ids is not None:
            ids = list(player_ids)
            if not ids:
                return []
            query = query.where(_PLAYER_GAMES.c.player_id.in_(ids))
        if game_ids is not None:
            games = list(game_ids)
            if not games:
                return []
            query = query.where(_PLAYER_GAMES.c.game_id.in_(games))
        if team is not None:
            query = query.where(_PLAYER_GAMES.c.team_tricode == team)
        with read_connection(self.engine, connection) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def stints(
        self,
        player_id: int,
        game_ids: Sequence[str],
        *,
        connection: Connection | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """One player's stints in the given games, chronological per game."""

        if not game_ids:
            return {}
        with read_connection(self.engine, connection) as connection:
            rows = connection.execute(
                select(_STINTS)
                .where(
                    _STINTS.c.player_id == player_id,
                    _STINTS.c.game_id.in_(list(game_ids)),
                )
                .order_by(_STINTS.c.game_id, _STINTS.c.seq)
            ).mappings()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                grouped.setdefault(row["game_id"], []).append(dict(row))
            return grouped


__all__ = ["RotationRepository"]
