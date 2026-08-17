"""Durable normalized projection evidence and its database-first live read model."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
import threading
from typing import Any, Callable

from sqlalchemy import delete, func, inspect, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.config.settings import ConfigurationError
from app.domain.comparisons import market_reference
from app.domain.statistics import MatchState, ScoringPeriod
from app.domain.utc import assume_utc
from app.models.projection_archive import (
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.providers.dfs import (
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
)
from app.services.dfs_snapshot_cache import (
    deserialize_provider_snapshot,
    serialize_provider_snapshot,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog


PROJECTION_ARCHIVE_REQUIRED_TABLES = (
    "projection_archive_scope_locks",
    "projection_provider_snapshots",
    "projection_provider_polls",
    "projection_observations",
    "projection_materialization_generations",
    "latest_player_projections",
)
PROJECTION_ARCHIVE_REQUIRED_COLUMNS = {
    "projection_provider_polls": ("failure_reason", "promoted"),
    "latest_player_projections": ("confirmed_at",),
}
DEFAULT_PROJECTION_ARCHIVE_MAX_MARKETS = 10_000
DEFAULT_PROJECTION_ARCHIVE_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
PROJECTION_LIVE_MAX_AGE = timedelta(minutes=15)
PROJECTION_FAILURE_FALLBACK_MAX_AGE = timedelta(hours=6)
_FAILURE_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


def require_projection_archive_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    missing_tables = tuple(
        table_name
        for table_name in PROJECTION_ARCHIVE_REQUIRED_TABLES
        if not inspector.has_table(table_name)
    )
    missing_columns = tuple(
        f"{table_name}.{column_name}"
        for table_name, required_columns in PROJECTION_ARCHIVE_REQUIRED_COLUMNS.items()
        if table_name not in missing_tables
        for column_name in required_columns
        if column_name
        not in {
            column["name"] for column in inspector.get_columns(table_name)
        }
    )
    if missing_tables or missing_columns:
        missing = (*missing_tables, *missing_columns)
        raise ConfigurationError(
            "Projection archive dependencies require migrations "
            "037_projection_archive and 038_projection_archive_transitions; missing: "
            + ", ".join(missing)
        )


@dataclass(frozen=True, slots=True)
class ProjectionArchiveResult:
    """Observable result of accepting one normalized provider snapshot."""

    snapshot_id: str
    generation_id: str
    changed: bool
    observation_count: int
    materialization_outcome: str


@dataclass(frozen=True, slots=True)
class ProjectionPollResult:
    """Observable result of accepting a poll without usable snapshot evidence."""

    poll_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class _PartialLatestTransition:
    references: tuple[str, ...]
    selected_rows: tuple[dict[str, Any], ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class ProjectionArchiveReadScope:
    """One explicit provider/query identity selected for request-time reads."""

    provider: str
    query: NBAMarketQuery

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold()
        if not provider:
            raise ValueError("projection archive read provider is required")
        if not isinstance(self.query, NBAMarketQuery) or self.query.season is None:
            raise ValueError("projection archive read scope requires a season query")
        object.__setattr__(self, "provider", provider)

    @property
    def query_key(self) -> str:
        return _query_key(self.query)


def _digest(prefix: str, *values: object) -> str:
    payload = "\x1f".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}_{sha256(payload).hexdigest()}"


def _snapshot_checksum(query_key: str, document: str) -> str:
    return sha256(f"{query_key}\x1f{document}".encode("utf-8")).hexdigest()


def _snapshot_content_checksum(query_key: str, document: str) -> str:
    payload = json.loads(document)
    del payload["retrieved_at"]
    canonical_content = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return _snapshot_checksum(query_key, canonical_content)


def _materialization_checksum(rows: list[dict[str, Any]]) -> str:
    governed_fields = (
        "ordinal",
        "market_reference",
        "canonical_game_id",
        "canonical_player_id",
        "canonical_player_name",
        "canonical_team_id",
        "canonical_statistic_id",
        "market_category",
        "targetable",
    )
    payload = [{field: row[field] for field in governed_fields} for row in rows]
    return sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _source_snapshot(snapshot: ProviderSnapshot) -> ProviderSnapshot:
    """Remove derived statistic matches from the immutable source document."""

    return replace(
        snapshot,
        markets=tuple(
            replace(market, statistic_match=None) for market in snapshot.markets
        ),
    )


def _query_key(query: NBAMarketQuery) -> str:
    return _digest(
        "qry",
        query.season,
        query.sport,
        query.league,
        ",".join(sorted(status.value for status in query.market_statuses)),
        query.pregame_only,
    )


class ProjectionArchive:
    """Accept a complete normalized snapshot as one atomic archive generation."""

    _scope_locks: dict[tuple[int, str, str, str], threading.RLock] = {}
    _scope_locks_guard = threading.Lock()

    def __init__(
        self,
        engine: Engine,
        statistic_catalog: StatisticCatalog,
        *,
        max_markets: int = DEFAULT_PROJECTION_ARCHIVE_MAX_MARKETS,
        max_document_bytes: int = DEFAULT_PROJECTION_ARCHIVE_MAX_DOCUMENT_BYTES,
    ) -> None:
        if max_markets < 1 or max_document_bytes < 1:
            raise ValueError("projection archive evidence bounds must be positive")
        self.engine = engine
        self.max_markets = max_markets
        self.max_document_bytes = max_document_bytes
        self.market_categories = {
            statistic.id: statistic.market_category
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        }

    def ingest_complete_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
    ) -> ProjectionArchiveResult:
        if snapshot.status is not SnapshotStatus.COMPLETE:
            raise ValueError(
                "only complete provider snapshots may enter this archive path"
            )
        return self.ingest_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
        )

    def ingest_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
    ) -> ProjectionArchiveResult:
        """Archive one Complete or Partial normalized provider observation."""

        if not isinstance(snapshot, ProviderSnapshot):
            raise TypeError("snapshot must be ProviderSnapshot")
        if not isinstance(query, NBAMarketQuery) or query.season is None:
            raise ValueError("projection archive queries require a canonical season")
        if len(snapshot.markets) > self.max_markets:
            raise ValueError("projection snapshot exceeds the configured market limit")

        accepted = assume_utc(accepted_at or datetime.now(timezone.utc))
        poll_started = None if poll_started_at is None else assume_utc(poll_started_at)
        if poll_started is not None and poll_started > accepted:
            raise ValueError("projection poll cannot start after it completes")
        source = _source_snapshot(snapshot)
        query_key = _query_key(query)
        observation_count = len(snapshot.markets)
        document = serialize_provider_snapshot(source, query, allow_partial=True)
        if len(document.encode("utf-8")) > self.max_document_bytes:
            raise ValueError("projection snapshot exceeds the evidence document limit")
        checksum = _snapshot_checksum(query_key, document)
        content_checksum = _snapshot_content_checksum(query_key, document)
        evidence_snapshot_id = f"psn_{checksum}"
        observation_rows = self._observation_rows(snapshot)
        materialization_checksum = _materialization_checksum(observation_rows)
        poll_id = _digest(
            "poll",
            evidence_snapshot_id,
            "" if poll_started is None else poll_started.isoformat(),
            accepted.isoformat(),
        )
        if accepted < assume_utc(snapshot.retrieved_at):
            raise ValueError("projection snapshot cannot be accepted before retrieval")
        snapshot_table = ProjectionProviderSnapshot.__table__

        with self._scope_transaction(
            snapshot.provider,
            query.season,
            query_key,
        ) as connection:
            replayed = self._replayed_result(connection, poll_id)
            if replayed is not None:
                return replayed
            current = self._current_materialization(
                connection,
                provider=snapshot.provider,
                season=query.season,
                query_key=query_key,
            )
            promoted_through = self._promotion_fence(
                connection,
                provider=snapshot.provider,
                season=query.season,
                query_key=query_key,
            )
            materialization_outcome = self._materialization_outcome(
                current,
                incoming_retrieved_at=snapshot.retrieved_at,
                promoted_through=promoted_through,
            )
            partial_transition = None
            if snapshot.status is SnapshotStatus.PARTIAL:
                partial_transition = self._plan_partial_transition(
                    connection,
                    observation_rows,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                )
                materialization_checksum = partial_transition.checksum
            if (
                current is not None
                and current["content_checksum"] == content_checksum
                and current["materialization_checksum"] == materialization_checksum
            ):
                self._record_poll(
                    connection,
                    poll_id=poll_id,
                    snapshot=snapshot,
                    season=query.season,
                    query_key=query_key,
                    started_at=poll_started,
                    completed_at=accepted,
                    outcome="unchanged",
                    promoted=materialization_outcome == "advanced",
                    snapshot_id=str(current["snapshot_id"]),
                    generation_id=str(current["generation_id"]),
                    observation_count=observation_count,
                )
                if materialization_outcome == "advanced":
                    self._confirm_latest(
                        connection,
                        observation_rows,
                        provider=snapshot.provider,
                        season=query.season,
                        query_key=query_key,
                        confirmed_at=snapshot.retrieved_at,
                        complete=snapshot.status is SnapshotStatus.COMPLETE,
                    )
                return ProjectionArchiveResult(
                    snapshot_id=str(current["snapshot_id"]),
                    generation_id=str(current["generation_id"]),
                    changed=False,
                    observation_count=observation_count,
                    materialization_outcome="unchanged",
                )

            provider_content_unchanged = (
                current is not None and current["content_checksum"] == content_checksum
            )
            if (
                provider_content_unchanged
                and materialization_outcome == "same_time_not_promoted"
            ):
                # A governed mapping correction can rematerialize the exact
                # confirmed provider observation without inventing a newer
                # provider time. Same-time conflicting provider content still
                # remains non-promoting below.
                materialization_outcome = "advanced"
            existing = connection.execute(
                select(snapshot_table.c.snapshot_id).where(
                    snapshot_table.c.checksum == checksum
                )
            ).scalar_one_or_none()
            snapshot_id = (
                str(current["snapshot_id"])
                if provider_content_unchanged
                else str(existing or evidence_snapshot_id)
            )
            generation_id = _digest(
                "gen",
                snapshot_id,
                materialization_checksum,
                assume_utc(snapshot.retrieved_at).isoformat(),
            )
            existing_generation = connection.execute(
                select(ProjectionMaterializationGeneration.outcome).where(
                    ProjectionMaterializationGeneration.generation_id == generation_id
                )
            ).scalar_one_or_none()
            if existing_generation is not None:
                self._record_poll(
                    connection,
                    poll_id=poll_id,
                    snapshot=snapshot,
                    season=query.season,
                    query_key=query_key,
                    started_at=poll_started,
                    completed_at=accepted,
                    outcome="unchanged",
                    promoted=False,
                    snapshot_id=snapshot_id,
                    generation_id=generation_id,
                    observation_count=observation_count,
                )
                return ProjectionArchiveResult(
                    snapshot_id=snapshot_id,
                    generation_id=generation_id,
                    changed=False,
                    observation_count=observation_count,
                    materialization_outcome="unchanged",
                )

            if existing is None and not provider_content_unchanged:
                connection.execute(insert(snapshot_table).values(
                    snapshot_id=snapshot_id,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                    contract_version=snapshot.contract_version,
                    snapshot_status=snapshot.status.value,
                    retrieved_at=snapshot.retrieved_at,
                    accepted_at=accepted,
                    checksum=checksum,
                    content_checksum=content_checksum,
                    evidence_document=document,
                ))
            for row in observation_rows:
                row["snapshot_id"] = snapshot_id
                row["generation_id"] = generation_id
                row["source_poll_id"] = poll_id
                row["observation_id"] = _digest(
                    "obs", generation_id, row["ordinal"], row["market_reference"]
                )
            self._record_poll(
                connection,
                poll_id=poll_id,
                snapshot=snapshot,
                season=query.season,
                query_key=query_key,
                started_at=poll_started,
                completed_at=accepted,
                outcome=(
                    "rematerialized"
                    if provider_content_unchanged
                    else (
                        "partial"
                        if snapshot.status is SnapshotStatus.PARTIAL
                        else "changed"
                    )
                ),
                promoted=materialization_outcome == "advanced",
                snapshot_id=snapshot_id,
                generation_id=generation_id,
                observation_count=observation_count,
            )
            connection.execute(
                insert(ProjectionMaterializationGeneration.__table__).values(
                    generation_id=generation_id,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                    snapshot_id=snapshot_id,
                    source_poll_id=poll_id,
                    created_at=accepted,
                    retrieved_at=snapshot.retrieved_at,
                    materialization_checksum=materialization_checksum,
                    outcome=materialization_outcome,
                )
            )
            if observation_rows:
                connection.execute(
                    insert(ProjectionObservation.__table__), observation_rows
                )
            if materialization_outcome == "advanced":
                if snapshot.status is SnapshotStatus.COMPLETE:
                    self._advance_latest(
                        connection,
                        observation_rows,
                        provider=snapshot.provider,
                        season=query.season,
                        query_key=query_key,
                        generation_id=generation_id,
                    )
                else:
                    assert partial_transition is not None
                    self._apply_partial_transition(
                        connection,
                        partial_transition,
                        provider=snapshot.provider,
                        season=query.season,
                        query_key=query_key,
                        generation_id=generation_id,
                    )
        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            changed=not provider_content_unchanged,
            observation_count=observation_count,
            materialization_outcome=materialization_outcome,
        )

    def record_failed_poll(
        self,
        *,
        provider: str,
        query: NBAMarketQuery,
        completed_at: datetime | None = None,
        poll_started_at: datetime | None = None,
        failure_reason: str,
    ) -> ProjectionPollResult:
        """Record bounded provider failure health without changing evidence or Latest."""

        normalized_provider = provider.strip().casefold()
        normalized_reason = failure_reason.strip().casefold()
        if not normalized_provider:
            raise ValueError("projection poll provider is required")
        if not _FAILURE_REASON.fullmatch(normalized_reason):
            raise ValueError("projection poll failure reason must be a bounded code")
        if not isinstance(query, NBAMarketQuery) or query.season is None:
            raise ValueError("projection archive queries require a canonical season")
        completed = assume_utc(completed_at or datetime.now(timezone.utc))
        started = None if poll_started_at is None else assume_utc(poll_started_at)
        if started is not None and started > completed:
            raise ValueError("projection poll cannot start after it completes")
        query_key = _query_key(query)
        poll_id = _digest(
            "poll_failure",
            normalized_provider,
            query_key,
            normalized_reason,
            "" if started is None else started.isoformat(),
            completed.isoformat(),
        )
        table = ProviderPoll.__table__
        with self._scope_transaction(
            normalized_provider, query.season, query_key
        ) as connection:
            if connection.execute(
                select(table.c.poll_id).where(table.c.poll_id == poll_id)
            ).scalar_one_or_none() is None:
                connection.execute(
                    insert(table).values(
                        poll_id=poll_id,
                        provider=normalized_provider,
                        season=query.season,
                        query_key=query_key,
                        started_at=started,
                        completed_at=completed,
                        retrieved_at=None,
                        outcome="failed",
                        promoted=False,
                        failure_reason=normalized_reason,
                        snapshot_id=None,
                        generation_id=None,
                        observation_count=0,
                    )
                )
        return ProjectionPollResult(poll_id=poll_id, outcome="failed")

    @contextmanager
    def _scope_transaction(
        self,
        provider: str,
        season: str,
        query_key: str,
    ) -> Any:
        key = (id(self.engine), provider, season, query_key)
        with self._scope_locks_guard:
            lock = self._scope_locks.setdefault(key, threading.RLock())
        table = ProjectionArchiveScopeLock.__table__
        if self.engine.dialect.name == "sqlite":
            with lock, self.engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        insert(table)
                        .values(provider=provider, season=season, query_key=query_key)
                        .prefix_with("OR IGNORE")
                    )
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            return
        with lock, self.engine.begin() as connection:
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(table).values(
                            provider=provider,
                            season=season,
                            query_key=query_key,
                        )
                    )
            except IntegrityError:
                pass
            connection.execute(
                select(table)
                .where(
                    table.c.provider == provider,
                    table.c.season == season,
                    table.c.query_key == query_key,
                )
                .with_for_update()
            ).one()
            yield connection

    @staticmethod
    def _replayed_result(
        connection: Any,
        poll_id: str,
    ) -> ProjectionArchiveResult | None:
        poll_table = ProviderPoll.__table__
        generation_table = ProjectionMaterializationGeneration.__table__
        poll = (
            connection.execute(
                select(
                    poll_table.c.snapshot_id,
                    poll_table.c.generation_id,
                    poll_table.c.outcome,
                    poll_table.c.observation_count,
                ).where(poll_table.c.poll_id == poll_id)
            )
            .mappings()
            .one_or_none()
        )
        if poll is None:
            return None
        snapshot_id = str(poll["snapshot_id"])
        generation = (
            connection.execute(
                select(
                    generation_table.c.generation_id,
                    generation_table.c.outcome,
                ).where(
                    generation_table.c.generation_id == poll["generation_id"]
                )
            )
            .mappings()
            .one()
        )
        changed = poll["outcome"] in {"changed", "partial"}
        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=str(generation["generation_id"]),
            changed=changed,
            observation_count=int(poll["observation_count"]),
            materialization_outcome=(
                str(generation["outcome"])
                if poll["outcome"] in {"changed", "partial", "rematerialized"}
                else "unchanged"
            ),
        )

    def load_source_snapshot(self, snapshot_id: str) -> ProviderSnapshot | None:
        """Read and checksum-verify one archived normalized source document."""

        table = ProjectionProviderSnapshot.__table__
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        table.c.evidence_document,
                        table.c.checksum,
                        table.c.query_key,
                    ).where(table.c.snapshot_id == snapshot_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        document = str(row["evidence_document"])
        checksum = _snapshot_checksum(str(row["query_key"]), document)
        if checksum != row["checksum"]:
            raise ValueError("archived projection snapshot checksum is invalid")
        return deserialize_provider_snapshot(document, allow_partial=True)

    @staticmethod
    def _current_materialization(
        connection: Any,
        *,
        provider: str,
        season: str,
        query_key: str,
    ) -> Any | None:
        generation_table = ProjectionMaterializationGeneration.__table__
        snapshot_table = ProjectionProviderSnapshot.__table__
        return (
            connection.execute(
                select(
                    generation_table.c.generation_id,
                    snapshot_table.c.snapshot_id,
                    generation_table.c.retrieved_at,
                    generation_table.c.materialization_checksum,
                    snapshot_table.c.content_checksum,
                )
                .select_from(
                    generation_table.join(
                        snapshot_table,
                        generation_table.c.snapshot_id == snapshot_table.c.snapshot_id,
                    )
                )
                .where(
                    generation_table.c.provider == provider,
                    generation_table.c.season == season,
                    generation_table.c.query_key == query_key,
                    generation_table.c.outcome == "advanced",
                )
                .order_by(generation_table.c.retrieved_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _materialization_outcome(
        current: Any | None,
        *,
        incoming_retrieved_at: datetime,
        promoted_through: datetime | None = None,
    ) -> str:
        if current is None and promoted_through is None:
            return "advanced"
        incoming = assume_utc(incoming_retrieved_at)
        fences = []
        if current is not None:
            fences.append(assume_utc(current["retrieved_at"]))
        if promoted_through is not None:
            fences.append(assume_utc(promoted_through))
        current_retrieved_at = max(fences)
        if incoming > current_retrieved_at:
            return "advanced"
        if incoming == current_retrieved_at:
            return "same_time_not_promoted"
        return "older_not_promoted"

    @staticmethod
    def _promotion_fence(
        connection: Any,
        *,
        provider: str,
        season: str,
        query_key: str,
    ) -> datetime | None:
        table = ProviderPoll.__table__
        return connection.execute(
            select(func.max(table.c.retrieved_at)).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
                table.c.promoted.is_(True),
            )
        ).scalar_one()

    @staticmethod
    def _record_poll(
        connection: Any,
        *,
        poll_id: str,
        snapshot: ProviderSnapshot,
        season: str,
        query_key: str,
        started_at: datetime | None,
        completed_at: datetime,
        outcome: str,
        promoted: bool,
        snapshot_id: str,
        generation_id: str,
        observation_count: int,
    ) -> None:
        connection.execute(
            insert(ProviderPoll.__table__).values(
                poll_id=poll_id,
                provider=snapshot.provider,
                season=season,
                query_key=query_key,
                started_at=started_at,
                completed_at=completed_at,
                retrieved_at=snapshot.retrieved_at,
                outcome=outcome,
                promoted=promoted,
                snapshot_id=snapshot_id,
                generation_id=generation_id,
                observation_count=observation_count,
            )
        )

    def _observation_rows(self, snapshot: ProviderSnapshot) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ordinal, market in enumerate(snapshot.markets):
            reference = market_reference(market)
            canonical_statistic_id = self._canonical_statistic_id(market)
            category = (
                None
                if canonical_statistic_id is None
                else self.market_categories.get(canonical_statistic_id)
            )
            canonical_player_id, canonical_player_name, athlete_team_id = (
                self._canonical_athlete_fields(market)
            )
            canonical_game_id = (
                None if market.event is None else market.event.canonical_id
            )
            canonical_team_id = (
                market.team.canonical_id
                if market.team is not None and market.team.canonical_id is not None
                else athlete_team_id
            )
            targetable = (
                market.status is MarketStatus.AVAILABLE
                and market.variant is MarketVariant.STANDARD
                and market.scoring_period is ScoringPeriod.FULL_GAME
                and canonical_player_id is not None
                and canonical_player_name is not None
                and canonical_game_id is not None
                and canonical_team_id is not None
                and category is not None
            )
            rows.append(
                {
                    "ordinal": ordinal,
                    "provider": snapshot.provider,
                    "provider_market_id": market.market_id,
                    "market_reference": reference,
                    "canonical_game_id": canonical_game_id,
                    "canonical_player_id": canonical_player_id,
                    "canonical_team_id": canonical_team_id,
                    "canonical_statistic_id": canonical_statistic_id,
                    "market_category": category,
                    "market_status": market.status.value,
                    "market_variant": market.variant.value,
                    "scoring_period": market.scoring_period.value,
                    "targetable": targetable,
                    "observed_at": snapshot.retrieved_at,
                    "canonical_player_name": canonical_player_name,
                }
            )
        return rows

    @staticmethod
    def _canonical_statistic_id(market: PlayerProjectionMarket) -> str | None:
        match = market.statistic_match
        if match is None or match.state is not MatchState.CANONICAL:
            return None
        return match.canonical_id

    @staticmethod
    def _canonical_athlete_fields(
        market: PlayerProjectionMarket,
    ) -> tuple[int | None, str | None, int | None]:
        athlete = market.athlete
        if athlete is None:
            return None, None, None
        athlete_team_id = None if athlete.team is None else athlete.team.canonical_id
        return athlete.canonical_id, athlete.name, athlete_team_id

    @staticmethod
    def _advance_latest(
        connection: Any,
        observation_rows: list[dict[str, Any]],
        *,
        provider: str,
        season: str,
        query_key: str,
        generation_id: str,
    ) -> None:
        table = LatestPlayerProjection.__table__
        connection.execute(
            delete(table).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
            )
        )
        materialized_references: set[str] = set()
        for row in observation_rows:
            if not row["targetable"]:
                continue
            reference = str(row["market_reference"])
            if reference in materialized_references:
                continue
            materialized_references.add(reference)
            connection.execute(
                insert(table).values(
                    provider=row["provider"],
                    season=season,
                    query_key=query_key,
                    canonical_game_id=row["canonical_game_id"],
                    canonical_player_id=row["canonical_player_id"],
                    market_reference=row["market_reference"],
                    observation_id=row["observation_id"],
                    generation_id=generation_id,
                    canonical_team_id=row["canonical_team_id"],
                    canonical_player_name=row["canonical_player_name"],
                    canonical_statistic_id=row["canonical_statistic_id"],
                    market_category=row["market_category"],
                    observed_at=row["observed_at"],
                    confirmed_at=row["observed_at"],
                )
            )

    @staticmethod
    def _plan_partial_transition(
        connection: Any,
        observation_rows: list[dict[str, Any]],
        *,
        provider: str,
        season: str,
        query_key: str,
    ) -> _PartialLatestTransition:
        """Plan one canonical partial Latest transition and its checksum."""

        table = LatestPlayerProjection.__table__
        current = connection.execute(
            select(
                table.c.market_reference,
                table.c.canonical_game_id,
                table.c.canonical_player_id,
                table.c.canonical_player_name,
                table.c.canonical_team_id,
                table.c.canonical_statistic_id,
                table.c.market_category,
            ).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
            )
        ).mappings()
        state = {
            str(row["market_reference"]): dict(row)
            for row in current
        }
        references = tuple(
            sorted({str(row["market_reference"]) for row in observation_rows})
        )
        for reference in references:
            state.pop(reference, None)
        selected: dict[str, dict[str, Any]] = {}
        for row in observation_rows:
            reference = str(row["market_reference"])
            if row["targetable"] and reference not in selected:
                selected[reference] = row
                state[reference] = {
                    "market_reference": reference,
                    "canonical_game_id": row["canonical_game_id"],
                    "canonical_player_id": row["canonical_player_id"],
                    "canonical_player_name": row["canonical_player_name"],
                    "canonical_team_id": row["canonical_team_id"],
                    "canonical_statistic_id": row["canonical_statistic_id"],
                    "market_category": row["market_category"],
                }
        checksum = sha256(
            json.dumps(
                [state[key] for key in sorted(state)],
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return _PartialLatestTransition(
            references=references,
            selected_rows=tuple(selected[key] for key in sorted(selected)),
            checksum=checksum,
        )

    @staticmethod
    def _apply_partial_transition(
        connection: Any,
        transition: _PartialLatestTransition,
        *,
        provider: str,
        season: str,
        query_key: str,
        generation_id: str,
    ) -> None:
        """Apply only explicitly observed partial offerings and carry omissions."""

        table = LatestPlayerProjection.__table__
        scope = (
            table.c.provider == provider,
            table.c.season == season,
            table.c.query_key == query_key,
        )
        if transition.references:
            connection.execute(
                delete(table).where(
                    *scope,
                    table.c.market_reference.in_(transition.references),
                )
            )
        connection.execute(update(table).where(*scope).values(generation_id=generation_id))
        for row in transition.selected_rows:
            connection.execute(
                insert(table).values(
                    provider=row["provider"],
                    season=season,
                    query_key=query_key,
                    canonical_game_id=row["canonical_game_id"],
                    canonical_player_id=row["canonical_player_id"],
                    market_reference=row["market_reference"],
                    observation_id=row["observation_id"],
                    generation_id=generation_id,
                    canonical_team_id=row["canonical_team_id"],
                    canonical_player_name=row["canonical_player_name"],
                    canonical_statistic_id=row["canonical_statistic_id"],
                    market_category=row["market_category"],
                    observed_at=row["observed_at"],
                    confirmed_at=row["observed_at"],
                )
            )


    @staticmethod
    def _confirm_latest(
        connection: Any,
        observation_rows: list[dict[str, Any]],
        *,
        provider: str,
        season: str,
        query_key: str,
        confirmed_at: datetime,
        complete: bool,
    ) -> None:
        table = LatestPlayerProjection.__table__
        predicates = [
            table.c.provider == provider,
            table.c.season == season,
            table.c.query_key == query_key,
        ]
        if not complete:
            references = tuple(
                {str(row["market_reference"]) for row in observation_rows}
            )
            if not references:
                return
            predicates.append(table.c.market_reference.in_(references))
        predicates.append(table.c.confirmed_at < confirmed_at)
        connection.execute(update(table).where(*predicates).values(confirmed_at=confirmed_at))


class LatestProjectionPlayerPoolReader:
    """Read the current Latest Player Projections; never call a provider."""

    def __init__(
        self,
        engine: Engine,
        scope: ProjectionArchiveReadScope | Iterable[ProjectionArchiveReadScope],
        *,
        clock: Callable[[], datetime] | None = None,
        required_providers: Iterable[str] | None = None,
        live_max_age: timedelta = PROJECTION_LIVE_MAX_AGE,
        failure_fallback_max_age: timedelta = PROJECTION_FAILURE_FALLBACK_MAX_AGE,
    ) -> None:
        scopes = (
            (scope,)
            if isinstance(scope, ProjectionArchiveReadScope)
            else tuple(scope)
        )
        if not scopes:
            raise ValueError("projection archive reader requires at least one scope")
        if len({(item.query.season, item.query_key) for item in scopes}) != 1:
            raise ValueError("projection archive reader scopes must share one query")
        if len({item.provider for item in scopes}) != len(scopes):
            raise ValueError("projection archive reader provider scopes must be unique")
        if live_max_age <= timedelta(0) or failure_fallback_max_age < live_max_age:
            raise ValueError("projection archive freshness windows are invalid")
        self.engine = engine
        self.scopes = tuple(sorted(scopes, key=lambda item: item.provider))
        self.scope = self.scopes[0]
        required = (
            {item.provider for item in self.scopes}
            if required_providers is None
            else {str(provider).strip().casefold() for provider in required_providers}
        )
        if not required <= {item.provider for item in self.scopes}:
            raise ValueError("required projection providers must have read scopes")
        self.required_providers = frozenset(required)
        self.clock = clock
        self.live_max_age = live_max_age
        self.failure_fallback_max_age = failure_fallback_max_age

    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool:
        return self.get_pool(season=season, game_ids=(game_id,))

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        if season != self.scope.query.season:
            raise ValueError("projection archive read season is outside its scope")
        requested_games = tuple(sorted({str(game_id) for game_id in game_ids}))
        if not requested_games:
            return PlayerPool((), {}, PlayerPool.missing_projection_freshness(), {})
        table = LatestPlayerProjection.__table__
        poll_table = ProviderPoll.__table__
        providers_in_scope = tuple(item.provider for item in self.scopes)
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ"
                )
            with connection.begin():
                rows = (
                    connection.execute(
                        select(table).where(
                            table.c.season == season,
                            table.c.provider.in_(providers_in_scope),
                            table.c.query_key == self.scope.query_key,
                            table.c.canonical_game_id.in_(requested_games),
                        ).order_by(
                            table.c.provider,
                            table.c.canonical_player_id,
                            table.c.market_reference,
                        )
                    )
                    .mappings()
                    .all()
                )
                polls = (
                    connection.execute(
                        select(
                            poll_table.c.provider,
                            poll_table.c.outcome,
                            poll_table.c.promoted,
                            poll_table.c.completed_at,
                        ).where(
                            poll_table.c.season == season,
                            poll_table.c.provider.in_(providers_in_scope),
                            poll_table.c.query_key == self.scope.query_key,
                        ).order_by(
                            poll_table.c.provider,
                            poll_table.c.completed_at.desc(),
                            poll_table.c.poll_id.desc(),
                        )
                    )
                    .mappings()
                    .all()
                )

        latest_outcomes: dict[str, str] = {}
        for poll in polls:
            if poll["outcome"] == "failed" or poll["promoted"]:
                latest_outcomes.setdefault(
                    str(poll["provider"]), str(poll["outcome"])
                )
        now = (
            assume_utc(self.clock())
            if self.clock is not None
            else max(
                (assume_utc(row["confirmed_at"]) for row in rows),
                default=datetime.now(timezone.utc),
            )
        )
        provider_statuses: dict[str, str] = {}
        eligible_rows: list[Any] = []
        for row in rows:
            provider = str(row["provider"])
            outcome = latest_outcomes.get(provider)
            age = now - assume_utc(row["confirmed_at"])
            status = None
            if outcome == "failed":
                failure_age = (
                    self.failure_fallback_max_age
                    if provider in self.required_providers
                    else self.live_max_age
                )
                if timedelta(0) <= age <= failure_age:
                    status = "stale-served"
            elif outcome != "failed" and timedelta(0) <= age <= self.live_max_age:
                status = "fresh"
            if status is not None:
                provider_statuses[provider] = status
                eligible_rows.append(row)
        rows = eligible_rows

        game_states = {
            game_id: self._state_for_rows(
                [row for row in rows if row["canonical_game_id"] == game_id]
            )
            for game_id in requested_games
        }
        if not rows:
            return PlayerPool(
                (),
                {},
                PlayerPool.missing_projection_freshness(),
                game_states,
            )

        contributions: dict[int, dict[str, Any]] = {}
        for row in rows:
            player_id = int(row["canonical_player_id"])
            entry = contributions.setdefault(
                player_id,
                {
                    "name": str(row["canonical_player_name"]),
                    "team_id": int(row["canonical_team_id"]),
                    "providers": {},
                },
            )
            entry["providers"].setdefault(str(row["provider"]), set()).add(
                str(row["market_category"])
            )
        players = tuple(
            PoolPlayer(
                canonical_player_id=player_id,
                name=entry["name"],
                team_id=entry["team_id"],
                market_categories=tuple(
                    sorted(
                        {
                            category
                            for categories in entry["providers"].values()
                            for category in categories
                        }
                    )
                ),
                provenance={
                    provider: tuple(sorted(categories))
                    for provider, categories in sorted(entry["providers"].items())
                },
            )
            for player_id, entry in sorted(contributions.items())
        )
        team_counts: dict[int, int] = {}
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
        observed_at = min(assume_utc(row["confirmed_at"]) for row in rows)
        provider_observed_at: dict[str, datetime] = {}
        for row in rows:
            provider = str(row["provider"])
            row_observed_at = assume_utc(row["confirmed_at"])
            provider_observed_at[provider] = min(
                provider_observed_at.get(provider, row_observed_at),
                row_observed_at,
            )
        providers = {
            provider: {
                "status": provider_statuses[provider],
                "retrieved_at": provider_observed_at[provider].isoformat(),
            }
            for provider in sorted(provider_observed_at)
        }
        statuses = set(provider_statuses.values())
        missing_provider = not self.required_providers <= set(provider_statuses)
        if any(state["state"] == "missing" for state in game_states.values()) or missing_provider or len(statuses) > 1:
            aggregate_status = "partial"
        elif statuses == {"stale-served"}:
            aggregate_status = "stale-served"
        else:
            aggregate_status = "fresh"
        freshness = {
            "status": aggregate_status,
            "state": "live",
            "observed_at": observed_at.isoformat(),
            "retrieved_at": observed_at.isoformat(),
            "providers": providers,
        }
        return PlayerPool(players, team_counts, freshness, game_states)

    @staticmethod
    def _state_for_rows(rows: list[Any]) -> dict[str, Any]:
        if not rows:
            return {"state": "missing", "observed_at": None}
        observed_at = min(assume_utc(row["confirmed_at"]) for row in rows)
        return {"state": "live", "observed_at": observed_at.isoformat()}


class ProjectionSelectionPlayerPoolReader:
    """Translate missing archive evidence to Selection's unavailable contract."""

    def __init__(self, reader: LatestProjectionPlayerPoolReader) -> None:
        self.reader = reader

    def get_pool_for_game(
        self,
        *,
        season: str,
        game_id: str,
    ) -> PlayerPool | None:
        pool = self.reader.get_pool_for_game(season=season, game_id=game_id)
        if pool.freshness.get("state") == "missing":
            return None
        return pool


class ProjectionRecordingService:
    """Record provider outcomes only into the application's selected read scopes."""

    def __init__(
        self,
        archive: ProjectionArchive,
        scope: ProjectionArchiveReadScope | Iterable[ProjectionArchiveReadScope],
    ) -> None:
        self.archive = archive
        scopes = (
            (scope,)
            if isinstance(scope, ProjectionArchiveReadScope)
            else tuple(scope)
        )
        if not scopes:
            raise ValueError("projection recorder requires at least one scope")
        self.scopes = {item.provider: item for item in scopes}
        self.scope = scopes[0]

    def _scope_for(self, provider: str, query: NBAMarketQuery) -> ProjectionArchiveReadScope:
        normalized_provider = provider.strip().casefold()
        scope = self.scopes.get(normalized_provider)
        if scope is None:
            raise ValueError(
                "projection snapshot provider is outside the configured read scope: "
                f"received {normalized_provider!r}"
            )
        if _query_key(query) != scope.query_key:
            raise ValueError(
                "projection snapshot query is outside the configured read scope"
            )
        return scope

    def record_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
    ) -> ProjectionArchiveResult:
        require_projection_archive_schema(self.archive.engine)
        self._scope_for(snapshot.provider, query)
        return self.archive.ingest_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
        )

    def record_complete_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
    ) -> ProjectionArchiveResult:
        require_projection_archive_schema(self.archive.engine)
        if snapshot.status is not SnapshotStatus.COMPLETE:
            raise ValueError(
                "only complete provider snapshots may enter this archive path"
            )
        return self.record_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
        )

    def record_failed_poll(
        self,
        *,
        provider: str,
        query: NBAMarketQuery,
        completed_at: datetime | None = None,
        poll_started_at: datetime | None = None,
        failure_reason: str,
    ) -> ProjectionPollResult:
        require_projection_archive_schema(self.archive.engine)
        self._scope_for(provider, query)
        return self.archive.record_failed_poll(
            provider=provider,
            query=query,
            completed_at=completed_at,
            poll_started_at=poll_started_at,
            failure_reason=failure_reason,
        )


__all__ = [
    "LatestProjectionPlayerPoolReader",
    "ProjectionArchive",
    "ProjectionArchiveReadScope",
    "ProjectionArchiveResult",
    "ProjectionPollResult",
    "ProjectionRecordingService",
    "ProjectionSelectionPlayerPoolReader",
    "require_projection_archive_schema",
]
