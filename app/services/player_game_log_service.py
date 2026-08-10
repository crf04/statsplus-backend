"""Refresh-time canonicalization for durable player-game-log facts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, NoReturn, Protocol

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_events import (
    PLAYER_GAME_LOG_SEASON_TYPES,
    is_final_event,
    is_postponed_event,
    player_game_log_season_type,
)
from app.domain.utc import assume_utc, parse_utc_iso
from app.providers.nba_stats import NBAStatsProvider
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.player_game_log_repository import (
    PlayerGameLogFreshness,
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


class _ExclusionReason(Enum):
    ATHLETE = "athlete"
    EVENT = "event"
    TEAM = "team"


@dataclass(frozen=True, slots=True)
class _Joined:
    record: PlayerGameLogRecord


@dataclass(frozen=True, slots=True)
class _Excluded:
    reason: _ExclusionReason


_CanonicalRow = _Joined | _Excluded


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
    """Fetch both supported season phases and atomically replace stored facts."""

    def __init__(
        self,
        *,
        nba_stats_provider: NBAStatsProvider,
        athlete_catalog: AthleteCatalogReader,
        event_catalog: EventCatalogReader,
        repository: PlayerGameLogRepository,
        minimum_active_players_per_team_game: int,
        clock: Callable[[], datetime] | None = None,
        telemetry_recorder: PlayerGameLogTelemetryRecorder | None = None,
    ) -> None:
        self.provider = nba_stats_provider
        self.athlete_catalog = athlete_catalog
        self.event_catalog = event_catalog
        self.repository = repository
        if (
            isinstance(minimum_active_players_per_team_game, bool)
            or not isinstance(minimum_active_players_per_team_game, int)
            or minimum_active_players_per_team_game < 1
        ):
            raise ValueError(
                "minimum_active_players_per_team_game must be a positive integer"
            )
        self._minimum_active_players_per_team_game = (
            minimum_active_players_per_team_game
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.telemetry_recorder = (
            telemetry_recorder or BoundedPlayerGameLogTelemetryRecorder()
        )

    def refresh(
        self, season: str, *, now: datetime | None = None
    ) -> PlayerGameLogRefreshResult:
        canonical_season = validate_canonical_season(season)
        frames: dict[str, pd.DataFrame] = {}
        observed_source_rows = 0
        for season_type in PLAYER_GAME_LOG_SEASON_TYPES:
            frame = self.provider.get_season_player_game_logs(
                season=canonical_season,
                season_type=season_type,
            )
            if not isinstance(frame, pd.DataFrame):
                self._reject(
                    "player game logs must be a normalized provider data frame",
                    source=observed_source_rows,
                )
            frames[season_type] = frame
            observed_source_rows += len(frame.index)
        retrieved_at = assume_utc(now or self._clock())
        return self._refresh_dataframes(canonical_season, frames, retrieved_at)

    def _refresh_dataframes(
        self,
        canonical_season: str,
        frames: dict[str, pd.DataFrame],
        retrieved_at: datetime,
    ) -> PlayerGameLogRefreshResult:
        canonicalized: _CanonicalizationResult | None = None
        source_row_count = sum(len(frame.index) for frame in frames.values())
        try:
            events = self._require_complete_event_catalog(
                canonical_season, source_row_count, retrieved_at
            )
            completed_events = {
                season_type: self._completed_events(
                    events,
                    season_type=season_type,
                    observed_at=retrieved_at,
                )
                for season_type in PLAYER_GAME_LOG_SEASON_TYPES
            }
            if all(frame.empty for frame in frames.values()):
                return self._publish_empty(
                    canonical_season,
                    retrieved_at,
                    completed_events=tuple(
                        event
                        for phase_events in completed_events.values()
                        for event in phase_events
                    ),
                )
            for season_type, frame in frames.items():
                if frame.empty and completed_events[season_type]:
                    self._reject(
                        f"an empty {season_type} snapshot cannot represent completed games",
                        source=source_row_count,
                    )

            athletes = self._require_fresh_athlete_catalog(
                canonical_season, source_row_count, retrieved_at
            )
            phase_results: list[_CanonicalizationResult] = []
            try:
                for season_type, frame in frames.items():
                    if frame.empty:
                        continue
                    try:
                        phase_results.append(
                            self._canonicalize(
                                canonical_season,
                                frame,
                                season_type=season_type,
                                athletes=athletes,
                                events=events,
                            )
                        )
                    except _CanonicalizationAbort as error:
                        combined = self._combine_canonicalizations(
                            (*phase_results, error.result)
                        )
                        raise _CanonicalizationAbort(
                            str(error),
                            combined,
                            malformed_row_count=error.malformed_row_count,
                        ) from error
                canonicalized = self._combine_canonicalizations(
                    tuple(phase_results)
                )
            except _CanonicalizationAbort as error:
                self._emit_telemetry(
                    result=error.result,
                    malformed=error.malformed_row_count,
                    rejected=1,
                )
                raise
            if not canonicalized.records:
                self._reject(
                    "a non-empty player game log snapshot produced no canonical rows",
                    result=canonicalized,
                )
            for season_type, phase_events in completed_events.items():
                phase_records = tuple(
                    record
                    for record in canonicalized.records
                    if record.season_type == season_type
                )
                if not self._has_complete_game_coverage(
                    phase_records, phase_events
                ):
                    self._reject(
                        "player game logs do not provide complete completed game coverage",
                        result=canonicalized,
                    )
            prior = self.repository.get_freshness(canonical_season)
            if self._growth_hidden_by_unjoined_identity(canonicalized, prior):
                self._reject(
                    "cumulative player logs reveal incomplete canonical identity coverage",
                    result=canonicalized,
                )
            try:
                publication = self.repository.publish(
                    canonical_season,
                    canonicalized.records,
                    retrieved_at=retrieved_at,
                    source_provider=SOURCE_PROVIDER,
                    source_row_count=canonicalized.source_row_count,
                    current_catalog_game_ids=frozenset(
                        str(event["nba_game_id"]) for event in events
                    ),
                    recoverable_game_ids=frozenset(
                        str(event["nba_game_id"])
                        for event in events
                        if player_game_log_season_type(event) is None
                        or is_postponed_event(event)
                    ),
                )
            except ValueError:
                self._emit_telemetry(result=canonicalized, rejected=1)
                raise
            self._emit_telemetry(
                result=canonicalized,
                published=publication.row_count,
                recovered=publication.recovered_removed_row_count,
            )
            return PlayerGameLogRefreshResult(
                season=canonical_season,
                row_count=publication.row_count,
                retrieved_at=retrieved_at.isoformat(),
            )
        except SQLAlchemyError:
            self._emit_telemetry(
                source=source_row_count, result=canonicalized, rejected=1
            )
            raise

    @staticmethod
    def _growth_hidden_by_unjoined_identity(
        result: _CanonicalizationResult, prior: PlayerGameLogFreshness
    ) -> bool:
        has_unjoined_identity = bool(
            result.unjoined_athlete_count
            or result.unjoined_event_count
            or result.team_mismatch_count
        )
        return (
            has_unjoined_identity
            and prior.retrieved_at is not None
            and prior.row_count > 0
            and result.source_row_count > prior.source_row_count
            and len(result.records) <= prior.row_count
        )

    def _require_complete_event_catalog(
        self, season: str, source_row_count: int, retrieved_at: datetime
    ) -> list[dict[str, Any]]:
        events = self.event_catalog.get_events(season)
        freshness = self.event_catalog.get_freshness(season, now=retrieved_at)
        event_count = freshness.get("event_count")
        if not freshness.get("fresh") or not event_count or event_count != len(events):
            self._reject(
                "the Event Catalog must be present, fresh, and complete before publication",
                source=source_row_count,
            )
        return events

    def _require_fresh_athlete_catalog(
        self, season: str, source_row_count: int, retrieved_at: datetime
    ) -> list[dict[str, Any]]:
        freshness = self.athlete_catalog.get_freshness(
            season, now=retrieved_at
        )
        if not freshness.get("is_fresh") or not freshness.get("row_count"):
            self._reject(
                "the Athlete Catalog must be present and fresh before publication",
                source=source_row_count,
            )
        return self.athlete_catalog.get_catalog(season, active_only=False)

    def _publish_empty(
        self,
        season: str,
        retrieved_at: datetime,
        *,
        completed_events: tuple[dict[str, Any], ...],
    ) -> PlayerGameLogRefreshResult:
        if completed_events:
            self._reject(
                "an empty snapshot cannot represent a season with completed games"
            )
        try:
            publication = self.repository.publish(
                season,
                (),
                retrieved_at=retrieved_at,
                source_provider=SOURCE_PROVIDER,
                source_row_count=0,
                allow_empty=True,
            )
        except ValueError:
            self._emit_telemetry(rejected=1)
            raise
        self._emit_telemetry(published=publication.row_count)
        return PlayerGameLogRefreshResult(
            season=season,
            row_count=publication.row_count,
            retrieved_at=retrieved_at.isoformat(),
        )

    def _canonicalize(
        self,
        season: str,
        frame: pd.DataFrame,
        *,
        season_type: str,
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
                outcome = self._canonicalize_row(
                    season,
                    row,
                    season_type=season_type,
                    athlete_map=athlete_map,
                    event_map=event_map,
                )
            except PlayerGameLogIdentityError as error:
                raise _CanonicalizationAbort(
                    str(error),
                    observed_result(),
                    malformed_row_count=1,
                ) from error
            if isinstance(outcome, _Excluded):
                if outcome.reason is _ExclusionReason.ATHLETE:
                    unjoined_athlete_count += 1
                elif outcome.reason is _ExclusionReason.EVENT:
                    unjoined_event_count += 1
                else:
                    team_mismatch_count += 1
                continue
            record = outcome.record
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

    @staticmethod
    def _combine_canonicalizations(
        results: tuple[_CanonicalizationResult, ...],
    ) -> _CanonicalizationResult:
        records: dict[tuple[int, str], PlayerGameLogRecord] = {}
        duplicate_row_count = sum(result.duplicate_row_count for result in results)

        def combined_result() -> _CanonicalizationResult:
            return _CanonicalizationResult(
                records=tuple(records.values()),
                source_row_count=sum(result.source_row_count for result in results),
                unjoined_athlete_count=sum(
                    result.unjoined_athlete_count for result in results
                ),
                unjoined_event_count=sum(
                    result.unjoined_event_count for result in results
                ),
                team_mismatch_count=sum(
                    result.team_mismatch_count for result in results
                ),
                duplicate_row_count=duplicate_row_count,
            )

        for result in results:
            for record in result.records:
                key = (record.player_id, record.game_id)
                existing = records.get(key)
                if existing is not None and existing != record:
                    raise _CanonicalizationAbort(
                        "conflicting player game log facts share one identity",
                        combined_result(),
                    )
                records[key] = record
        return combined_result()

    @staticmethod
    def _completed_events(
        events: list[dict[str, Any]],
        *,
        season_type: str,
        observed_at: datetime,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            event
            for event in events
            if player_game_log_season_type(event) == season_type
            and is_final_event(event)
            and not is_postponed_event(event)
            and parse_utc_iso(str(event["scheduled_at"])) <= observed_at
        )

    def _has_complete_game_coverage(
        self,
        records: tuple[PlayerGameLogRecord, ...],
        completed_events: tuple[dict[str, Any], ...],
    ) -> bool:
        participants: dict[tuple[str, int], set[int]] = {}
        for record in records:
            if record.minutes > 0:
                participants.setdefault(
                    (record.game_id, record.team_id), set()
                ).add(record.player_id)
        return all(
            len(participants.get((str(event["nba_game_id"]), int(team_id)), set()))
            >= self._minimum_active_players_per_team_game
            for event in completed_events
            for team_id in (event["home_team_id"], event["away_team_id"])
        )

    def _canonicalize_row(
        self,
        season: str,
        row: dict[str, Any],
        *,
        season_type: str,
        athlete_map: dict[int, dict[str, Any]],
        event_map: dict[str, dict[str, Any]],
    ) -> _CanonicalRow:
        player_id = int(
            self._number(
                row["PLAYER_ID"], "player identity", integral=True, minimum=1
            )
        )
        athlete = athlete_map.get(player_id)
        if athlete is None:
            return _Excluded(_ExclusionReason.ATHLETE)
        player_name = str(athlete["display_name"])
        game_id = self._game_id(row["GAME_ID"])
        event = event_map.get(game_id)
        if event is None:
            return _Excluded(_ExclusionReason.EVENT)
        if player_game_log_season_type(event) != season_type:
            raise PlayerGameLogIdentityError(
                "a player game log phase does not match its governed event"
            )
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
            return _Excluded(_ExclusionReason.TEAM)

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
            or three_pointers_made > field_goals_made
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

        return _Joined(
            PlayerGameLogRecord(
                season=season,
                season_type=season_type,
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
            )
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

    def _reject(
        self,
        message: str,
        *,
        source: int = 0,
        result: _CanonicalizationResult | None = None,
        malformed: int = 0,
    ) -> NoReturn:
        self._emit_telemetry(
            source=source,
            result=result,
            malformed=malformed,
            rejected=1,
        )
        raise PlayerGameLogIdentityError(message)

    def _emit_telemetry(
        self,
        *,
        source: int = 0,
        result: _CanonicalizationResult | None = None,
        published: int = 0,
        malformed: int = 0,
        rejected: int = 0,
        recovered: int = 0,
    ) -> None:
        source_count = result.source_row_count if result is not None else source
        self.telemetry_recorder.record(
            PlayerGameLogTelemetryEvent(
                source_row_count=source_count,
                published_row_count=published,
                unjoined_athlete_count=(
                    result.unjoined_athlete_count if result is not None else 0
                ),
                unjoined_event_count=(
                    result.unjoined_event_count if result is not None else 0
                ),
                team_mismatch_count=(
                    result.team_mismatch_count if result is not None else 0
                ),
                malformed_row_count=malformed,
                rejected_publication_count=rejected,
                duplicate_row_count=(
                    result.duplicate_row_count if result is not None else 0
                ),
                recovered_shrink_row_count=recovered,
            )
        )


__all__ = [
    "PlayerGameLogIdentityError",
    "PlayerGameLogRefreshResult",
    "PlayerGameLogService",
]
