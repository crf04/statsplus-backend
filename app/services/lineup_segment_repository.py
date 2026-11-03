"""Persistence for lineup segments and the on/off split read (crf04/statsplus#410).

Writes run on the caller's connection so the rotation job stores a game's
segments in the same transaction as its stints.  A game's rows are replaced
whole.  A player gets the next free roster slot of his team-season the first
time a segment holds him, and keeps it; a 65th player is
:class:`RosterSlotsExhausted` and the game's transaction rolls back.

The split read is one aggregate over ``lineup_segment_players``: a row counts
when its lineup mask holds every ``on`` slot and no ``off`` slot.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, delete, func, insert, literal, or_, select, text, update
from sqlalchemy.engine import Connection, Engine

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.slate_time import EASTERN
from app.models.event_catalog import EventCatalogEntry
from app.models.lineup_segments import (
    MAX_ROSTER_SLOTS,
    LineupRosterSlot,
    LineupSegmentGame,
    LineupSegmentPlayer,
    LineupSegmentSeason,
)
from app.services.lineup_segments import COUNTS, GameSegments
from app.services.request_reads import read_connection

_SLOTS = LineupRosterSlot.__table__
_GAMES = LineupSegmentGame.__table__
_PLAYERS = LineupSegmentPlayer.__table__
_SEASONS = LineupSegmentSeason.__table__
_EVENTS = EventCatalogEntry.__table__
_TEAM_TOTALS = ("team_fga", "team_fta", "team_tov", "team_heaves", "off_poss")


class RosterSlotsExhausted(RuntimeError):
    """A team-season needs more players than a lineup mask has bits."""


@dataclass(frozen=True, slots=True)
class SegmentSplit:
    """One lineup state's sums per player, and the games behind them."""

    #: Per player: ``player_id``, ``name``, ``duration_cs``, the :data:`COUNTS`
    #: and the team's on-floor totals while he played in the state.
    rows: tuple[Mapping[str, Any], ...]
    #: When the newest stored game of the team-season was written.
    written_at: datetime | None
    #: The team-season's rejected games, which have no segments, oldest first:
    #: ``{game_id, game_date, matchup}`` with the Eastern date and ``"AWAY @ HOME"``.
    games_missing: tuple[Mapping[str, Any], ...]


def mask_of(slots: Iterable[int]) -> int:
    """The signed 64-bit lineup mask with one bit per slot."""

    mask = 0
    for slot in slots:
        mask |= 1 << slot
    return mask - (1 << 64) if mask >= 1 << 63 else mask


def write_game(connection: Connection, season: str, segments: GameSegments, written_at: datetime) -> None:
    """Write one game's segments and mark it stored, after ``delete_game``."""

    rows = []
    for team_id in (segments.home_team_id, segments.away_team_id):
        team_segments = [segment for segment in segments.segments if segment.team_id == team_id]
        slots = _assign_slots(
            connection,
            season,
            team_id,
            [player_id for segment in team_segments for player_id in segment.players],
            segments.names,
        )
        for segment in team_segments:
            mask = mask_of(slots[player_id] for player_id in segment.players)
            for player_id in segment.players:
                rows.append(
                    {
                        "game_id": segments.game_id,
                        "team_id": team_id,
                        "seg": segment.seg,
                        "player_id": player_id,
                        "season": season,
                        "lineup_mask": mask,
                        "duration_cs": segment.duration_cs,
                        **segment.counts[player_id],
                        "team_fga": segment.team_fga,
                        "team_fta": segment.team_fta,
                        "team_tov": segment.team_tov,
                        "team_heaves": segment.team_heaves,
                        "off_poss": segment.off_poss,
                    }
                )
    if rows:
        connection.execute(insert(_PLAYERS), rows)
    connection.execute(
        insert(_GAMES).values(
            game_id=segments.game_id,
            season=season,
            home_team_id=segments.home_team_id,
            away_team_id=segments.away_team_id,
            written_at=written_at,
            rejected_reason=None,
        )
    )


def record_rejected(
    connection: Connection,
    season: str,
    game_id: str,
    team_ids: tuple[int, int],
    reason: str,
    at: datetime,
) -> None:
    """Mark a game with no segments as missing, unless segments are stored for it.

    A forced retry of a stored game that is rejected this time leaves the
    stored rotation alone, so its stored segments stay too.
    """

    stored = connection.execute(
        select(_GAMES.c.game_id).where(_GAMES.c.game_id == game_id, _GAMES.c.rejected_reason.is_(None))
    ).first()
    if stored is not None:
        return
    connection.execute(delete(_GAMES).where(_GAMES.c.game_id == game_id))
    connection.execute(
        insert(_GAMES).values(
            game_id=game_id,
            season=season,
            home_team_id=team_ids[0],
            away_team_id=team_ids[1],
            written_at=at,
            rejected_reason=reason,
        )
    )


def delete_game(connection: Connection, game_id: str) -> None:
    connection.execute(delete(_PLAYERS).where(_PLAYERS.c.game_id == game_id))
    connection.execute(delete(_GAMES).where(_GAMES.c.game_id == game_id))


def _assign_slots(
    connection: Connection,
    season: str,
    team_id: int,
    players: Sequence[int],
    names: Mapping[int, str],
) -> dict[int, int]:
    if connection.dialect.name == "postgresql":
        # Two writers (the nightly job and a backfill) must not hand out the
        # same next slot of one team-season.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"lineup_slots:{season}:{team_id}"},
        )
    slots = {
        row.player_id: row.slot
        for row in connection.execute(
            select(_SLOTS.c.player_id, _SLOTS.c.slot).where(
                _SLOTS.c.season == season, _SLOTS.c.team_id == team_id
            )
        )
    }
    following = max(slots.values(), default=-1) + 1
    new = []
    for player_id in dict.fromkeys(players):
        if player_id in slots:
            continue
        if following >= MAX_ROSTER_SLOTS:
            raise RosterSlotsExhausted(
                f"team {team_id} needs more than {MAX_ROSTER_SLOTS} roster slots in {season}"
            )
        slots[player_id] = following
        new.append(
            {
                "season": season,
                "team_id": team_id,
                "player_id": player_id,
                "slot": following,
                "player_name": names.get(player_id, ""),
            }
        )
        following += 1
    if new:
        connection.execute(insert(_SLOTS), new)
    return slots


class LineupSegmentRepository:
    """Season coverage, the backfill's progress reads and the split read."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def written_game_ids(self, season: str) -> set[str]:
        """Games stored or rejected: the ones a backfill no longer needs."""

        with self.engine.connect() as connection:
            return set(
                connection.execute(select(_GAMES.c.game_id).where(_GAMES.c.season == season)).scalars()
            )

    def record_coverage(self, season: str, at: datetime) -> None:
        """Serve ``season`` from segments from now on; a rerun keeps the first time."""

        with self.engine.begin() as connection:
            row = connection.execute(select(_SEASONS.c.complete).where(_SEASONS.c.season == season)).first()
            if row is None:
                connection.execute(insert(_SEASONS).values(season=season, complete=True, completed_at=at))
            elif not row.complete:
                connection.execute(
                    update(_SEASONS).where(_SEASONS.c.season == season).values(complete=True, completed_at=at)
                )

    def covered(self, season: str, *, connection: Connection | None = None) -> bool:
        with read_connection(self.engine, connection) as connection:
            return bool(
                connection.execute(select(_SEASONS.c.complete).where(_SEASONS.c.season == season)).scalar()
            )

    def read_split(
        self, season: str, team_id: int, on: Sequence[int], off: Sequence[int]
    ) -> SegmentSplit | None:
        """The split, or ``None`` when ``season`` is not covered.

        An ``on`` player with no slot never played for the team that season,
        so no segment holds him and the split is empty; an ``off`` player with
        no slot is off the floor in every segment.
        """

        with self.engine.connect() as connection:
            if not self.covered(season, connection=connection):
                return None
            roster = {
                row.player_id: (row.slot, row.player_name)
                for row in connection.execute(
                    select(_SLOTS.c.player_id, _SLOTS.c.slot, _SLOTS.c.player_name).where(
                        _SLOTS.c.season == season, _SLOTS.c.team_id == team_id
                    )
                )
            }
            games = connection.execute(
                select(
                    _GAMES.c.game_id,
                    _GAMES.c.home_team_id,
                    _GAMES.c.away_team_id,
                    _GAMES.c.written_at,
                    _GAMES.c.rejected_reason,
                    _EVENTS.c.scheduled_at,
                    _EVENTS.c.home_team_tricode,
                    _EVENTS.c.away_team_tricode,
                )
                .select_from(_GAMES.outerjoin(_EVENTS, _EVENTS.c.nba_game_id == _GAMES.c.game_id))
                .where(
                    _GAMES.c.season == season,
                    or_(_GAMES.c.home_team_id == team_id, _GAMES.c.away_team_id == team_id),
                )
            ).all()
            rows: tuple[Mapping[str, Any], ...] = ()
            if all(player_id in roster for player_id in on):
                rows = self._sums(
                    connection,
                    season,
                    team_id,
                    mask_of(roster[player_id][0] for player_id in on),
                    mask_of(roster[player_id][0] for player_id in off if player_id in roster),
                    {player_id: name for player_id, (_slot, name) in roster.items()},
                )
        written = [game.written_at for game in games if game.rejected_reason is None]
        return SegmentSplit(
            rows=rows,
            written_at=_utc(max(written)) if written else None,
            games_missing=tuple(
                sorted(
                    (_missing_game(game) for game in games if game.rejected_reason is not None),
                    key=lambda game: (game["game_date"] or "", game["game_id"]),
                )
            ),
        )

    @staticmethod
    def _sums(
        connection: Connection,
        season: str,
        team_id: int,
        on_mask: int,
        off_mask: int,
        names: Mapping[int, str],
    ) -> tuple[Mapping[str, Any], ...]:
        mask = _PLAYERS.c.lineup_mask
        query = (
            select(
                _PLAYERS.c.player_id,
                func.sum(_PLAYERS.c.duration_cs).label("duration_cs"),
                *(func.sum(_PLAYERS.c[key]).label(key) for key in (*COUNTS, *_TEAM_TOTALS)),
            )
            .where(_PLAYERS.c.team_id == team_id, _PLAYERS.c.season == season)
            .group_by(_PLAYERS.c.player_id)
        )
        if on_mask:
            on = literal(on_mask, BigInteger())
            query = query.where(mask.op("&")(on) == on)
        if off_mask:
            query = query.where(mask.op("&")(literal(off_mask, BigInteger())) == 0)
        return tuple(
            {**row, "name": names.get(row["player_id"], "")}
            for row in (dict(row) for row in connection.execute(query).mappings())
        )


def _missing_game(game: Any) -> dict[str, Any]:
    """A rejected game named by its Event Catalog date and teams.

    A game missing from the catalog (only a forced ``--game-id`` collects one)
    keeps its teams from the box score and has no date.
    """

    home = game.home_team_tricode or NBA_TEAM_ID_TO_TRICODE.get(game.home_team_id, str(game.home_team_id))
    away = game.away_team_tricode or NBA_TEAM_ID_TO_TRICODE.get(game.away_team_id, str(game.away_team_id))
    return {
        "game_id": game.game_id,
        "game_date": None
        if game.scheduled_at is None
        else _utc(game.scheduled_at).astimezone(EASTERN).date().isoformat(),
        "matchup": f"{away} @ {home}",
    }


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every stored time is UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "LineupSegmentRepository",
    "RosterSlotsExhausted",
    "SegmentSplit",
    "delete_game",
    "mask_of",
    "record_rejected",
    "write_game",
]
