"""Refresh-time canonicalization for durable player-game-log facts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Protocol

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_events import (
    is_final_event,
    is_postponed_event,
    is_regular_season_event,
)
from app.domain.utc import assume_utc
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
    duplicate_row_count: int = 0


class _CanonicalizationAbort(PlayerGameLogIdentityError):
    def __init__(
        self,
        message: str,
        result: _CanonicalizationResult,
        *,
        malformed_row_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.malformed_row_count = malformed_row_count


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

    def get_freshness(
        self, season: str, *, now: datetime | None = None
    ) -> dict[str, Any]: ...


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
            self._record_telemetry(0, 0, 0, 0, 0, rejected=1)
            raise PlayerGameLogIdentityError(
                "player game logs must be a normalized provider data frame"
            )
        return self._refresh_dataframe(canonical_season, frame, retrieved_at)

    def _refresh_dataframe(
        self, canonical_season: str, frame: pd.DataFrame, retrieved_at: datetime
    ) -> PlayerGameLogRefreshResult:
        canonicalized: _CanonicalizationResult | None = None
        try:
            events = self.event_catalog.get_events(canonical_season)
            event_freshness = self.event_catalog.get_freshness(
                canonical_season, now=retrieved_at
            )
            event_count = event_freshness.get("event_count")
            if (
                not event_freshness.get("fresh")
                or not event_count
                or event_count != len(events)
            ):
                self._record_telemetry(len(frame.index), 0, 0, 0, 0, rejected=1)
                raise PlayerGameLogIdentityError(
                    "the Event Catalog must be present, fresh, and complete before publication"
                )
            if frame.empty:
                if any(
                    is_regular_season_event(event)
                    and is_final_event(event)
                    and not is_postponed_event(event)
                    for event in events
                ):
                    self._record_telemetry(0, 0, 0, 0, 0, rejected=1)
                    raise PlayerGameLogIdentityError(
                        "an empty snapshot cannot represent a season with completed games"
                    )
                try:
                    row_count = self.repository.publish(
                        canonical_season,
                        (),
                        retrieved_at=retrieved_at,
                        source_provider=SOURCE_PROVIDER,
                        allow_empty=True,
                    )
                except ValueError:
                    self._record_telemetry(0, 0, 0, 0, 0, rejected=1)
                    raise
                self._record_telemetry(0, row_count, 0, 0, 0)
                return PlayerGameLogRefreshResult(
                    season=canonical_season,
                    row_count=row_count,
                    retrieved_at=retrieved_at.isoformat(),
                )

            athlete_freshness = self.athlete_catalog.get_freshness(
                canonical_season, now=retrieved_at
            )
            if not athlete_freshness.get(
                "is_fresh"
            ) or not athlete_freshness.get("row_count"):
                self._record_telemetry(len(frame.index), 0, 0, 0, 0, rejected=1)
                raise PlayerGameLogIdentityError(
                    "the Athlete Catalog must be present and fresh before publication"
                )
            athletes = self.athlete_catalog.get_catalog(
                canonical_season, active_only=False
            )
            published_player_identities = (
                self.repository.get_published_player_identities(
                    canonical_season, source_provider=SOURCE_PROVIDER
                )
            )
            try:
                canonicalized = self._canonicalize(
                    canonical_season,
                    frame,
                    athletes=athletes,
                    events=events,
                    published_player_identities=published_player_identities,
                )
            except _CanonicalizationAbort as error:
                self._record_canonicalization_telemetry(
                    error.result,
                    published=0,
                    malformed=error.malformed_row_count,
                    rejected=1,
                )
                raise
            if not canonicalized.records:
                self._record_canonicalization_telemetry(
                    canonicalized, published=0, rejected=1
                )
                raise PlayerGameLogIdentityError(
                    "a non-empty player game log snapshot produced no canonical rows"
                )
            prior = self.repository.get_freshness(canonical_season)
            if (
                canonicalized.unjoined_event_count
                and prior.retrieved_at is not None
                and prior.row_count > 0
                and canonicalized.source_row_count > prior.row_count
                and len(canonicalized.records) <= prior.row_count
            ):
                self._record_canonicalization_telemetry(
                    canonicalized, published=0, rejected=1
                )
                raise PlayerGameLogIdentityError(
                    "cumulative player logs reveal an incomplete Event Catalog"
                )
            try:
                row_count = self.repository.publish(
                    canonical_season,
                    canonicalized.records,
                    retrieved_at=retrieved_at,
                    source_provider=SOURCE_PROVIDER,
                )
            except ValueError:
                self._record_canonicalization_telemetry(
                    canonicalized, published=0, rejected=1
                )
                raise
            self._record_canonicalization_telemetry(
                canonicalized, published=row_count
            )
            return PlayerGameLogRefreshResult(
                season=canonical_season,
                row_count=row_count,
                retrieved_at=retrieved_at.isoformat(),
            )
        except SQLAlchemyError:
            if canonicalized is None:
                self._record_telemetry(len(frame.index), 0, 0, 0, 0, rejected=1)
            else:
                self._record_canonicalization_telemetry(
                    canonicalized, published=0, rejected=1
                )
            raise

    def _canonicalize(
        self,
        season: str,
        frame: pd.DataFrame,
        *,
        athletes: list[dict[str, Any]],
        events: list[dict[str, Any]],
        published_player_identities: dict[int, str],
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
            raise _CanonicalizationAbort(
                "player game logs are missing required canonical facts",
                _CanonicalizationResult((), len(frame.index), 0, 0, 0),
                malformed_row_count=1,
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

        records: dict[tuple[int, str], PlayerGameLogRecord] = {}
        unjoined_athlete_count = 0
        unjoined_event_count = 0
        team_mismatch_count = 0
        duplicate_row_count = 0

        def observed_result() -> _CanonicalizationResult:
            return _CanonicalizationResult(
                records=tuple(records.values()),
                source_row_count=len(rows),
                unjoined_athlete_count=unjoined_athlete_count,
                unjoined_event_count=unjoined_event_count,
                team_mismatch_count=team_mismatch_count,
                duplicate_row_count=duplicate_row_count,
            )

        for row in rows:
            try:
                record, exclusion = self._canonicalize_row(
                    season,
                    row,
                    athlete_map=athlete_map,
                    event_map=event_map,
                    published_player_identities=published_player_identities,
                )
            except PlayerGameLogIdentityError as error:
                raise _CanonicalizationAbort(
                    str(error),
                    observed_result(),
                    malformed_row_count=1,
                ) from error
            if exclusion == "athlete":
                unjoined_athlete_count += 1
                continue
            if exclusion == "event":
                unjoined_event_count += 1
                continue
            if exclusion == "team":
                team_mismatch_count += 1
                continue
            assert record is not None
            key = (record.player_id, record.game_id)
            existing = records.get(key)
            if existing is not None:
                if existing == record:
                    duplicate_row_count += 1
                    continue
                raise _CanonicalizationAbort(
                    "conflicting player game log facts share one identity",
                    observed_result(),
                )
            records[key] = record

        return observed_result()

    def _canonicalize_row(
        self,
        season: str,
        row: dict[str, Any],
        *,
        athlete_map: dict[int, dict[str, Any]],
        event_map: dict[str, dict[str, Any]],
        published_player_identities: dict[int, str],
    ) -> tuple[PlayerGameLogRecord | None, str | None]:
        player_id = int(
            self._number(
                row["PLAYER_ID"], "player identity", integral=True, minimum=1
            )
        )
        athlete = athlete_map.get(player_id)
        player_name = (
            str(athlete["display_name"])
            if athlete is not None
            else published_player_identities.get(player_id)
        )
        if player_name is None:
            return None, "athlete"
        game_id = self._game_id(row["GAME_ID"])
        event = event_map.get(game_id)
        if event is None:
            return None, "event"
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
            return None, "team"

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

        return (
            PlayerGameLogRecord(
                season=season,
                player_id=player_id,
                game_id=game_id,
                player_name=player_name,
                game_date=game_date,
                team_id=team_id,
                team_tricode=str(team_tricode),
                opponent_team_id=int(opponent_team_id),
                opponent_team_tricode=str(opponent_team_tricode),
                is_home=is_home,
                minutes=self._number(row["MIN"], "minutes"),
                points=int(self._number(row["PTS"], "points", integral=True)),
                rebounds=int(self._number(row["REB"], "rebounds", integral=True)),
                assists=int(self._number(row["AST"], "assists", integral=True)),
                field_goals_made=field_goals_made,
                field_goals_attempted=field_goals_attempted,
                three_pointers_made=three_pointers_made,
                three_pointers_attempted=three_pointers_attempted,
                turnovers=int(
                    self._number(row["TOV"], "turnovers", integral=True)
                ),
                steals=int(self._number(row["STL"], "steals", integral=True)),
                blocks=int(self._number(row["BLK"], "blocks", integral=True)),
            ),
            None,
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
        self,
        result: _CanonicalizationResult,
        *,
        published: int,
        malformed: int = 0,
        rejected: int = 0,
    ) -> None:
        self._record_telemetry(
            result.source_row_count,
            published,
            result.unjoined_athlete_count,
            result.unjoined_event_count,
            result.team_mismatch_count,
            malformed=malformed,
            rejected=rejected,
            duplicates=result.duplicate_row_count,
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
        rejected: int = 0,
        duplicates: int = 0,
    ) -> None:
        self.telemetry_recorder.record(
            PlayerGameLogTelemetryEvent(
                source_row_count=source,
                published_row_count=published,
                unjoined_athlete_count=unjoined_athletes,
                unjoined_event_count=unjoined_events,
                team_mismatch_count=team_mismatches,
                malformed_row_count=malformed,
                rejected_publication_count=rejected,
                duplicate_row_count=duplicates,
            )
        )


__all__ = [
    "PlayerGameLogIdentityError",
    "PlayerGameLogRefreshResult",
    "PlayerGameLogService",
]
