"""Transactional persistence and deterministic reads for player game logs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_events import (
    REGULAR_SEASON_TYPE,
    validate_player_game_log_season_type,
)
from app.domain.freshness import (
    exact_age_seconds,
    exact_seconds,
    exact_timedelta,
    within_max_age,
)
from app.domain.utc import assume_utc
from app.models.player_game_log import (
    PlayerGameLog,
    PlayerGameLogRefresh,
    PlayerGameLogSync,
)
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.player_game_log_values import (
    player_game_log_market_values,
    validate_player_game_log_components,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import (
    PLAYER_GAME_LOG_SURFACE,
    StatsFreshnessRepository,
)


@dataclass(frozen=True, slots=True)
class PlayerGameLogRecord:
    season: str
    season_type: str
    player_id: int
    game_id: str
    player_name: str
    game_date: date
    team_id: int
    team_tricode: str
    opponent_team_id: int
    opponent_team_tricode: str
    is_home: bool
    minutes: float
    points: int
    rebounds: int
    assists: int
    field_goals_made: int
    field_goals_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    free_throws_made: int = 0
    free_throws_attempted: int = 0
    offensive_rebounds: int = 0
    defensive_rebounds: int = 0
    turnovers: int = 0
    steals: int = 0
    blocks: int = 0
    personal_fouls: int = 0


@dataclass(frozen=True, slots=True)
class PlayerGameLogFreshness:
    season: str
    source_provider: str | None
    retrieved_at: datetime | None
    row_count: int
    source_row_count: int
    identity_source_row_count: int
    #: Nothing is treated as a complete publication without PBP-derived,
    #: parity-checked evidence; the safe default for any legacy/NBA-derived or
    #: unknown state is ``in_progress``.
    publication_status: str = "in_progress"


@dataclass(frozen=True, slots=True)
class PlayerGameLogReadFreshness:
    status: str
    retrieved_at: datetime | None


@dataclass(frozen=True, slots=True)
class PlayerGameLogPublication:
    row_count: int
    recovered_removed_row_count: int


@dataclass(frozen=True, slots=True)
class PlayerGameLogRefreshChange:
    """One game staged for atomic publication by a refresh run."""

    game_id: str
    season_type: str
    records: tuple[PlayerGameLogRecord, ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class PlayerGameLogSyncStatus:
    season: str
    game_id: str
    season_type: str
    status: str
    checksum: str
    row_count: int
    source_provider: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class PlayerSeasonRate:
    season: str
    player_id: int
    game_count: int
    total_minutes: float
    per_game: dict[str, float]
    per_minute: dict[str, float]


@dataclass(frozen=True, slots=True)
class PlayerSeasonLogSummary:
    season: str
    player_id: int
    season_rate: PlayerSeasonRate | None
    last_ten_minutes: tuple[float, ...]


class PlayerGameLogRepository:
    """Publish season facts and observe one explicit current stats surface."""

    def __init__(
        self,
        engine: Engine,
        *,
        statistic_catalog: StatisticCatalog,
        stats_surface_max_age: timedelta,
        stats_surface_season: str,
        clock: Callable[[], datetime] | None = None,
        write_fence: Any | None = None,
        serve_stale: bool = False,
    ) -> None:
        self.engine = engine
        self._stats_surface_season = validate_canonical_season(stats_surface_season)
        self._surface_freshness = StatsFreshnessRepository(
            engine, surface=PLAYER_GAME_LOG_SURFACE
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._write_fence = write_fence
        self._serve_stale = bool(serve_stale)
        self._stats_surface_max_age_seconds = exact_seconds(
            exact_timedelta(
                exact_seconds(stats_surface_max_age),
                field="PLAYER_GAME_LOG_MAX_AGE_HOURS",
            )
        )
        self._market_statistics = tuple(
            statistic
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        )
        required_components = {
            component
            for statistic in self._market_statistics
            for component in statistic.components
        }
        stored_components = set(PlayerGameLogRecord.__dataclass_fields__)
        validate_player_game_log_components(
            required_components, stored_components=stored_components
        )

    def publish(
        self,
        season: str,
        records: Iterable[PlayerGameLogRecord],
        *,
        retrieved_at: datetime,
        source_provider: str,
        source_row_count: int,
        identity_source_row_count: int | None = None,
        allow_empty: bool = False,
        publication_status: str = "in_progress",
        current_catalog_game_ids: frozenset[str] | None = None,
        recoverable_game_ids: frozenset[str] = frozenset(),
    ) -> PlayerGameLogPublication:
        self._assert_legacy_write_allowed()
        canonical_season = validate_canonical_season(season)
        if publication_status not in {"complete", "in_progress"}:
            raise ValueError(
                "publication_status must be complete or in_progress"
            )
        if not source_provider or source_provider != source_provider.strip():
            raise ValueError("source_provider must be a non-empty canonical value")
        if (
            isinstance(source_row_count, bool)
            or not isinstance(source_row_count, int)
            or source_row_count < 0
        ):
            raise ValueError("source_row_count must be a non-negative integer")
        if identity_source_row_count is None:
            identity_source_row_count = source_row_count
        if (
            isinstance(identity_source_row_count, bool)
            or not isinstance(identity_source_row_count, int)
            or identity_source_row_count < 0
            or identity_source_row_count > source_row_count
        ):
            raise ValueError(
                "identity_source_row_count must be between zero and source_row_count"
            )

        unique: dict[tuple[int, str], PlayerGameLogRecord] = {}
        canonical_input_count = 0
        for record in records:
            canonical_input_count += 1
            if record.season != canonical_season:
                raise ValueError("every player game log must belong to the publication season")
            validate_player_game_log_season_type(record.season_type)
            key = (record.player_id, record.game_id)
            existing = unique.get(key)
            # The service rejects provider conflicts for telemetry; publish repeats
            # the invariant because it is also a direct persistence boundary.
            if existing is not None and existing != record:
                raise ValueError("conflicting player game log facts share one identity")
            unique[key] = record

        if not unique and not allow_empty:
            raise ValueError("empty player game log snapshot cannot be published")
        if source_row_count < canonical_input_count:
            raise ValueError(
                "source_row_count cannot be smaller than canonical input rows"
            )
        if identity_source_row_count < canonical_input_count:
            raise ValueError(
                "identity_source_row_count cannot be smaller than canonical input rows"
            )

        retrieved = assume_utc(retrieved_at)
        log_table = PlayerGameLog.__table__
        refresh_table = PlayerGameLogRefresh.__table__
        with self.engine.begin() as connection:
            existing_row_count = connection.execute(
                select(refresh_table.c.row_count).where(
                    refresh_table.c.season == canonical_season
                )
            ).scalar_one_or_none()
            if not unique and existing_row_count is not None and existing_row_count > 0:
                raise ValueError(
                    "empty player game log snapshot cannot replace a valid publication"
                )
            published_keys = set(
                connection.execute(
                    select(log_table.c.player_id, log_table.c.game_id).where(
                        log_table.c.season == canonical_season
                    )
                ).all()
            )
            removed_keys = published_keys.difference(unique)
            if removed_keys:
                if current_catalog_game_ids is None:
                    raise ValueError(
                        "a player game log publication cannot remove cumulative facts"
                    )
                if any(
                    game_id in current_catalog_game_ids
                    and game_id not in recoverable_game_ids
                    for _player_id, game_id in removed_keys
                ):
                    raise ValueError(
                        "a player game log publication cannot remove eligible cumulative facts"
                    )
            connection.execute(
                delete(log_table).where(log_table.c.season == canonical_season)
            )
            if unique:
                connection.execute(
                    insert(log_table), [asdict(record) for record in unique.values()]
                )
            values = {
                "source_provider": source_provider,
                "retrieved_at": retrieved,
                "row_count": len(unique),
                "source_row_count": source_row_count,
                "identity_source_row_count": identity_source_row_count,
                "publication_status": publication_status,
            }
            result = connection.execute(
                update(refresh_table)
                .where(refresh_table.c.season == canonical_season)
                .values(**values)
            )
            if result.rowcount == 0:
                connection.execute(
                    insert(refresh_table).values(season=canonical_season, **values)
                )
            if canonical_season == self._stats_surface_season:
                self._surface_freshness.record_success(
                    retrieved, connection=connection
                )
        return PlayerGameLogPublication(
            row_count=len(unique),
            recovered_removed_row_count=len(removed_keys),
        )

    def list_player_rows(
        self, season: str, player_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_season = validate_canonical_season(season)
        if not self._season_is_readable(canonical_season):
            return ()
        log_table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                self._published_rows_statement()
                .where(
                    log_table.c.season == canonical_season,
                    log_table.c.player_id == player_id,
                )
                .order_by(log_table.c.game_date.desc(), log_table.c.game_id.desc())
            ).mappings()
            return tuple(PlayerGameLogRecord(**dict(row)) for row in rows)

    def get_freshness(self, season: str) -> PlayerGameLogFreshness:
        canonical_season = validate_canonical_season(season)
        refresh_table = PlayerGameLogRefresh.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(refresh_table).where(
                    refresh_table.c.season == canonical_season
                )
            ).mappings().one_or_none()
        if row is None:
            return PlayerGameLogFreshness(canonical_season, None, None, 0, 0, 0)
        return PlayerGameLogFreshness(
            season=canonical_season,
            source_provider=row["source_provider"],
            retrieved_at=assume_utc(row["retrieved_at"]),
            row_count=int(row["row_count"]),
            source_row_count=int(row["source_row_count"]),
            identity_source_row_count=int(row["identity_source_row_count"]),
            publication_status=str(row["publication_status"]),
        )

    def get_read_freshness(self, season: str) -> PlayerGameLogReadFreshness:
        canonical_season = validate_canonical_season(season)
        publication = self.get_freshness(canonical_season)
        if publication.retrieved_at is None:
            return PlayerGameLogReadFreshness("missing", None)
        if canonical_season != self._stats_surface_season:
            return PlayerGameLogReadFreshness("fresh", publication.retrieved_at)
        completed_at = self._surface_freshness.get().last_successful_completion
        if completed_at is None:
            return PlayerGameLogReadFreshness("missing", publication.retrieved_at)
        elapsed = exact_seconds(assume_utc(self._clock()) - completed_at)
        age = exact_age_seconds(
            max(elapsed, Decimal(0)), field="player game log publication age"
        )
        status = (
            "fresh"
            if within_max_age(age, self._stats_surface_max_age_seconds)
            else "stale"
        )
        return PlayerGameLogReadFreshness(status, publication.retrieved_at)

    def publish_refresh(
        self,
        season: str,
        changes: Iterable[PlayerGameLogRefreshChange],
        *,
        retrieved_at: datetime,
        source_provider: str,
        expected_complete_game_ids: frozenset[str],
    ) -> PlayerGameLogPublication:
        """Atomically publish one refresh run's staged game facts.

        The refresh service collects and fully validates every target game
        before calling this method, so a single transaction replaces the
        changed games' player rows, upserts their per-game sync evidence,
        recomputes the season sidecar, and advances the configured current
        season's stats-surface observation.  A run that fails before this
        boundary changes nothing, preserving the exact prior fact rows and the
        last complete publication.
        """
        self._assert_legacy_write_allowed()
        canonical_season = validate_canonical_season(season)
        if not source_provider or source_provider != source_provider.strip():
            raise ValueError("source_provider must be a non-empty canonical value")
        staged: dict[str, tuple[str, tuple[PlayerGameLogRecord, ...], str]] = {}
        for change in changes:
            if not change.game_id or change.game_id != change.game_id.strip():
                raise ValueError("game_id must be a non-empty canonical value")
            if not change.checksum or change.checksum != change.checksum.strip():
                raise ValueError("checksum must be a non-empty canonical value")
            validate_player_game_log_season_type(change.season_type)
            unique: dict[tuple[int, str], PlayerGameLogRecord] = {}
            for record in change.records:
                if (
                    record.season != canonical_season
                    or record.game_id != change.game_id
                ):
                    raise ValueError(
                        "every player game log must belong to its game publication"
                    )
                if record.season_type != change.season_type:
                    raise ValueError(
                        "every player game log must match the game's phase"
                    )
                key = (record.player_id, record.game_id)
                existing = unique.get(key)
                if existing is not None and existing != record:
                    raise ValueError(
                        "conflicting player game log facts share one identity"
                    )
                unique[key] = record
            if not unique:
                raise ValueError(
                    "a completed game must publish at least one player fact"
                )
            staged[change.game_id] = (
                change.season_type,
                tuple(unique.values()),
                change.checksum,
            )

        retrieved = assume_utc(retrieved_at)
        log_table = PlayerGameLog.__table__
        refresh_table = PlayerGameLogRefresh.__table__
        sync_table = PlayerGameLogSync.__table__
        published_rows = 0
        with self.engine.begin() as connection:
            for game_id, (season_type, records, checksum) in staged.items():
                connection.execute(
                    delete(log_table).where(
                        log_table.c.season == canonical_season,
                        log_table.c.game_id == game_id,
                    )
                )
                connection.execute(
                    insert(log_table),
                    [asdict(record) for record in records],
                )
                sync_values = {
                    "season": canonical_season,
                    "game_id": game_id,
                    "season_type": season_type,
                    "status": "complete",
                    "checksum": checksum,
                    "row_count": len(records),
                    "source_provider": source_provider,
                    "retrieved_at": retrieved,
                }
                result = connection.execute(
                    update(sync_table)
                    .where(
                        sync_table.c.season == canonical_season,
                        sync_table.c.game_id == game_id,
                    )
                    .values(**sync_values)
                )
                if result.rowcount == 0:
                    connection.execute(
                        insert(sync_table).values(**sync_values)
                    )
                published_rows += len(records)
            row_count = connection.execute(
                select(func.count())
                .select_from(log_table)
                .where(log_table.c.season == canonical_season)
            ).scalar_one()
            publication_status = self._completed_coverage_status(
                connection,
                canonical_season,
                expected_complete_game_ids,
            )
            values = {
                "source_provider": source_provider,
                "retrieved_at": retrieved,
                "row_count": int(row_count),
                "source_row_count": int(row_count),
                "identity_source_row_count": int(row_count),
                "publication_status": publication_status,
            }
            result = connection.execute(
                update(refresh_table)
                .where(refresh_table.c.season == canonical_season)
                .values(**values)
            )
            if result.rowcount == 0:
                connection.execute(
                    insert(refresh_table).values(season=canonical_season, **values)
                )
            if canonical_season == self._stats_surface_season:
                self._surface_freshness.record_success(
                    retrieved, connection=connection
                )
        return PlayerGameLogPublication(
            row_count=published_rows,
            recovered_removed_row_count=0,
        )

    def _assert_legacy_write_allowed(self) -> None:
        """Keep the pre-activation writer additive but fence it after cutover."""

        if self._write_fence is not None:
            checker = getattr(self._write_fence, "assert_writable", None)
            if callable(checker):
                checker("player_game_logs")

    def _completed_coverage_status(
        self,
        connection: Any,
        season: str,
        expected_complete_game_ids: frozenset[str],
    ) -> str:
        if not expected_complete_game_ids:
            return "complete"
        sync_table = PlayerGameLogSync.__table__
        stored = set(
            connection.execute(
                select(sync_table.c.game_id).where(
                    sync_table.c.season == season,
                    sync_table.c.status == "complete",
                )
            ).scalars().all()
        )
        return (
            "complete"
            if expected_complete_game_ids.issubset(stored)
            else "in_progress"
        )

    def has_complete_publication(self, season: str) -> bool:
        """Whether this season may serve database-first reads.

        A season is durably complete only when its sidecar carries a complete
        publication and that publication is still valid: a historical season
        with any sidecar is complete, while the configured current season must
        also carry a fresh stats-surface observation.  A database that does not
        own the player-log surface (such as the read-only demo fixture) simply
        reports no complete publication.
        """
        canonical_season = validate_canonical_season(season)
        try:
            freshness = self.get_freshness(canonical_season)
            if (
                freshness.retrieved_at is None
                or freshness.publication_status != "complete"
            ):
                return False
            if self._serve_stale:
                # Once database-first activation is enabled, a failed refresh
                # never hides the last good complete publication.  The caller
                # receives its real stale status separately.
                return True
            return self.get_read_freshness(canonical_season).status == "fresh"
        except SQLAlchemyError:
            return False

    def stored_game_ids(self, season: str) -> frozenset[str]:
        """Return the governed game identities with stored facts for a season."""
        canonical_season = validate_canonical_season(season)
        log_table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(log_table.c.game_id)
                .where(log_table.c.season == canonical_season)
                .distinct()
            ).scalars().all()
        return frozenset(str(row) for row in rows)

    def get_sync_status(
        self, season: str, game_id: str
    ) -> PlayerGameLogSyncStatus | None:
        """Return the bounded synchronization evidence for one game."""
        canonical_season = validate_canonical_season(season)
        sync_table = PlayerGameLogSync.__table__
        with self.engine.connect() as connection:
            row = connection.execute(
                select(sync_table).where(
                    sync_table.c.season == canonical_season,
                    sync_table.c.game_id == game_id,
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return PlayerGameLogSyncStatus(
            season=canonical_season,
            game_id=str(row["game_id"]),
            season_type=str(row["season_type"]),
            status=str(row["status"]),
            checksum=str(row["checksum"]),
            row_count=int(row["row_count"]),
            source_provider=str(row["source_provider"]),
            retrieved_at=assume_utc(row["retrieved_at"]),
        )

    @staticmethod
    def game_checksum(records: Iterable[PlayerGameLogRecord]) -> str:
        """Return one bounded checksum over a game's normalized facts."""
        facts = [
            (
                record.game_id,
                record.player_id,
                record.player_name,
                str(record.game_date),
                record.team_id,
                record.opponent_team_id,
                record.is_home,
                record.minutes,
                record.points,
                record.rebounds,
                record.assists,
                record.field_goals_made,
                record.field_goals_attempted,
                record.three_pointers_made,
                record.three_pointers_attempted,
                record.free_throws_made,
                record.free_throws_attempted,
                record.offensive_rebounds,
                record.defensive_rebounds,
                record.turnovers,
                record.steals,
                record.blocks,
                record.personal_fouls,
            )
            for record in records
        ]
        digest = sha256()
        for fact in sorted(facts):
            digest.update(repr(fact).encode("utf-8"))
        return digest.hexdigest()

    def get_season_rate(
        self,
        season: str,
        player_id: int,
        *,
        season_type: str | None = REGULAR_SEASON_TYPE,
    ) -> PlayerSeasonRate | None:
        return self.get_player_summaries(
            season,
            (player_id,),
            rate_season_type=season_type,
        )[player_id].season_rate

    def get_player_summaries(
        self,
        season: str,
        player_ids: Iterable[int],
        *,
        rate_season_type: str | None = REGULAR_SEASON_TYPE,
    ) -> dict[int, PlayerSeasonLogSummary]:
        """Return per-player rates and chronological combined-phase last tens."""

        canonical_season = validate_canonical_season(season)
        canonical_ids = tuple(sorted(set(player_ids)))
        if not canonical_ids:
            return {}
        if rate_season_type is not None:
            rate_season_type = validate_player_game_log_season_type(
                rate_season_type
            )
        rows_by_player: dict[int, list[PlayerGameLogRecord]] = {
            player_id: [] for player_id in canonical_ids
        }
        if self._season_is_readable(canonical_season):
            log_table = PlayerGameLog.__table__
            with self.engine.connect() as connection:
                rows = connection.execute(
                    self._published_rows_statement()
                    .where(
                        log_table.c.season == canonical_season,
                        log_table.c.player_id.in_(canonical_ids),
                    )
                    .order_by(
                        log_table.c.player_id.asc(),
                        log_table.c.game_date.asc(),
                        log_table.c.game_id.asc(),
                    )
                ).mappings()
                for row in rows:
                    record = PlayerGameLogRecord(**dict(row))
                    rows_by_player[record.player_id].append(record)
        return {
            player_id: PlayerSeasonLogSummary(
                season=canonical_season,
                player_id=player_id,
                season_rate=self._season_rate(
                    canonical_season,
                    player_id,
                    tuple(
                        row
                        for row in rows_by_player[player_id]
                        if rate_season_type is None
                        or row.season_type == rate_season_type
                    ),
                ),
                last_ten_minutes=tuple(
                    row.minutes for row in rows_by_player[player_id][-10:]
                ),
            )
            for player_id in canonical_ids
        }

    def _season_rate(
        self,
        season: str,
        player_id: int,
        rows: tuple[PlayerGameLogRecord, ...],
    ) -> PlayerSeasonRate | None:
        total_minutes = sum(row.minutes for row in rows)
        if not rows or total_minutes <= 0:
            return None
        row_values = tuple(self._market_values(row) for row in rows)
        totals = {
            statistic.market_category: sum(
                values[statistic.market_category] for values in row_values
            )
            for statistic in self._market_statistics
            if statistic.market_category is not None
        }
        return PlayerSeasonRate(
            season=season,
            player_id=player_id,
            game_count=len(rows),
            total_minutes=total_minutes,
            per_game={market: value / len(rows) for market, value in totals.items()},
            per_minute={
                market: value / total_minutes for market, value in totals.items()
            },
        )

    def get_last_ten_minutes(
        self, season: str, player_id: int
    ) -> tuple[float, ...]:
        return self.get_player_summaries(season, (player_id,))[
            player_id
        ].last_ten_minutes

    def list_h2h_rows(
        self, season: str, player_id: int, opponent_team_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        return self._list_rows(
            season,
            player_ids=(player_id,),
            opponent_team_id=opponent_team_id,
        )

    def list_archetype_rows(
        self, season: str, player_ids: Iterable[int], opponent_team_id: int
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_ids = tuple(sorted(set(player_ids)))
        if not canonical_ids:
            return ()
        return self._list_rows(
            season,
            player_ids=canonical_ids,
            opponent_team_id=opponent_team_id,
        )

    def _list_rows(
        self,
        season: str,
        *,
        player_ids: tuple[int, ...],
        opponent_team_id: int,
    ) -> tuple[PlayerGameLogRecord, ...]:
        canonical_season = validate_canonical_season(season)
        if not self._season_is_readable(canonical_season):
            return ()
        log_table = PlayerGameLog.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                self._published_rows_statement()
                .where(
                    log_table.c.season == canonical_season,
                    log_table.c.player_id.in_(player_ids),
                    log_table.c.opponent_team_id == opponent_team_id,
                )
                .order_by(
                    log_table.c.game_date.desc(),
                    log_table.c.player_id.asc(),
                    log_table.c.game_id.desc(),
                )
            ).mappings()
            return tuple(PlayerGameLogRecord(**dict(row)) for row in rows)

    def _season_is_readable(self, season: str) -> bool:
        if self._serve_stale:
            publication = self.get_freshness(season)
            return publication.retrieved_at is not None and publication.row_count > 0
        if season != self._stats_surface_season:
            return True
        completed_at = self._surface_freshness.get().last_successful_completion
        if completed_at is None:
            return False
        elapsed = exact_seconds(assume_utc(self._clock()) - completed_at)
        age = exact_age_seconds(
            max(elapsed, Decimal(0)), field="player game log publication age"
        )
        return within_max_age(age, self._stats_surface_max_age_seconds)

    @staticmethod
    def _published_rows_statement():
        log_table = PlayerGameLog.__table__
        refresh_table = PlayerGameLogRefresh.__table__
        return (
            select(log_table)
            .join(
                refresh_table,
                refresh_table.c.season == log_table.c.season,
            )
            .where(refresh_table.c.row_count > 0)
        )

    def _market_values(self, record: PlayerGameLogRecord) -> dict[str, float]:
        return player_game_log_market_values(record, self._market_statistics)


__all__ = [
    "PlayerGameLogFreshness",
    "PlayerGameLogPublication",
    "PlayerGameLogReadFreshness",
    "PlayerGameLogRecord",
    "PlayerGameLogRefreshChange",
    "PlayerGameLogRepository",
    "PlayerGameLogSyncStatus",
    "PlayerSeasonLogSummary",
    "PlayerSeasonRate",
]
