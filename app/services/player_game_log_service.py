"""Refresh-time canonicalization for durable player-game-log facts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.athlete_catalog import AthleteCatalog
from app.models.event_catalog import EventCatalogEntry
from app.providers.nba_stats import NBAStatsProvider
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)


SOURCE_PROVIDER = "nba_stats"


class PlayerGameLogIdentityError(ValueError):
    """A provider row could not join to canonical athlete or game identity."""


@dataclass(frozen=True, slots=True)
class PlayerGameLogRefreshResult:
    season: str
    row_count: int
    retrieved_at: str


class PlayerGameLogService:
    """Fetch a complete season once and atomically replace its stored facts."""

    def __init__(
        self,
        engine: Engine,
        *,
        nba_stats_provider: NBAStatsProvider,
        repository: PlayerGameLogRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.provider = nba_stats_provider
        self.repository = repository or PlayerGameLogRepository(engine)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(
        self, season: str, *, now: datetime | None = None
    ) -> PlayerGameLogRefreshResult:
        canonical_season = validate_canonical_season(season)
        retrieved_at = assume_utc(now or self._clock())
        frame = self.provider.get_season_player_game_logs(
            season=canonical_season,
            season_type="Regular Season",
        )
        records = self._canonicalize(canonical_season, frame)
        row_count = self.repository.publish(
            canonical_season,
            records,
            retrieved_at=retrieved_at,
            source_provider=SOURCE_PROVIDER,
        )
        return PlayerGameLogRefreshResult(
            season=canonical_season,
            row_count=row_count,
            retrieved_at=retrieved_at.isoformat(),
        )

    def _canonicalize(
        self, season: str, frame: pd.DataFrame
    ) -> tuple[PlayerGameLogRecord, ...]:
        if not isinstance(frame, pd.DataFrame):
            raise PlayerGameLogIdentityError(
                "player game logs must be a normalized provider data frame"
            )
        if frame.empty:
            return ()

        required = {
            "PLAYER_ID",
            "GAME_ID",
            "GAME_DATE",
            "TEAM_ID",
            "MIN",
            "PTS",
            "REB",
            "AST",
            "FGM",
            "FGA",
            "FG3M",
            "FG3A",
            "TOV",
            "STL",
            "BLK",
        }
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise PlayerGameLogIdentityError(
                "player game logs are missing required canonical facts"
            )

        rows = frame.to_dict(orient="records")
        player_ids = {self._whole(row["PLAYER_ID"], "player identity") for row in rows}
        game_ids = {self._game_id(row["GAME_ID"]) for row in rows}
        athletes, events = self._identity_maps(season, player_ids, game_ids)

        records = []
        for row in rows:
            player_id = self._whole(row["PLAYER_ID"], "player identity")
            athlete = athletes.get(player_id)
            if athlete is None:
                raise PlayerGameLogIdentityError(
                    "a player game log has no canonical athlete identity"
                )
            game_id = self._game_id(row["GAME_ID"])
            event = events.get(game_id)
            if event is None:
                raise PlayerGameLogIdentityError(
                    "a player game log has no canonical game identity"
                )
            team_id = self._whole(row["TEAM_ID"], "team identity")
            if team_id == event["home_team_id"]:
                opponent_team_id = event["away_team_id"]
                team_tricode = event["home_team_tricode"]
                opponent_team_tricode = event["away_team_tricode"]
                is_home = True
            elif team_id == event["away_team_id"]:
                opponent_team_id = event["home_team_id"]
                team_tricode = event["away_team_tricode"]
                opponent_team_tricode = event["home_team_tricode"]
                is_home = False
            else:
                raise PlayerGameLogIdentityError(
                    "a player game log team does not belong to its canonical game"
                )

            field_goals_made = self._count(row["FGM"], "field goals made")
            field_goals_attempted = self._count(row["FGA"], "field goals attempted")
            three_pointers_made = self._count(row["FG3M"], "three-pointers made")
            three_pointers_attempted = self._count(
                row["FG3A"], "three-pointers attempted"
            )
            if (
                field_goals_made > field_goals_attempted
                or three_pointers_made > three_pointers_attempted
                or three_pointers_attempted > field_goals_attempted
            ):
                raise PlayerGameLogIdentityError(
                    "a player game log contains inconsistent shooting facts"
                )
            try:
                game_date = pd.Timestamp(row["GAME_DATE"]).date()
            except (TypeError, ValueError, OverflowError) as error:
                raise PlayerGameLogIdentityError(
                    "a player game log has an invalid game date"
                ) from error

            records.append(
                PlayerGameLogRecord(
                    season=season,
                    player_id=player_id,
                    game_id=game_id,
                    player_name=str(athlete["display_name"]),
                    game_date=game_date,
                    team_id=team_id,
                    team_tricode=str(team_tricode),
                    opponent_team_id=int(opponent_team_id),
                    opponent_team_tricode=str(opponent_team_tricode),
                    is_home=is_home,
                    minutes=self._minutes(row["MIN"]),
                    points=self._count(row["PTS"], "points"),
                    rebounds=self._count(row["REB"], "rebounds"),
                    assists=self._count(row["AST"], "assists"),
                    field_goals_made=field_goals_made,
                    field_goals_attempted=field_goals_attempted,
                    three_pointers_made=three_pointers_made,
                    three_pointers_attempted=three_pointers_attempted,
                    turnovers=self._count(row["TOV"], "turnovers"),
                    steals=self._count(row["STL"], "steals"),
                    blocks=self._count(row["BLK"], "blocks"),
                )
            )
        return tuple(records)

    def _identity_maps(
        self, season: str, player_ids: set[int], game_ids: set[str]
    ) -> tuple[dict[int, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
        athlete_table = AthleteCatalog.__table__
        event_table = EventCatalogEntry.__table__
        with self.engine.connect() as connection:
            athlete_rows = connection.execute(
                select(athlete_table).where(
                    athlete_table.c.season == season,
                    athlete_table.c.player_id.in_(player_ids),
                )
            ).mappings()
            athletes = {int(row["player_id"]): row for row in athlete_rows}
            event_rows = connection.execute(
                select(event_table).where(
                    event_table.c.season == season,
                    event_table.c.nba_game_id.in_(game_ids),
                )
            ).mappings()
            events = {str(row["nba_game_id"]): row for row in event_rows}
        return athletes, events

    @staticmethod
    def _game_id(value: Any) -> str:
        game_id = str(value).strip()
        if not game_id:
            raise PlayerGameLogIdentityError(
                "a player game log has no canonical game identity"
            )
        return game_id

    @staticmethod
    def _whole(value: Any, field: str) -> int:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise PlayerGameLogIdentityError(
                f"a player game log has an invalid {field}"
            ) from error
        if not math.isfinite(numeric) or not numeric.is_integer() or numeric <= 0:
            raise PlayerGameLogIdentityError(
                f"a player game log has an invalid {field}"
            )
        return int(numeric)

    @classmethod
    def _count(cls, value: Any, field: str) -> int:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise PlayerGameLogIdentityError(
                f"a player game log has invalid {field}"
            ) from error
        if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 0:
            raise PlayerGameLogIdentityError(
                f"a player game log has invalid {field}"
            )
        return int(numeric)

    @staticmethod
    def _minutes(value: Any) -> float:
        try:
            minutes = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise PlayerGameLogIdentityError(
                "a player game log has invalid minutes"
            ) from error
        if not math.isfinite(minutes) or minutes < 0:
            raise PlayerGameLogIdentityError(
                "a player game log has invalid minutes"
            )
        return minutes


__all__ = [
    "PlayerGameLogIdentityError",
    "PlayerGameLogRefreshResult",
    "PlayerGameLogService",
]
