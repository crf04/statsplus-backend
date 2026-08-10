"""Refresh-time canonicalization for durable player-game-log facts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Protocol

import pandas as pd

from app.domain.utc import assume_utc
from app.domain.nba_events import is_final_event
from app.providers.nba_stats import DEFAULT_SEASON_TYPE, NBAStatsProvider
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)
from app.utils.telemetry import (
    BoundedPlayerGameLogTelemetryRecorder,
    PlayerGameLogTelemetryEvent,
    PlayerGameLogTelemetryRecorder,
)


SOURCE_PROVIDER = "nba_stats"


class PlayerGameLogIdentityError(ValueError):
    """A provider row could not join to canonical athlete or game identity."""


@dataclass(frozen=True, slots=True)
class PlayerGameLogRefreshResult:
    season: str
    row_count: int
    retrieved_at: str


@dataclass(frozen=True, slots=True)
class _CanonicalizationResult:
    records: tuple[PlayerGameLogRecord, ...]
    source_row_count: int
    unjoined_athlete_count: int
    unjoined_event_count: int
    team_mismatch_count: int


class AthleteCatalogReader(Protocol):
    """Owner read seam for canonical athletes."""

    def get_catalog(
        self, season: str, *, active_only: bool = False
    ) -> list[dict[str, Any]]: ...

    def get_freshness(
        self, season: str, *, now: datetime | None = None
    ) -> dict[str, Any]: ...


class EventCatalogReader(Protocol):
    """Owner read seam for canonical games."""

    def get_events(self, season: str) -> list[dict[str, Any]]: ...


class PlayerGameLogService:
    """Fetch a complete season once and atomically replace its stored facts."""

    def __init__(
        self,
        *,
        nba_stats_provider: NBAStatsProvider,
        athlete_catalog: AthleteCatalogReader,
        event_catalog: EventCatalogReader,
        repository: PlayerGameLogRepository,
        clock: Callable[[], datetime] | None = None,
        telemetry_recorder: PlayerGameLogTelemetryRecorder | None = None,
    ) -> None:
        self.provider = nba_stats_provider
        self.athlete_catalog = athlete_catalog
        self.event_catalog = event_catalog
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.telemetry_recorder = (
            telemetry_recorder or BoundedPlayerGameLogTelemetryRecorder()
        )

    def refresh(
        self, season: str, *, now: datetime | None = None
    ) -> PlayerGameLogRefreshResult:
        canonical_season = validate_canonical_season(season)
        frame = self.provider.get_season_player_game_logs(
            season=canonical_season,
            season_type=DEFAULT_SEASON_TYPE,
        )
        retrieved_at = assume_utc(now or self._clock())
        if not isinstance(frame, pd.DataFrame):
            raise PlayerGameLogIdentityError(
                "player game logs must be a normalized provider data frame"
            )

        events = self.event_catalog.get_events(canonical_season)
        if frame.empty:
            if not events:
                raise PlayerGameLogIdentityError(
                    "an empty snapshot has no event catalog evidence"
                )
            if any(is_final_event(event) for event in events):
                raise PlayerGameLogIdentityError(
                    "an empty snapshot cannot represent a season with completed games"
                )
            row_count = self.repository.publish(
                canonical_season,
                (),
                retrieved_at=retrieved_at,
                source_provider=SOURCE_PROVIDER,
                allow_empty=True,
            )
            self._record_telemetry(0, row_count, 0, 0, 0)
            return PlayerGameLogRefreshResult(
                season=canonical_season,
                row_count=row_count,
                retrieved_at=retrieved_at.isoformat(),
            )

        athlete_freshness = self.athlete_catalog.get_freshness(
            canonical_season, now=retrieved_at
        )
        if not athlete_freshness.get("is_fresh") or not athlete_freshness.get(
            "row_count"
        ):
            raise PlayerGameLogIdentityError(
                "the Athlete Catalog must be present and fresh before publication"
            )
        athletes = self.athlete_catalog.get_catalog(
            canonical_season, active_only=False
        )
        try:
            canonicalized = self._canonicalize(
                canonical_season,
                frame,
                athletes=athletes,
                events=events,
            )
        except PlayerGameLogIdentityError:
            self._record_telemetry(len(frame.index), 0, 0, 0, 0, malformed=1)
            raise
        if not canonicalized.records:
            self._record_canonicalization_telemetry(canonicalized, published=0)
            raise PlayerGameLogIdentityError(
                "a non-empty player game log snapshot produced no canonical rows"
            )
        row_count = self.repository.publish(
            canonical_season,
            canonicalized.records,
            retrieved_at=retrieved_at,
            source_provider=SOURCE_PROVIDER,
        )
        self._record_canonicalization_telemetry(canonicalized, published=row_count)
        return PlayerGameLogRefreshResult(
            season=canonical_season,
            row_count=row_count,
            retrieved_at=retrieved_at.isoformat(),
        )

    def _canonicalize(
        self,
        season: str,
        frame: pd.DataFrame,
        *,
        athletes: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> _CanonicalizationResult:
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
        athlete_map = {
            int(athlete["player_id"]): athlete
            for athlete in athletes
            if athlete.get("player_id") is not None
        }
        event_map = {
            str(event["nba_game_id"]): event
            for event in events
            if event.get("nba_game_id") is not None
        }

        records = []
        unjoined_athlete_count = 0
        unjoined_event_count = 0
        team_mismatch_count = 0
        for row in rows:
            player_id = int(
                self._number(
                    row["PLAYER_ID"], "player identity", integral=True, minimum=1
                )
            )
            athlete = athlete_map.get(player_id)
            if athlete is None:
                unjoined_athlete_count += 1
                continue
            game_id = self._game_id(row["GAME_ID"])
            event = event_map.get(game_id)
            if event is None:
                unjoined_event_count += 1
                continue
            team_id = int(
                self._number(
                    row["TEAM_ID"], "team identity", integral=True, minimum=1
                )
            )
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
                team_mismatch_count += 1
                continue

            field_goals_made = int(
                self._number(row["FGM"], "field goals made", integral=True)
            )
            field_goals_attempted = int(
                self._number(row["FGA"], "field goals attempted", integral=True)
            )
            three_pointers_made = int(
                self._number(row["FG3M"], "three-pointers made", integral=True)
            )
            three_pointers_attempted = int(
                self._number(
                    row["FG3A"], "three-pointers attempted", integral=True
                )
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
                    minutes=self._number(row["MIN"], "minutes"),
                    points=int(self._number(row["PTS"], "points", integral=True)),
                    rebounds=int(
                        self._number(row["REB"], "rebounds", integral=True)
                    ),
                    assists=int(
                        self._number(row["AST"], "assists", integral=True)
                    ),
                    field_goals_made=field_goals_made,
                    field_goals_attempted=field_goals_attempted,
                    three_pointers_made=three_pointers_made,
                    three_pointers_attempted=three_pointers_attempted,
                    turnovers=int(
                        self._number(row["TOV"], "turnovers", integral=True)
                    ),
                    steals=int(self._number(row["STL"], "steals", integral=True)),
                    blocks=int(self._number(row["BLK"], "blocks", integral=True)),
                )
            )
        return _CanonicalizationResult(
            records=tuple(records),
            source_row_count=len(rows),
            unjoined_athlete_count=unjoined_athlete_count,
            unjoined_event_count=unjoined_event_count,
            team_mismatch_count=team_mismatch_count,
        )

    @staticmethod
    def _game_id(value: Any) -> str:
        game_id = str(value).strip()
        if not game_id:
            raise PlayerGameLogIdentityError(
                "a player game log has no canonical game identity"
            )
        return game_id

    @staticmethod
    def _number(
        value: Any,
        field: str,
        *,
        integral: bool = False,
        minimum: float = 0,
    ) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise PlayerGameLogIdentityError(
                f"a player game log has an invalid {field}"
            ) from error
        if (
            not math.isfinite(numeric)
            or numeric < minimum
            or (integral and not numeric.is_integer())
        ):
            raise PlayerGameLogIdentityError(
                f"a player game log has invalid {field}"
            )
        return numeric

    def _record_canonicalization_telemetry(
        self, result: _CanonicalizationResult, *, published: int
    ) -> None:
        self._record_telemetry(
            result.source_row_count,
            published,
            result.unjoined_athlete_count,
            result.unjoined_event_count,
            result.team_mismatch_count,
        )

    def _record_telemetry(
        self,
        source: int,
        published: int,
        unjoined_athletes: int,
        unjoined_events: int,
        team_mismatches: int,
        *,
        malformed: int = 0,
    ) -> None:
        self.telemetry_recorder.record(
            PlayerGameLogTelemetryEvent(
                source_row_count=source,
                published_row_count=published,
                unjoined_athlete_count=unjoined_athletes,
                unjoined_event_count=unjoined_events,
                team_mismatch_count=team_mismatches,
                malformed_row_count=malformed,
            )
        )


__all__ = [
    "PlayerGameLogIdentityError",
    "PlayerGameLogRefreshResult",
    "PlayerGameLogService",
]
