"""Incremental durable player-game-log ingestion from PBP Stats.

This is the Stage 2 refresh seam of the PBP game-log migration: one PBP
per-game player request for each missing completed game, bounded recent-game
reconciliation for stat corrections, and atomic per-game replacement through
the repository.  The old season-wide NBA path remains available as a legacy
backfill seam; Nightly Refresh uses this service so game logs are populated
from an egress that works on hosted infrastructure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn, Protocol

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_events import (
    is_final_event,
    is_postponed_event,
    player_game_log_season_type,
)
from app.domain.utc import assume_utc, parse_utc_iso
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.pbp_game_log_normalization import normalize_pbp_game_logs
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRefreshChange,
    PlayerGameLogRepository,
)
from app.utils.telemetry import (
    BoundedPlayerGameLogTelemetryRecorder,
    PlayerGameLogTelemetryEvent,
    PlayerGameLogTelemetryRecorder,
)

SOURCE_PROVIDER = "pbp_stats"


class PlayerGameLogIngestError(ValueError):
    """A game could not be ingested without losing canonical evidence."""


@dataclass(frozen=True, slots=True)
class PlayerGameLogIngestResult:
    season: str
    games_processed: int
    row_count: int
    retrieved_at: str


@dataclass(frozen=True, slots=True)
class _AggregatedCounts:
    source_row_count: int = 0
    unjoined_athlete_count: int = 0
    unjoined_event_count: int = 0
    team_mismatch_count: int = 0
    unsupported_phase_count: int = 0
    malformed_row_count: int = 0
    duplicate_row_count: int = 0


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


class PlayerGameLogIngestService:
    """Ingest completed games incrementally through one PBP call per game."""

    def __init__(
        self,
        *,
        pbp_provider: Any,
        athlete_catalog: AthleteCatalogReader,
        event_catalog: EventCatalogReader,
        repository: PlayerGameLogRepository,
        minimum_active_players_per_team_game: int,
        reconciliation_days: int,
        clock: Callable[[], datetime] | None = None,
        telemetry_recorder: PlayerGameLogTelemetryRecorder | None = None,
    ) -> None:
        self.pbp_provider = pbp_provider
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
        if (
            isinstance(reconciliation_days, bool)
            or not isinstance(reconciliation_days, int)
            or reconciliation_days < 0
        ):
            raise ValueError("reconciliation_days must be a non-negative integer")
        self._reconciliation_days = reconciliation_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.telemetry_recorder = (
            telemetry_recorder or BoundedPlayerGameLogTelemetryRecorder()
        )

    def refresh(
        self, season: str, *, now: datetime | None = None
    ) -> PlayerGameLogIngestResult:
        """Stage and atomically publish one run's missing/reconciled games."""
        canonical_season = validate_canonical_season(season)
        retrieved_at = assume_utc(now or self._clock())
        events = self._require_complete_event_catalog(canonical_season, retrieved_at)
        athletes = self._require_fresh_athlete_catalog(canonical_season, retrieved_at)
        completed = self._completed_events(events, observed_at=retrieved_at)
        completed_ids = frozenset(
            str(event["nba_game_id"]) for event in completed
        )
        if not completed:
            return self._publish_empty(canonical_season, retrieved_at)
        stored_ids = self.repository.stored_game_ids(canonical_season)
        targets = self._target_games(
            completed, stored_ids, observed_at=retrieved_at
        )
        athlete_map = {
            int(athlete["player_id"]): athlete
            for athlete in athletes
            if athlete.get("player_id") is not None
        }
        counts = _AggregatedCounts()
        changes: list[PlayerGameLogRefreshChange] = []
        games_processed = 0
        for event in targets:
            try:
                change = self._stage_game(
                    canonical_season,
                    event,
                    events=events,
                    athlete_map=athlete_map,
                )
            except SQLAlchemyError:
                self._emit_telemetry(counts=counts, rejected=1)
                raise
            if change is None:
                continue
            games_processed += 1
            changes.append(
                PlayerGameLogRefreshChange(
                    game_id=change.game_id,
                    season_type=change.season_type,
                    records=change.records,
                    checksum=change.checksum,
                )
            )
            counts = self._merge_counts(counts, change)
        # Every target game was fetched, normalized, and validated before this
        # point, so a single transaction replaces the changed games' rows,
        # upserts sync evidence, recomputes the season sidecar, and advances
        # the configured current season's stats-surface observation.  A game
        # that fails earlier preserves the exact prior fact rows and the last
        # complete publication.
        publication = self.repository.publish_refresh(
            canonical_season,
            changes,
            retrieved_at=retrieved_at,
            source_provider=SOURCE_PROVIDER,
            expected_complete_game_ids=completed_ids,
        )
        self._emit_telemetry(counts=counts, published=publication.row_count)
        return PlayerGameLogIngestResult(
            season=canonical_season,
            games_processed=games_processed,
            row_count=publication.row_count,
            retrieved_at=retrieved_at.isoformat(),
        )

    def _stage_game(
        self,
        season: str,
        event: dict[str, Any],
        *,
        events: list[dict[str, Any]],
        athlete_map: dict[int, dict[str, Any]],
    ) -> _GameChange | None:
        game_id = str(event["nba_game_id"])
        season_type = player_game_log_season_type(event)
        if season_type is None:
            return None
        observations = self.pbp_provider.fetch_game_player_logs(
            game_id,
            season,
            season_type=season_type,
        )
        frame, join_counts = normalize_pbp_game_logs(
            observations,
            events,
            season_type=season_type,
            round_minutes=False,
        )
        counts = _AggregatedCounts(
            source_row_count=join_counts.source_row_count,
            unjoined_event_count=join_counts.unjoined_event_count,
            team_mismatch_count=join_counts.team_mismatch_count,
            unsupported_phase_count=join_counts.unsupported_phase_count,
        )
        if frame.empty:
            self._reject(
                "PBP Stats returned no player facts for a completed game",
                counts=counts,
            )
        records, duplicates = self._canonicalize_game(
            season,
            frame,
            season_type=season_type,
            athlete_map=athlete_map,
            event=event,
        )
        counts = _AggregatedCounts(
            source_row_count=join_counts.source_row_count,
            unjoined_event_count=join_counts.unjoined_event_count,
            team_mismatch_count=join_counts.team_mismatch_count,
            unsupported_phase_count=join_counts.unsupported_phase_count,
            duplicate_row_count=duplicates,
        )
        self._validate_game_coverage(records, event, counts=counts)
        checksum = self.repository.game_checksum(records)
        sync = self.repository.get_sync_status(season, game_id)
        if (
            sync is not None
            and sync.status == "complete"
            and sync.checksum == checksum
        ):
            return None
        return _GameChange(
            game_id=game_id,
            season_type=season_type,
            records=records,
            checksum=checksum,
            counts=counts,
        )

    def _canonicalize_game(
        self,
        season: str,
        frame: pd.DataFrame,
        *,
        season_type: str,
        athlete_map: dict[int, dict[str, Any]],
        event: dict[str, Any],
    ) -> tuple[tuple[PlayerGameLogRecord, ...], int]:
        records: dict[int, PlayerGameLogRecord] = {}
        duplicate_row_count = 0
        for row in frame.to_dict(orient="records"):
            player_id = int(row["PLAYER_ID"])
            athlete = athlete_map.get(player_id)
            if athlete is None:
                # Identity evidence is never dropped: an athlete PBP reports
                # that the governed catalog cannot place fails the whole game
                # publication rather than publishing a partial box score.
                raise PlayerGameLogIngestError(
                    "a player game log cannot join the governed Athlete Catalog"
                )
            team_id = int(row["TEAM_ID"])
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
                raise PlayerGameLogIngestError(
                    "a player game log has a contradictory team identity"
                )
            try:
                game_date = pd.Timestamp(row["GAME_DATE"]).date()
            except (TypeError, ValueError, OverflowError) as error:
                raise PlayerGameLogIngestError(
                    "a player game log has an invalid game date"
                ) from error
            record = PlayerGameLogRecord(
                season=season,
                season_type=season_type,
                player_id=player_id,
                game_id=str(event["nba_game_id"]),
                player_name=str(athlete["display_name"]),
                game_date=game_date,
                team_id=team_id,
                team_tricode=str(team_tricode),
                opponent_team_id=int(opponent_team_id),
                opponent_team_tricode=str(opponent_team_tricode),
                is_home=is_home,
                minutes=float(row["MIN"]),
                points=int(row["PTS"]),
                rebounds=int(row["REB"]),
                assists=int(row["AST"]),
                field_goals_made=int(row["FGM"]),
                field_goals_attempted=int(row["FGA"]),
                three_pointers_made=int(row["FG3M"]),
                three_pointers_attempted=int(row["FG3A"]),
                free_throws_made=int(row["FTM"]),
                free_throws_attempted=int(row["FTA"]),
                offensive_rebounds=int(row["OREB"]),
                defensive_rebounds=int(row["DREB"]),
                turnovers=int(row["TOV"]),
                steals=int(row["STL"]),
                blocks=int(row["BLK"]),
                personal_fouls=int(row["PF"]),
            )
            existing = records.get(player_id)
            if existing is not None:
                if existing == record:
                    duplicate_row_count += 1
                    continue
                raise PlayerGameLogIngestError(
                    "conflicting player game log facts share one identity"
                )
            records[player_id] = record
        if not records:
            raise PlayerGameLogIngestError(
                "a completed game produced no canonical player facts"
            )
        return tuple(records.values()), duplicate_row_count

    def _validate_game_coverage(
        self,
        records: tuple[PlayerGameLogRecord, ...],
        event: dict[str, Any],
        *,
        counts: _AggregatedCounts,
    ) -> None:
        participants: dict[int, set[int]] = {}
        for record in records:
            if record.minutes > 0:
                participants.setdefault(record.team_id, set()).add(record.player_id)
        for team_id in (event["home_team_id"], event["away_team_id"]):
            if len(participants.get(team_id, set())) < (
                self._minimum_active_players_per_team_game
            ):
                self._reject(
                    "player game logs do not cover both teams of a completed game",
                    counts=counts,
                )

    def _completed_events(
        self,
        events: list[dict[str, Any]],
        *,
        observed_at: datetime,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            event
            for event in events
            if player_game_log_season_type(event) is not None
            and is_final_event(event)
            and not is_postponed_event(event)
            and parse_utc_iso(str(event["scheduled_at"])) <= observed_at
        )

    def _target_games(
        self,
        completed: tuple[dict[str, Any], ...],
        stored_ids: frozenset[str],
        *,
        observed_at: datetime,
    ) -> tuple[dict[str, Any], ...]:
        cutoff = (observed_at - timedelta(days=self._reconciliation_days)).date()
        ordered = sorted(
            completed,
            key=lambda event: (
                str(event["scheduled_at"]),
                str(event["nba_game_id"]),
            ),
        )
        targets = []
        for event in ordered:
            game_id = str(event["nba_game_id"])
            if game_id not in stored_ids:
                targets.append(event)
                continue
            scheduled_date = parse_utc_iso(
                str(event["scheduled_at"])
            ).date()
            if scheduled_date >= cutoff:
                targets.append(event)
        return tuple(targets)

    def _require_complete_event_catalog(
        self, season: str, retrieved_at: datetime
    ) -> list[dict[str, Any]]:
        events = self.event_catalog.get_events(season)
        freshness = self.event_catalog.get_freshness(season, now=retrieved_at)
        event_count = freshness.get("event_count")
        if not freshness.get("fresh") or not event_count or event_count != len(events):
            self._reject(
                "the Event Catalog must be present, fresh, and complete before publication"
            )
        return events

    def _require_fresh_athlete_catalog(
        self, season: str, retrieved_at: datetime
    ) -> list[dict[str, Any]]:
        freshness = self.athlete_catalog.get_freshness(season, now=retrieved_at)
        if not freshness.get("is_fresh") or not freshness.get("row_count"):
            self._reject(
                "the Athlete Catalog must be present and fresh before publication"
            )
        return self.athlete_catalog.get_catalog(season, active_only=False)

    def _publish_empty(
        self, season: str, retrieved_at: datetime
    ) -> PlayerGameLogIngestResult:
        try:
            self.repository.publish(
                season,
                (),
                retrieved_at=retrieved_at,
                source_provider=SOURCE_PROVIDER,
                source_row_count=0,
                allow_empty=True,
                publication_status="complete",
            )
        except ValueError:
            self._emit_telemetry(rejected=1)
            raise
        return PlayerGameLogIngestResult(
            season=season,
            games_processed=0,
            row_count=0,
            retrieved_at=retrieved_at.isoformat(),
        )

    @staticmethod
    def _merge_counts(
        counts: _AggregatedCounts, change: "_GameChange"
    ) -> _AggregatedCounts:
        added = change.counts
        return _AggregatedCounts(
            source_row_count=counts.source_row_count + added.source_row_count,
            unjoined_athlete_count=(
                counts.unjoined_athlete_count + added.unjoined_athlete_count
            ),
            unjoined_event_count=(
                counts.unjoined_event_count + added.unjoined_event_count
            ),
            team_mismatch_count=(
                counts.team_mismatch_count + added.team_mismatch_count
            ),
            unsupported_phase_count=(
                counts.unsupported_phase_count + added.unsupported_phase_count
            ),
            malformed_row_count=(
                counts.malformed_row_count + added.malformed_row_count
            ),
            duplicate_row_count=(
                counts.duplicate_row_count + added.duplicate_row_count
            ),
        )

    def _reject(
        self,
        message: str,
        *,
        counts: _AggregatedCounts | None = None,
        rejected: int = 1,
    ) -> NoReturn:
        self._emit_telemetry(counts=counts, rejected=rejected)
        raise PlayerGameLogIngestError(message)

    def _emit_telemetry(
        self,
        *,
        counts: _AggregatedCounts | None = None,
        published: int = 0,
        rejected: int = 0,
    ) -> None:
        coverage = counts or _AggregatedCounts()
        self.telemetry_recorder.record(
            PlayerGameLogTelemetryEvent(
                source_row_count=coverage.source_row_count,
                published_row_count=published,
                unjoined_athlete_count=coverage.unjoined_athlete_count,
                unjoined_event_count=coverage.unjoined_event_count,
                team_mismatch_count=coverage.team_mismatch_count,
                unsupported_phase_count=coverage.unsupported_phase_count,
                malformed_row_count=coverage.malformed_row_count,
                rejected_publication_count=rejected,
                duplicate_row_count=coverage.duplicate_row_count,
                recovered_shrink_row_count=0,
            )
        )


@dataclass(frozen=True, slots=True)
class _GameChange:
    """One fully validated game staged for the run's atomic publication."""

    game_id: str
    season_type: str
    records: tuple[PlayerGameLogRecord, ...]
    checksum: str
    counts: _AggregatedCounts


__all__ = [
    "PlayerGameLogIngestError",
    "PlayerGameLogIngestResult",
    "PlayerGameLogIngestService",
    "SOURCE_PROVIDER",
]
