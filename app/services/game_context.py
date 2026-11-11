"""Each team's context before one game, stored for the Matchup (crf04/statsplus#466).

The refresh recomputes every eligible Event Catalog game of a season from the
Canonical Game Ledger and writes only the rows whose values changed, so a rerun
changes nothing and a game that left the schedule loses its rows.  The Matchup
reads the stored rows and computes nothing.

Every input is strictly before the game's Eastern date.  Ratings and results
come from the team's final Regular Season games in the ledger; rest comes from
the team's previous eligible game in the Event Catalog, so a game the ledger has
not ingested yet still counts for rest.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.domain.nba_events import canonical_event_kind, is_postponed_event
from app.domain.utc import assume_utc
from app.models.canonical_game_ledger import CanonicalGameLedgerGame, CanonicalGameLedgerTeamFact
from app.models.event_catalog import EventCatalogEntry
from app.models.game_context import GameContext, GameContextTeam
from app.services.event_catalog_repository import EventCatalogRepository
from app.services.game_lines import read_game_line
from app.services.request_reads import read_connection

EASTERN = ZoneInfo("America/New_York")
#: Event kinds that are not part of a team's competitive schedule.
EXCLUDED_EVENT_KINDS = frozenset({"Preseason", "All-Star"})
LAST_N_RATINGS = 10
LAST_N_RESULTS = 5
WINDOWS = ("season", "last_10")
RATING_FIELDS = ("pace", "offensive_rating", "defensive_rating", "net_rating")


@dataclass(frozen=True, slots=True)
class TeamGame:
    """One team's side of one final ledger game."""

    game_id: str
    game_date: date
    team_id: int
    opponent_tricode: str
    is_home: bool
    points: int
    opponent_points: int
    team_minutes: float
    possessions: float | None
    opponent_possessions: float | None


@dataclass(frozen=True, slots=True)
class ScheduledGame:
    """One eligible Event Catalog game."""

    game_id: str
    game_date: date
    away_team_id: int
    away_tricode: str
    home_team_id: int
    home_tricode: str


def ratings(games: Sequence[TeamGame]) -> dict[str, Any]:
    """Pace and ratings over the games whose possessions both sides carry, each rounded to 1 decimal.

    Net is the unrounded offensive minus defensive rating, rounded once.
    """

    rated = [
        game for game in games if game.possessions and game.opponent_possessions and game.team_minutes > 0
    ]
    block: dict[str, Any] = {"games": len(games), "possession_games": len(rated)}
    if not rated:
        return {**block, **dict.fromkeys(RATING_FIELDS)}
    own = math.fsum(game.possessions for game in rated)
    opponent = math.fsum(game.opponent_possessions for game in rated)
    offensive = 100.0 * sum(game.points for game in rated) / own
    defensive = 100.0 * sum(game.opponent_points for game in rated) / opponent
    pace = 48.0 * (own + opponent) / 2.0 / math.fsum(game.team_minutes for game in rated)
    return {
        **block,
        "pace": round(pace, 1),
        "offensive_rating": round(offensive, 1),
        "defensive_rating": round(defensive, 1),
        "net_rating": round(offensive - defensive, 1),
    }


def result(game: TeamGame) -> dict[str, Any]:
    margin = game.points - game.opponent_points
    return {
        "game_date": game.game_date.isoformat(),
        "opponent_tricode": game.opponent_tricode,
        "is_home": game.is_home,
        "points": game.points,
        "opponent_points": game.opponent_points,
        "result": "W" if margin > 0 else "L",
        "margin": margin,
    }


def team_context(
    *,
    game_date: date,
    played: Sequence[TeamGame],
    schedule_dates: Iterable[date],
) -> dict[str, Any]:
    """One team's context before a game on ``game_date``.

    ``played`` is the team's final ledger games and ``schedule_dates`` its
    eligible scheduled game dates, in any order and possibly including the game
    itself and later ones; only those strictly before ``game_date`` count.
    """

    before = sorted(
        (game for game in played if game.game_date < game_date),
        key=lambda game: (game.game_date, game.game_id),
        reverse=True,
    )
    previous = max((day for day in schedule_dates if day < game_date), default=None)
    rest_days = None if previous is None else (game_date - previous).days - 1
    return {
        "through_game_date": before[0].game_date.isoformat() if before else None,
        "rest": {
            "previous_game_date": None if previous is None else previous.isoformat(),
            "rest_days": rest_days,
            "back_to_back": rest_days == 0,
        },
        "ratings": {
            "season": ratings(before),
            "last_10": ratings(before[:LAST_N_RATINGS]),
        },
        "last_5": [result(game) for game in before[:LAST_N_RESULTS]],
    }


def eastern_date(value: datetime) -> date:
    return assume_utc(value).astimezone(EASTERN).date()


class GameContextRepository:
    """Refreshes and reads the stored team context of each game."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self.engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- refresh ------------------------------------------------------------

    def refresh(self, season: str) -> dict[str, Any]:
        """Recompute every eligible game of ``season`` and write what changed, in one transaction."""

        with self.engine.begin() as connection:
            schedule = self._schedule(connection, season)
            played = self._played(connection, season)
            dates_by_team: dict[int, list[date]] = {}
            for game in schedule:
                dates_by_team.setdefault(game.away_team_id, []).append(game.game_date)
                dates_by_team.setdefault(game.home_team_id, []).append(game.game_date)
            wanted = {game.game_id: self._rows(game, played, dates_by_team, season) for game in schedule}
            stored = self._stored(connection, season)
            now = assume_utc(self._clock())
            inserted = updated = 0
            for game_id, (head, teams) in wanted.items():
                existing = stored.pop(game_id, None)
                if existing is None:
                    connection.execute(insert(GameContext.__table__).values(**head, computed_at=now))
                    connection.execute(insert(GameContextTeam.__table__), teams)
                    inserted += 1
                elif existing != (head, teams):
                    connection.execute(
                        delete(GameContextTeam.__table__).where(GameContextTeam.game_id == game_id)
                    )
                    connection.execute(
                        update(GameContext.__table__)
                        .where(GameContext.game_id == game_id)
                        .values(**head, computed_at=now)
                    )
                    connection.execute(insert(GameContextTeam.__table__), teams)
                    updated += 1
            removed = sorted(stored)
            if removed:
                connection.execute(
                    delete(GameContextTeam.__table__).where(GameContextTeam.game_id.in_(removed))
                )
                connection.execute(delete(GameContext.__table__).where(GameContext.game_id.in_(removed)))
        return {
            "season": season,
            "games": len(wanted),
            "ledger_games": len({game.game_id for games in played.values() for game in games}),
            "inserted": inserted,
            "updated": updated,
            "removed": len(removed),
            "unchanged": len(wanted) - inserted - updated,
        }

    @staticmethod
    def _schedule(connection: Connection, season: str) -> list[ScheduledGame]:
        table = EventCatalogEntry.__table__
        rows = connection.execute(select(table).where(table.c.season == season)).mappings()
        games = []
        for row in rows:
            if canonical_event_kind(row["nba_game_id"], row["classification"] or "") in EXCLUDED_EVENT_KINDS:
                continue
            # Decoded as the Event Catalog serves it, so stored empty evidence ('{}') is no postponement.
            if is_postponed_event(EventCatalogRepository._serialize(row)):
                continue
            games.append(
                ScheduledGame(
                    game_id=row["nba_game_id"],
                    game_date=eastern_date(row["scheduled_at"]),
                    away_team_id=int(row["away_team_id"]),
                    away_tricode=row["away_team_tricode"],
                    home_team_id=int(row["home_team_id"]),
                    home_tricode=row["home_team_tricode"],
                )
            )
        return games

    @staticmethod
    def _played(connection: Connection, season: str) -> dict[int, list[TeamGame]]:
        games = CanonicalGameLedgerGame.__table__
        facts = CanonicalGameLedgerTeamFact.__table__
        rows = connection.execute(
            select(
                games.c.game_id,
                games.c.game_date,
                facts.c.team_id,
                facts.c.opponent_team_id,
                facts.c.opponent_team_tricode,
                facts.c.is_home,
                facts.c.points,
                facts.c.team_minutes,
                facts.c.offensive_possessions,
            )
            .join(facts, facts.c.game_id == games.c.game_id)
            .where(games.c.season == season)
        ).mappings()
        sides: dict[str, dict[int, Mapping[str, Any]]] = {}
        for row in rows:
            sides.setdefault(row["game_id"], {})[int(row["team_id"])] = row
        played: dict[int, list[TeamGame]] = {}
        for pair in sides.values():
            if len(pair) != 2:
                continue
            for team_id, row in pair.items():
                opponent = pair.get(int(row["opponent_team_id"]))
                if opponent is None:
                    continue
                played.setdefault(team_id, []).append(
                    TeamGame(
                        game_id=row["game_id"],
                        game_date=row["game_date"],
                        team_id=team_id,
                        opponent_tricode=row["opponent_team_tricode"],
                        is_home=bool(row["is_home"]),
                        points=int(row["points"]),
                        opponent_points=int(opponent["points"]),
                        team_minutes=float(row["team_minutes"] or 0.0),
                        possessions=_float(row["offensive_possessions"]),
                        opponent_possessions=_float(opponent["offensive_possessions"]),
                    )
                )
        return played

    @staticmethod
    def _rows(
        game: ScheduledGame,
        played: Mapping[int, Sequence[TeamGame]],
        dates_by_team: Mapping[int, Sequence[date]],
        season: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        head = {
            "game_id": game.game_id,
            "season": season,
            "game_date": game.game_date,
            "away_team_id": game.away_team_id,
            "home_team_id": game.home_team_id,
        }
        teams = []
        for team_id, tricode, is_home in (
            (game.away_team_id, game.away_tricode, False),
            (game.home_team_id, game.home_tricode, True),
        ):
            context = team_context(
                game_date=game.game_date,
                played=played.get(team_id, ()),
                schedule_dates=dates_by_team.get(team_id, ()),
            )
            row: dict[str, Any] = {
                "game_id": game.game_id,
                "team_id": team_id,
                "tricode": tricode,
                "is_home": is_home,
                "through_game_date": _date(context["through_game_date"]),
                "previous_game_date": _date(context["rest"]["previous_game_date"]),
                "rest_days": context["rest"]["rest_days"],
                "back_to_back": context["rest"]["back_to_back"],
                "last_5": json.dumps(context["last_5"], sort_keys=True),
            }
            for window in WINDOWS:
                block = context["ratings"][window]
                row[f"{window}_games"] = block["games"]
                row[f"{window}_possession_games"] = block["possession_games"]
                for name in RATING_FIELDS:
                    row[f"{window}_{name}"] = block[name]
            teams.append(row)
        return head, teams

    @staticmethod
    def _stored(
        connection: Connection, season: str
    ) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
        heads = GameContext.__table__
        teams = GameContextTeam.__table__
        stored: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for row in connection.execute(select(heads).where(heads.c.season == season)).mappings():
            head = {
                key: row[key] for key in ("game_id", "season", "game_date", "away_team_id", "home_team_id")
            }
            stored[row["game_id"]] = (head, [])
        team_rows = connection.execute(
            select(teams)
            .join(heads, heads.c.game_id == teams.c.game_id)
            .where(heads.c.season == season)
            .order_by(teams.c.game_id, teams.c.is_home)
        ).mappings()
        for row in team_rows:
            stored[row["game_id"]][1].append(dict(row))
        return stored

    # --- read ---------------------------------------------------------------

    def read(self, game_id: str, *, connection: Connection | None = None) -> dict[str, Any]:
        """The Matchup's ``game_context`` block for ``game_id``: away team first, then home.

        ``game_line`` is the game's stored ESPN line (crf04/statsplus#493), served
        whether or not the team rows are stored.
        """

        heads = GameContext.__table__
        teams = GameContextTeam.__table__
        with read_connection(self.engine, connection) as connection:
            head = connection.execute(select(heads).where(heads.c.game_id == game_id)).mappings().first()
            rows = (
                connection.execute(select(teams).where(teams.c.game_id == game_id).order_by(teams.c.is_home))
                .mappings()
                .all()
            )
            game_line = read_game_line(connection, game_id)
        if head is None or len(rows) != 2:
            return {"status": "unavailable", "computed_at": None, "teams": [], "game_line": game_line}
        return {
            "status": "available",
            "computed_at": assume_utc(head["computed_at"]).isoformat(),
            "teams": [_serialize_team(row, head["season"]) for row in rows],
            "game_line": game_line,
        }


def _serialize_team(row: Mapping[str, Any], season: str) -> dict[str, Any]:
    return {
        "team_id": int(row["team_id"]),
        "tricode": row["tricode"],
        "is_home": bool(row["is_home"]),
        "season": season,
        "through_game_date": _iso(row["through_game_date"]),
        "rest": {
            "previous_game_date": _iso(row["previous_game_date"]),
            "rest_days": row["rest_days"],
            "back_to_back": bool(row["back_to_back"]),
        },
        "ratings": {
            window: {
                "games": int(row[f"{window}_games"]),
                "possession_games": int(row[f"{window}_possession_games"]),
                **{name: row[f"{window}_{name}"] for name in RATING_FIELDS},
            }
            for window in WINDOWS
        },
        "last_5": json.loads(row["last_5"]),
    }


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = ["GameContextRepository", "TeamGame", "ratings", "team_context"]
