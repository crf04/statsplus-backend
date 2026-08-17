"""Durable normalized projection evidence and its database-first live read model."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
import threading
from typing import Any, Callable

from sqlalchemy import case, delete, func, inspect, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.config.settings import ConfigurationError
from app.domain.comparisons import market_reference
from app.domain.freshness import (
    exact_age_seconds,
    exact_seconds,
    exact_timedelta,
    within_max_age,
)
from app.domain.market_content import market_evidence_key
from app.domain.statistics import MatchState, ScoringPeriod
from app.domain.utc import assume_utc, utc_now
from app.models.projection_archive import (
    LatestPlayerProjection,
    MaterializationOutcome,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPollOutcome,
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


def _projection_provider_status(
    *,
    now: datetime,
    observed_at: datetime,
    health_outcome: ProviderPollOutcome | None,
    success_is_current: bool,
    required: bool,
    live_max_age_seconds: Decimal,
    failure_fallback_max_age_seconds: Decimal,
) -> str | None:
    """Classify row-bearing and Complete-empty evidence at one age boundary."""

    elapsed = exact_seconds(now - assume_utc(observed_at))
    if elapsed < 0:
        return None
    age = exact_age_seconds(elapsed, field="projection evidence age")
    if health_outcome is ProviderPollOutcome.FAILED:
        maximum_age = (
            failure_fallback_max_age_seconds
            if required
            else live_max_age_seconds
        )
        return "stale-served" if within_max_age(age, maximum_age) else None
    if success_is_current and within_max_age(age, live_max_age_seconds):
        return "fresh"
    return None


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
    materialization_outcome: "MaterializationOutcome"


@dataclass(frozen=True, slots=True)
class ProjectionPollResult:
    """Observable result of accepting a poll without usable snapshot evidence."""

    poll_id: str
    outcome: "ProviderPollOutcome"


@dataclass(slots=True)
class _PreparedProjectionIngestion:
    snapshot: ProviderSnapshot
    query: NBAMarketQuery
    accepted_at: datetime
    poll_started_at: datetime | None
    query_key: str
    observation_count: int
    evidence_document: str
    evidence_checksum: str
    content_checksum: str
    evidence_snapshot_id: str
    observation_rows: list[dict[str, Any]]
    materialization_checksum: str
    poll_id: str


@dataclass(frozen=True, slots=True)
class _ProjectionTransitionPlan:
    current: Any | None
    partial_transition: "_PartialLatestTransition | None"
    materialization_checksum: str
    outcome: MaterializationOutcome


@dataclass(frozen=True, slots=True)
class _ProjectionPoolRead:
    rows: tuple[Any, ...]
    polls: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _LatestProviderPolls:
    health: dict[str, Any]
    promoted: dict[str, Any]
    complete_empty: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _EligibleProjectionEvidence:
    rows: tuple[Any, ...]
    provider_statuses: dict[str, str]
    empty_provider_observed_at: dict[str, datetime]


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


def _canonical_snapshot(snapshot: ProviderSnapshot) -> ProviderSnapshot:
    """Order normalized offerings by identity and retained source evidence."""

    return replace(
        snapshot,
        markets=tuple(
            sorted(
                snapshot.markets,
                key=lambda market: (
                    market_reference(market),
                    market_evidence_key(replace(market, statistic_match=None)),
                ),
            )
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
    """Accept a Complete or Partial snapshot as one atomic archive generation."""

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

        prepared = self._prepare_ingestion(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
        )
        return self._ingest_prepared(prepared)

    def _prepare_ingestion(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None,
        poll_started_at: datetime | None,
    ) -> _PreparedProjectionIngestion:
        """Validate and canonicalize one snapshot without touching the database."""

        if not isinstance(snapshot, ProviderSnapshot):
            raise TypeError("snapshot must be ProviderSnapshot")
        if not isinstance(query, NBAMarketQuery) or query.season is None:
            raise ValueError("projection archive queries require a canonical season")
        if len(snapshot.markets) > self.max_markets:
            raise ValueError("projection snapshot exceeds the configured market limit")
        snapshot = _canonical_snapshot(snapshot)

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
            snapshot.provider,
            query_key,
            assume_utc(snapshot.retrieved_at).isoformat(),
            checksum,
        )
        if accepted < assume_utc(snapshot.retrieved_at):
            raise ValueError("projection snapshot cannot be accepted before retrieval")
        return _PreparedProjectionIngestion(
            snapshot=snapshot,
            query=query,
            accepted_at=accepted,
            poll_started_at=poll_started,
            query_key=query_key,
            observation_count=observation_count,
            evidence_document=document,
            evidence_checksum=checksum,
            content_checksum=content_checksum,
            evidence_snapshot_id=evidence_snapshot_id,
            observation_rows=observation_rows,
            materialization_checksum=materialization_checksum,
            poll_id=poll_id,
        )

    def _ingest_prepared(
        self,
        prepared: _PreparedProjectionIngestion,
    ) -> ProjectionArchiveResult:
        """Plan and persist one already validated ingestion under the scope fence."""

        with self._scope_transaction(
            prepared.snapshot.provider,
            prepared.query.season,
            prepared.query_key,
        ) as connection:
            replayed = self._replayed_result(
                connection,
                prepared.poll_id,
                provider=prepared.snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
                evidence_checksum=prepared.evidence_checksum,
            )
            if replayed is not None:
                return replayed
            plan = self._plan_transition(connection, prepared)
            unchanged = self._record_current_confirmation(connection, prepared, plan)
            if unchanged is not None:
                return unchanged
            return self._persist_transition(connection, prepared, plan)

    def _record_current_confirmation(
        self,
        connection: Any,
        prepared: _PreparedProjectionIngestion,
        plan: _ProjectionTransitionPlan,
    ) -> ProjectionArchiveResult | None:
        current = plan.current
        if (
            current is None
            or current["content_checksum"] != prepared.content_checksum
            or current["materialization_checksum"] != plan.materialization_checksum
        ):
            return None
        snapshot = prepared.snapshot
        promoted = plan.outcome is MaterializationOutcome.ADVANCED
        self._record_poll(
            connection,
            poll_id=prepared.poll_id,
            snapshot=snapshot,
            season=prepared.query.season,
            query_key=prepared.query_key,
            started_at=prepared.poll_started_at,
            completed_at=prepared.accepted_at,
            outcome=ProviderPollOutcome.UNCHANGED,
            promoted=promoted,
            snapshot_id=str(current["snapshot_id"]),
            generation_id=str(current["generation_id"]),
            observation_count=prepared.observation_count,
        )
        if promoted:
            self._confirm_latest(
                connection,
                prepared.observation_rows,
                provider=snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
                confirmed_at=snapshot.retrieved_at,
                complete=snapshot.status is SnapshotStatus.COMPLETE,
            )
        return ProjectionArchiveResult(
            snapshot_id=str(current["snapshot_id"]),
            generation_id=str(current["generation_id"]),
            changed=False,
            observation_count=prepared.observation_count,
            materialization_outcome=MaterializationOutcome.UNCHANGED,
        )

    def _persist_transition(
        self,
        connection: Any,
        prepared: _PreparedProjectionIngestion,
        plan: _ProjectionTransitionPlan,
    ) -> ProjectionArchiveResult:
        """Persist one planned non-current transition inside the caller's fence."""

        snapshot = prepared.snapshot
        current = plan.current
        provider_content_unchanged = (
            current is not None
            and current["content_checksum"] == prepared.content_checksum
        )
        snapshot_table = ProjectionProviderSnapshot.__table__
        existing = connection.execute(
            select(snapshot_table.c.snapshot_id).where(
                snapshot_table.c.checksum == prepared.evidence_checksum
            )
        ).scalar_one_or_none()
        snapshot_id = (
            str(current["snapshot_id"])
            if provider_content_unchanged
            else str(existing or prepared.evidence_snapshot_id)
        )
        generation_id = _digest(
            "gen",
            snapshot_id,
            plan.materialization_checksum,
            assume_utc(snapshot.retrieved_at).isoformat(),
        )
        if connection.execute(
            select(ProjectionMaterializationGeneration.outcome).where(
                ProjectionMaterializationGeneration.generation_id == generation_id
            )
        ).scalar_one_or_none() is not None:
            self._record_poll(
                connection,
                poll_id=prepared.poll_id,
                snapshot=snapshot,
                season=prepared.query.season,
                query_key=prepared.query_key,
                started_at=prepared.poll_started_at,
                completed_at=prepared.accepted_at,
                outcome=ProviderPollOutcome.UNCHANGED,
                promoted=False,
                snapshot_id=snapshot_id,
                generation_id=generation_id,
                observation_count=prepared.observation_count,
            )
            return ProjectionArchiveResult(
                snapshot_id=snapshot_id,
                generation_id=generation_id,
                changed=False,
                observation_count=prepared.observation_count,
                materialization_outcome=MaterializationOutcome.UNCHANGED,
            )

        if existing is None and not provider_content_unchanged:
            connection.execute(insert(snapshot_table).values(
                snapshot_id=snapshot_id,
                provider=snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
                contract_version=snapshot.contract_version,
                snapshot_status=snapshot.status.value,
                retrieved_at=snapshot.retrieved_at,
                accepted_at=prepared.accepted_at,
                checksum=prepared.evidence_checksum,
                content_checksum=prepared.content_checksum,
                evidence_document=prepared.evidence_document,
            ))
        for row in prepared.observation_rows:
            row["snapshot_id"] = snapshot_id
            row["generation_id"] = generation_id
            row["source_poll_id"] = prepared.poll_id
            row["observation_id"] = _digest(
                "obs", generation_id, row["ordinal"], row["market_reference"]
            )
        poll_outcome = (
            ProviderPollOutcome.REMATERIALIZED
            if provider_content_unchanged
            else (
                ProviderPollOutcome.PARTIAL
                if snapshot.status is SnapshotStatus.PARTIAL
                else ProviderPollOutcome.CHANGED
            )
        )
        self._record_poll(
            connection,
            poll_id=prepared.poll_id,
            snapshot=snapshot,
            season=prepared.query.season,
            query_key=prepared.query_key,
            started_at=prepared.poll_started_at,
            completed_at=prepared.accepted_at,
            outcome=poll_outcome,
            promoted=plan.outcome is MaterializationOutcome.ADVANCED,
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            observation_count=prepared.observation_count,
        )
        connection.execute(
            insert(ProjectionMaterializationGeneration.__table__).values(
                generation_id=generation_id,
                provider=snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
                snapshot_id=snapshot_id,
                source_poll_id=prepared.poll_id,
                created_at=prepared.accepted_at,
                retrieved_at=snapshot.retrieved_at,
                materialization_checksum=plan.materialization_checksum,
                outcome=plan.outcome.value,
            )
        )
        if prepared.observation_rows:
            connection.execute(
                insert(ProjectionObservation.__table__), prepared.observation_rows
            )
        if plan.outcome is MaterializationOutcome.ADVANCED:
            if snapshot.status is SnapshotStatus.COMPLETE:
                self._advance_latest(
                    connection,
                    prepared.observation_rows,
                    provider=snapshot.provider,
                    season=prepared.query.season,
                    query_key=prepared.query_key,
                    generation_id=generation_id,
                )
            else:
                assert plan.partial_transition is not None
                self._apply_partial_transition(
                    connection,
                    plan.partial_transition,
                    provider=snapshot.provider,
                    season=prepared.query.season,
                    query_key=prepared.query_key,
                    generation_id=generation_id,
                )
        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            changed=not provider_content_unchanged,
            observation_count=prepared.observation_count,
            materialization_outcome=plan.outcome,
        )

    def _plan_transition(
        self,
        connection: Any,
        prepared: _PreparedProjectionIngestion,
    ) -> _ProjectionTransitionPlan:
        """Derive one canonical materialization decision under the scope fence."""

        snapshot = prepared.snapshot
        current = self._current_materialization(
            connection,
            provider=snapshot.provider,
            season=prepared.query.season,
            query_key=prepared.query_key,
        )
        promoted_through = self._promotion_fence(
            connection,
            provider=snapshot.provider,
            season=prepared.query.season,
            query_key=prepared.query_key,
        )
        failed_through = self._failure_attempt_fence(
            connection,
            provider=snapshot.provider,
            season=prepared.query.season,
            query_key=prepared.query_key,
        )
        partial_transition = None
        materialization_checksum = prepared.materialization_checksum
        if snapshot.status is SnapshotStatus.PARTIAL:
            partial_transition = self._plan_partial_transition(
                connection,
                prepared.observation_rows,
                provider=snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
            )
            materialization_checksum = partial_transition.checksum
        return _ProjectionTransitionPlan(
            current=current,
            partial_transition=partial_transition,
            materialization_checksum=materialization_checksum,
            outcome=self._materialization_outcome(
                current,
                incoming_retrieved_at=snapshot.retrieved_at,
                promoted_through=promoted_through,
                failed_through=failed_through,
            ),
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
                        outcome=ProviderPollOutcome.FAILED.value,
                        promoted=False,
                        failure_reason=normalized_reason,
                        snapshot_id=None,
                        generation_id=None,
                        observation_count=0,
                    )
                )
        return ProjectionPollResult(
            poll_id=poll_id,
            outcome=ProviderPollOutcome.FAILED,
        )

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
        *,
        provider: str,
        season: str,
        query_key: str,
        evidence_checksum: str,
    ) -> ProjectionArchiveResult | None:
        poll_table = ProviderPoll.__table__
        generation_table = ProjectionMaterializationGeneration.__table__
        snapshot_table = ProjectionProviderSnapshot.__table__
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
            poll = (
                connection.execute(
                    select(
                        poll_table.c.snapshot_id,
                        poll_table.c.generation_id,
                        poll_table.c.outcome,
                        poll_table.c.observation_count,
                    )
                    .select_from(
                        poll_table.join(
                            snapshot_table,
                            poll_table.c.snapshot_id == snapshot_table.c.snapshot_id,
                        )
                    )
                    .where(
                        poll_table.c.provider == provider,
                        poll_table.c.season == season,
                        poll_table.c.query_key == query_key,
                        snapshot_table.c.checksum == evidence_checksum,
                    )
                    .order_by(
                        poll_table.c.completed_at,
                        poll_table.c.poll_id,
                    )
                    .limit(1)
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
        poll_outcome = ProviderPollOutcome(str(poll["outcome"]))
        changed = poll_outcome in {
            ProviderPollOutcome.CHANGED,
            ProviderPollOutcome.PARTIAL,
        }
        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=str(generation["generation_id"]),
            changed=changed,
            observation_count=int(poll["observation_count"]),
            materialization_outcome=(
                MaterializationOutcome(str(generation["outcome"]))
                if poll_outcome
                in {
                    ProviderPollOutcome.CHANGED,
                    ProviderPollOutcome.PARTIAL,
                    ProviderPollOutcome.REMATERIALIZED,
                }
                else MaterializationOutcome.UNCHANGED
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
                    generation_table.c.outcome == MaterializationOutcome.ADVANCED.value,
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
        failed_through: datetime | None = None,
    ) -> MaterializationOutcome:
        if current is None and promoted_through is None and failed_through is None:
            return MaterializationOutcome.ADVANCED
        incoming = assume_utc(incoming_retrieved_at)
        fences = []
        if current is not None:
            fences.append(assume_utc(current["retrieved_at"]))
        if promoted_through is not None:
            fences.append(assume_utc(promoted_through))
        if failed_through is not None:
            fences.append(assume_utc(failed_through))
        current_retrieved_at = max(fences)
        if incoming > current_retrieved_at:
            return MaterializationOutcome.ADVANCED
        if incoming == current_retrieved_at:
            return MaterializationOutcome.SAME_TIME_NOT_PROMOTED
        return MaterializationOutcome.OLDER_NOT_PROMOTED

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
    def _failure_attempt_fence(
        connection: Any,
        *,
        provider: str,
        season: str,
        query_key: str,
    ) -> datetime | None:
        table = ProviderPoll.__table__
        return connection.execute(
            select(func.max(func.coalesce(table.c.started_at, table.c.completed_at))).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
                table.c.outcome == ProviderPollOutcome.FAILED.value,
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
        outcome: ProviderPollOutcome,
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
                outcome=outcome.value,
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
            ProjectionArchive._insert_latest_row(
                connection,
                row,
                season=season,
                query_key=query_key,
                generation_id=generation_id,
            )

    @staticmethod
    def _insert_latest_row(
        connection: Any,
        row: dict[str, Any],
        *,
        season: str,
        query_key: str,
        generation_id: str,
    ) -> None:
        connection.execute(
            insert(LatestPlayerProjection.__table__).values(
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
            ProjectionArchive._insert_latest_row(
                connection,
                row,
                season=season,
                query_key=query_key,
                generation_id=generation_id,
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
        try:
            live_max_age = exact_timedelta(
                exact_seconds(live_max_age),
                field="PROJECTION_LIVE_MAX_AGE",
            )
            failure_fallback_max_age = exact_timedelta(
                exact_seconds(failure_fallback_max_age),
                field="PROJECTION_FAILURE_FALLBACK_MAX_AGE",
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        if failure_fallback_max_age < live_max_age:
            raise ConfigurationError(
                "PROJECTION_LIVE_MAX_AGE can never exceed "
                "PROJECTION_FAILURE_FALLBACK_MAX_AGE"
            )
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
        self.clock = clock or utc_now
        self.live_max_age = live_max_age
        self.failure_fallback_max_age = failure_fallback_max_age
        self._live_max_age_seconds = exact_seconds(live_max_age)
        self._failure_fallback_max_age_seconds = exact_seconds(
            failure_fallback_max_age
        )

    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool:
        return self.get_pool(season=season, game_ids=(game_id,))

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        if season != self.scope.query.season:
            raise ValueError("projection archive read season is outside its scope")
        requested_games = tuple(sorted({str(game_id) for game_id in game_ids}))
        if not requested_games:
            return PlayerPool((), {}, PlayerPool.missing_projection_freshness(), {})
        read = self._read_pool_state(season, requested_games)
        poll_state = self._latest_provider_polls(read.polls)
        evidence = self._classify_eligible_evidence(
            read.rows,
            poll_state,
            now=assume_utc(self.clock()),
        )
        rows = list(evidence.rows)
        empty_provider_observed_at = evidence.empty_provider_observed_at

        game_states = {
            game_id: self._state_for_rows(
                [row for row in rows if row["canonical_game_id"] == game_id]
            )
            for game_id in requested_games
        }
        empty_pool = self._empty_pool(
            game_states=game_states,
            evidence=evidence,
        )
        if empty_pool is not None:
            return empty_pool
        if not rows:
            empty_observed_at = min(empty_provider_observed_at.values()).isoformat()
            game_states = {
                game_id: {"state": "live", "observed_at": empty_observed_at}
                for game_id in requested_games
            }

        players = self._players_from_rows(rows)
        team_counts: dict[int, int] = {}
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
        freshness = self._pool_freshness(
            requested_games=requested_games,
            game_states=game_states,
            rows=rows,
            evidence=evidence,
        )
        return PlayerPool(players, team_counts, freshness, game_states)

    def _empty_pool(
        self,
        *,
        game_states: dict[str, dict[str, Any]],
        evidence: _EligibleProjectionEvidence,
    ) -> PlayerPool | None:
        """Shape missing and mixed Complete-empty states, if one is terminal."""

        if evidence.rows:
            return None
        observed = evidence.empty_provider_observed_at
        if observed and self.required_providers <= set(observed):
            return None
        if observed:
            providers = {
                provider: {
                    "status": evidence.provider_statuses[provider],
                    "retrieved_at": observed_at.isoformat(),
                }
                for provider, observed_at in sorted(observed.items())
            }
            for provider in sorted(self.required_providers - set(providers)):
                providers[provider] = {"status": "missing", "retrieved_at": None}
            freshness = {
                "state": "missing",
                "observed_at": None,
                "retrieved_at": None,
                "providers": dict(sorted(providers.items())),
            }
        else:
            freshness = PlayerPool.missing_projection_freshness()
            freshness["providers"] = {
                provider: {"status": "missing", "retrieved_at": None}
                for provider in sorted(self.required_providers)
            }
        return PlayerPool((), {}, freshness, game_states)

    @staticmethod
    def _players_from_rows(rows: list[Any]) -> tuple[PoolPlayer, ...]:
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
        return tuple(
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

    def _pool_freshness(
        self,
        *,
        requested_games: tuple[str, ...],
        game_states: dict[str, dict[str, Any]],
        rows: list[Any],
        evidence: _EligibleProjectionEvidence,
    ) -> dict[str, Any]:
        provider_observed_at = dict(evidence.empty_provider_observed_at)
        for row in rows:
            provider = str(row["provider"])
            row_observed_at = assume_utc(row["confirmed_at"])
            provider_observed_at[provider] = min(
                provider_observed_at.get(provider, row_observed_at),
                row_observed_at,
            )
        observed_at = min(provider_observed_at.values())
        providers = {
            provider: {
                "status": evidence.provider_statuses[provider],
                "retrieved_at": provider_observed_at[provider].isoformat(),
            }
            for provider in sorted(provider_observed_at)
        }
        for provider in sorted(self.required_providers - set(providers)):
            providers[provider] = {"status": "missing", "retrieved_at": None}
        providers = dict(sorted(providers.items()))
        statuses = {provider["status"] for provider in providers.values()}
        freshness = {
            "state": "live",
            "observed_at": observed_at.isoformat(),
            "retrieved_at": observed_at.isoformat(),
            "providers": providers,
        }
        if (
            len(requested_games) > 1
            and any(state["state"] == "live" for state in game_states.values())
            and any(state["state"] == "missing" for state in game_states.values())
        ):
            freshness["status"] = "partial"
        elif statuses == {"stale-served"}:
            freshness["status"] = "stale-served"
        elif statuses == {"fresh"}:
            freshness["status"] = "fresh"
        return freshness

    def _read_pool_state(
        self,
        season: str,
        requested_games: tuple[str, ...],
    ) -> _ProjectionPoolRead:
        """Read Latest rows and poll health inside one database snapshot."""

        table = LatestPlayerProjection.__table__
        poll_table = ProviderPoll.__table__
        snapshot_table = ProjectionProviderSnapshot.__table__
        providers_in_scope = tuple(item.provider for item in self.scopes)
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ"
                )
            with connection.begin():
                rows = tuple(
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
                polls = tuple(
                    connection.execute(
                        select(
                            poll_table.c.poll_id,
                            poll_table.c.provider,
                            poll_table.c.outcome,
                            poll_table.c.promoted,
                            poll_table.c.completed_at,
                            poll_table.c.retrieved_at,
                            poll_table.c.observation_count,
                            snapshot_table.c.snapshot_status,
                        )
                        .select_from(
                            poll_table.outerjoin(
                                snapshot_table,
                                poll_table.c.snapshot_id == snapshot_table.c.snapshot_id,
                            )
                        )
                        .where(
                            poll_table.c.season == season,
                            poll_table.c.provider.in_(providers_in_scope),
                            poll_table.c.query_key == self.scope.query_key,
                        ).order_by(
                            poll_table.c.provider,
                            func.coalesce(
                                poll_table.c.retrieved_at,
                                poll_table.c.started_at,
                                poll_table.c.completed_at,
                            ).desc(),
                            case(
                                (
                                    poll_table.c.outcome
                                    == ProviderPollOutcome.FAILED.value,
                                    1,
                                ),
                                else_=0,
                            ).desc(),
                            poll_table.c.completed_at.desc(),
                            poll_table.c.poll_id.desc(),
                        )
                    )
                    .mappings()
                    .all()
                )
        return _ProjectionPoolRead(rows=rows, polls=polls)

    @staticmethod
    def _latest_provider_polls(polls: tuple[Any, ...]) -> _LatestProviderPolls:
        """Select provider health by provider-attempt chronology."""

        latest_health: dict[str, Any] = {}
        latest_promoted: dict[str, Any] = {}
        latest_empty_success: dict[str, Any] = {}
        for poll in polls:
            provider = str(poll["provider"])
            outcome = ProviderPollOutcome(str(poll["outcome"]))
            if outcome is ProviderPollOutcome.FAILED or poll["promoted"]:
                latest_health.setdefault(provider, poll)
            if poll["promoted"]:
                latest_promoted.setdefault(provider, poll)
            if (
                poll["promoted"]
                and latest_promoted[provider]["poll_id"] == poll["poll_id"]
                and poll["observation_count"] == 0
                and poll["snapshot_status"] == SnapshotStatus.COMPLETE.value
            ):
                latest_empty_success.setdefault(provider, poll)
        return _LatestProviderPolls(
            health=latest_health,
            promoted=latest_promoted,
            complete_empty=latest_empty_success,
        )

    def _classify_eligible_evidence(
        self,
        rows: tuple[Any, ...],
        polls: _LatestProviderPolls,
        *,
        now: datetime,
    ) -> _EligibleProjectionEvidence:
        """Apply the shared freshness policy to rows and Complete-empty evidence."""

        provider_statuses: dict[str, str] = {}
        eligible_rows: list[Any] = []
        for row in rows:
            provider = str(row["provider"])
            health = polls.health.get(provider)
            outcome = (
                None
                if health is None
                else ProviderPollOutcome(str(health["outcome"]))
            )
            status = _projection_provider_status(
                now=now,
                observed_at=row["confirmed_at"],
                health_outcome=outcome,
                success_is_current=outcome is not ProviderPollOutcome.FAILED,
                required=provider in self.required_providers,
                live_max_age_seconds=self._live_max_age_seconds,
                failure_fallback_max_age_seconds=(
                    self._failure_fallback_max_age_seconds
                ),
            )
            if status is not None:
                provider_statuses[provider] = status
                eligible_rows.append(row)

        empty_provider_observed_at: dict[str, datetime] = {}
        for provider, empty_poll in polls.complete_empty.items():
            health = polls.health.get(provider)
            if health is None:
                continue
            empty_retrieved_at = assume_utc(empty_poll["retrieved_at"])
            status = _projection_provider_status(
                now=now,
                observed_at=empty_retrieved_at,
                health_outcome=ProviderPollOutcome(str(health["outcome"])),
                success_is_current=health["poll_id"] == empty_poll["poll_id"],
                required=provider in self.required_providers,
                live_max_age_seconds=self._live_max_age_seconds,
                failure_fallback_max_age_seconds=(
                    self._failure_fallback_max_age_seconds
                ),
            )
            if status is not None:
                provider_statuses[provider] = status
                empty_provider_observed_at[provider] = empty_retrieved_at
        return _EligibleProjectionEvidence(
            rows=tuple(eligible_rows),
            provider_statuses=provider_statuses,
            empty_provider_observed_at=empty_provider_observed_at,
        )

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
    """Record provider outcomes only into explicitly enabled write scopes."""

    def __init__(
        self,
        archive: ProjectionArchive,
        scope: ProjectionArchiveReadScope | Iterable[ProjectionArchiveReadScope],
        *,
        default_scope: ProjectionArchiveReadScope | None = None,
    ) -> None:
        self.archive = archive
        scopes = (
            (scope,)
            if isinstance(scope, ProjectionArchiveReadScope)
            else tuple(scope)
        )
        self.scopes = {item.provider: item for item in scopes}
        self.default_scope = default_scope or (scopes[0] if scopes else None)
        self.scope = self.default_scope

    def _scope_for(self, provider: str, query: NBAMarketQuery) -> ProjectionArchiveReadScope:
        normalized_provider = provider.strip().casefold()
        scope = self.scopes.get(normalized_provider)
        if scope is None:
            raise ValueError(
                "projection snapshot provider is outside the configured recording scope: "
                f"received {normalized_provider!r}"
            )
        if _query_key(query) != scope.query_key:
            raise ValueError(
                "projection snapshot query is outside the configured recording scope"
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
        return self._record_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
            require_complete=False,
        )

    def _record_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None,
        poll_started_at: datetime | None,
        require_complete: bool,
    ) -> ProjectionArchiveResult:
        require_projection_archive_schema(self.archive.engine)
        if require_complete and snapshot.status is not SnapshotStatus.COMPLETE:
            raise ValueError(
                "only complete provider snapshots may enter this archive path"
            )
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
        return self._record_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
            require_complete=True,
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
