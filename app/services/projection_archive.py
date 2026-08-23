"""Durable normalized projection evidence and its database-first live read model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
import threading
from typing import Any, Callable

from sqlalchemy import and_, case, delete, func, inspect, insert, or_, select, update
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
from app.domain.nba_events import is_started_event
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.domain.utc import assume_utc, parse_utc_iso, utc_now
from app.models.projection_archive import (
    ClosingProjectionMembership,
    ClosingProjectionSet,
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
    TeamEvidence,
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
    "projection_closing_sets",
    "projection_closing_memberships",
)
PROJECTION_ARCHIVE_REQUIRED_COLUMNS = {
    "projection_archive_scope_locks": (
        "active_generation_id",
        "mapping_replayed_at",
        "mapping_replayed_retrieved_at",
    ),
    "projection_provider_polls": ("failure_reason", "promoted"),
    "projection_observations": (
        "source_observation_id",
        "source_ordinal",
        "athlete_provider_id",
        "event_provider_id",
        "statistic_provider_id",
        "statistic_provider_label",
        "resolution_state",
        "unresolved_identities",
    ),
    "projection_materialization_generations": ("is_replay",),
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
            "040_projection_archive, 041_projection_archive_transitions, "
            "044_projection_closing_sets, and 045_projection_mapping_replay; missing: "
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


@dataclass(frozen=True, slots=True)
class ClosingProjectionSetResult:
    """Observable result of one idempotent provider/game closing freeze."""

    closing_set_id: str
    provider: str
    season: str
    canonical_game_id: str
    started_at: datetime
    observation_count: int
    created: bool


@dataclass(frozen=True, slots=True)
class ProjectionReplayResult:
    """Result of replaying one governed mapping over archived evidence."""

    generation_id: str
    changed: bool
    observation_count: int
    materialization_outcome: "MaterializationOutcome"


@dataclass(frozen=True, slots=True)
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
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class _ProjectionPollWrite:
    poll_id: str
    provider: str
    season: str
    query_key: str
    started_at: datetime | None
    completed_at: datetime
    retrieved_at: datetime
    outcome: ProviderPollOutcome
    promoted: bool
    snapshot_id: str
    generation_id: str
    observation_count: int
    duration_ms: int | None

    @classmethod
    def from_ingestion(
        cls,
        prepared: _PreparedProjectionIngestion,
        *,
        outcome: ProviderPollOutcome,
        promoted: bool,
        snapshot_id: str,
        generation_id: str,
    ) -> "_ProjectionPollWrite":
        return cls(
            poll_id=prepared.poll_id,
            provider=prepared.snapshot.provider,
            season=prepared.query.season,
            query_key=prepared.query_key,
            started_at=prepared.poll_started_at,
            completed_at=prepared.accepted_at,
            retrieved_at=prepared.snapshot.retrieved_at,
            outcome=outcome,
            promoted=promoted,
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            observation_count=prepared.observation_count,
            duration_ms=prepared.duration_ms,
        )


@dataclass(frozen=True, slots=True)
class _ProjectionTransitionPlan:
    current: Any | None
    partial_transition: "_PartialLatestTransition | None"
    materialization_checksum: str
    outcome: MaterializationOutcome


@dataclass(frozen=True, slots=True)
class _ProjectionTransitionWrite:
    snapshot_id: str
    generation_id: str
    provider_content_unchanged: bool
    snapshot_exists: bool
    generation_exists: bool
    observation_rows: tuple[dict[str, Any], ...]


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
        return projection_query_key(self.query)


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


def _projection_closing_start_fence(
    event: Mapping[str, Any],
) -> datetime | None:
    """Return the durable governed start fence for a started NBA event."""

    if not is_started_event(event):
        return None
    for field in ("first_observed_started_at", "scheduled_at"):
        value = event.get(field)
        if value is None:
            continue
        try:
            return (
                assume_utc(value)
                if isinstance(value, datetime)
                else parse_utc_iso(str(value))
            )
        except (TypeError, ValueError):
            continue
    return None


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


def projection_query_key(query: NBAMarketQuery) -> str:
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
        self.statistic_catalog = statistic_catalog
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
        duration_ms: int | None = None,
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
            duration_ms=duration_ms,
        )

    def ingest_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> ProjectionArchiveResult:
        """Archive one Complete or Partial normalized provider observation."""

        prepared = self._prepare_ingestion(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
            duration_ms=duration_ms,
        )
        return self._ingest_prepared(prepared)

    def _prepare_ingestion(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None,
        poll_started_at: datetime | None,
        duration_ms: int | None,
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
        query_key = projection_query_key(query)
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
            duration_ms=self._normalize_duration_ms(duration_ms),
        )

    @staticmethod
    def _normalize_duration_ms(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("projection poll duration_ms must be a non-negative integer")
        return min(value, 7 * 24 * 60 * 60 * 1000)

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
            _ProjectionPollWrite.from_ingestion(
                prepared,
                outcome=ProviderPollOutcome.UNCHANGED,
                promoted=promoted,
                snapshot_id=str(current["snapshot_id"]),
                generation_id=str(current["generation_id"]),
            ),
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

        write = self._resolve_transition_write(connection, prepared, plan)
        if write.generation_exists:
            self._record_poll(
                connection,
                _ProjectionPollWrite.from_ingestion(
                    prepared,
                    outcome=ProviderPollOutcome.UNCHANGED,
                    promoted=False,
                    snapshot_id=write.snapshot_id,
                    generation_id=write.generation_id,
                ),
            )
            return self._transition_result(
                prepared,
                write,
                changed=False,
                outcome=MaterializationOutcome.UNCHANGED,
            )
        self._persist_generation_evidence(connection, prepared, plan, write)
        self._apply_transition_latest(connection, prepared, plan, write)
        return self._transition_result(
            prepared,
            write,
            changed=not write.provider_content_unchanged,
            outcome=plan.outcome,
        )

    @staticmethod
    def _resolve_transition_write(
        connection: Any,
        prepared: _PreparedProjectionIngestion,
        plan: _ProjectionTransitionPlan,
    ) -> _ProjectionTransitionWrite:
        """Resolve durable identities and immutable observation writes."""

        snapshot = prepared.snapshot
        provider_content_unchanged = (
            plan.current is not None
            and plan.current["content_checksum"] == prepared.content_checksum
        )
        snapshot_table = ProjectionProviderSnapshot.__table__
        existing = connection.execute(
            select(snapshot_table.c.snapshot_id).where(
                snapshot_table.c.checksum == prepared.evidence_checksum
            )
        ).scalar_one_or_none()
        snapshot_id = (
            str(plan.current["snapshot_id"])
            if provider_content_unchanged
            else str(existing or prepared.evidence_snapshot_id)
        )
        generation_id = _digest(
            "gen",
            snapshot_id,
            plan.materialization_checksum,
            assume_utc(snapshot.retrieved_at).isoformat(),
        )
        generation_exists = connection.execute(
            select(ProjectionMaterializationGeneration.outcome).where(
                ProjectionMaterializationGeneration.generation_id == generation_id
            )
        ).scalar_one_or_none() is not None
        observation_rows = []
        for row in prepared.observation_rows:
            observation_id = _digest(
                "obs", generation_id, row["ordinal"], row["market_reference"]
            )
            observation_rows.append(
                {
                    **row,
                    "snapshot_id": snapshot_id,
                    "generation_id": generation_id,
                    "source_poll_id": prepared.poll_id,
                    "source_observation_id": observation_id,
                    "source_ordinal": row["ordinal"],
                    "observation_id": observation_id,
                }
            )
        return _ProjectionTransitionWrite(
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            provider_content_unchanged=provider_content_unchanged,
            snapshot_exists=existing is not None,
            generation_exists=generation_exists,
            observation_rows=tuple(observation_rows),
        )

    def _persist_generation_evidence(
        self,
        connection: Any,
        prepared: _PreparedProjectionIngestion,
        plan: _ProjectionTransitionPlan,
        write: _ProjectionTransitionWrite,
    ) -> None:
        """Persist snapshot evidence, poll, generation, then observations."""

        snapshot = prepared.snapshot
        if not write.snapshot_exists and not write.provider_content_unchanged:
            connection.execute(
                insert(ProjectionProviderSnapshot.__table__).values(
                    snapshot_id=write.snapshot_id,
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
                )
            )
        outcome = (
            ProviderPollOutcome.REMATERIALIZED
            if write.provider_content_unchanged
            else (
                ProviderPollOutcome.PARTIAL
                if snapshot.status is SnapshotStatus.PARTIAL
                else ProviderPollOutcome.CHANGED
            )
        )
        self._record_poll(
            connection,
            _ProjectionPollWrite.from_ingestion(
                prepared,
                outcome=outcome,
                promoted=plan.outcome is MaterializationOutcome.ADVANCED,
                snapshot_id=write.snapshot_id,
                generation_id=write.generation_id,
            ),
        )
        connection.execute(
            insert(ProjectionMaterializationGeneration.__table__).values(
                generation_id=write.generation_id,
                provider=snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
                snapshot_id=write.snapshot_id,
                source_poll_id=prepared.poll_id,
                created_at=prepared.accepted_at,
                retrieved_at=snapshot.retrieved_at,
                materialization_checksum=plan.materialization_checksum,
                outcome=plan.outcome.value,
                is_replay=False,
            )
        )
        if write.observation_rows:
            connection.execute(
                insert(ProjectionObservation.__table__), write.observation_rows
            )

    def _apply_transition_latest(
        self,
        connection: Any,
        prepared: _PreparedProjectionIngestion,
        plan: _ProjectionTransitionPlan,
        write: _ProjectionTransitionWrite,
    ) -> None:
        """Apply one promoted generation to Latest after evidence is durable."""

        if plan.outcome is MaterializationOutcome.ADVANCED:
            if prepared.snapshot.status is SnapshotStatus.COMPLETE:
                self._advance_latest(
                    connection,
                    write.observation_rows,
                    provider=prepared.snapshot.provider,
                    season=prepared.query.season,
                    query_key=prepared.query_key,
                    generation_id=write.generation_id,
                )
            else:
                assert plan.partial_transition is not None
                decorated = {
                    int(row["ordinal"]): row for row in write.observation_rows
                }
                partial_transition = replace(
                    plan.partial_transition,
                    selected_rows=tuple(
                        decorated[int(row["ordinal"])]
                        for row in plan.partial_transition.selected_rows
                    ),
                )
                self._apply_partial_transition(
                    connection,
                    partial_transition,
                    provider=prepared.snapshot.provider,
                    season=prepared.query.season,
                    query_key=prepared.query_key,
                    generation_id=write.generation_id,
                )
            self._activate_generation(
                connection,
                provider=prepared.snapshot.provider,
                season=prepared.query.season,
                query_key=prepared.query_key,
                generation_id=write.generation_id,
            )

    @staticmethod
    def _transition_result(
        prepared: _PreparedProjectionIngestion,
        write: _ProjectionTransitionWrite,
        *,
        changed: bool,
        outcome: MaterializationOutcome,
    ) -> ProjectionArchiveResult:
        return ProjectionArchiveResult(
            snapshot_id=write.snapshot_id,
            generation_id=write.generation_id,
            changed=changed,
            observation_count=prepared.observation_count,
            materialization_outcome=outcome,
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
        unresolved_replay_fence = self._pre_decision_unresolved_fence(
            connection,
            prepared,
            provider=snapshot.provider,
            season=prepared.query.season,
            query_key=prepared.query_key,
        )
        if unresolved_replay_fence is not None and (
            promoted_through is None
            or unresolved_replay_fence > promoted_through
        ):
            promoted_through = unresolved_replay_fence
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
        duration_ms: int | None = None,
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
        query_key = projection_query_key(query)
        poll_id = _digest(
            "pollf",
            normalized_provider,
            query_key,
            normalized_reason,
            "" if started is None else started.isoformat(),
            completed.isoformat(),
        )
        table = ProviderPoll.__table__
        normalized_duration_ms = self._normalize_duration_ms(duration_ms)
        with self._scope_transaction(
            normalized_provider, query.season, query_key
        ) as connection:
            identity = (
                table.c.provider == normalized_provider,
                table.c.season == query.season,
                table.c.query_key == query_key,
                table.c.outcome == ProviderPollOutcome.FAILED.value,
                table.c.failure_reason == normalized_reason,
                table.c.completed_at == completed,
                (
                    table.c.started_at.is_(None)
                    if started is None
                    else table.c.started_at == started
                ),
            )
            existing_poll_id = connection.execute(
                select(table.c.poll_id).where(*identity).order_by(table.c.poll_id).limit(1)
            ).scalar_one_or_none()
            if existing_poll_id is None:
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
                        duration_ms=normalized_duration_ms,
                    )
                )
            else:
                poll_id = str(existing_poll_id)
        return ProjectionPollResult(
            poll_id=poll_id,
            outcome=ProviderPollOutcome.FAILED,
        )

    def freeze_closing_projection_set(
        self,
        *,
        provider: str,
        query: NBAMarketQuery,
        canonical_game_id: str,
        started_at: datetime,
        created_at: datetime | None = None,
    ) -> ClosingProjectionSetResult:
        """Freeze one provider/game's eligible state at the governed start fence.

        The scope lock makes close/materialize races deterministic.  The
        materialization replay is bounded by the poll completion fence, so a
        pregame observation delivered after the game-start status cannot enter
        a set even when its provider retrieval timestamp is older.
        """

        normalized_provider = provider.strip().casefold()
        game_id = str(canonical_game_id).strip()
        if not normalized_provider:
            raise ValueError("projection closing provider is required")
        if not game_id:
            raise ValueError("projection closing game is required")
        if not isinstance(query, NBAMarketQuery) or query.season is None:
            raise ValueError("projection archive queries require a canonical season")
        started = assume_utc(started_at)
        created = assume_utc(created_at or datetime.now(timezone.utc))
        if created < started:
            raise ValueError("projection closing set cannot be created before game start")
        query_key = projection_query_key(query)
        with self._scope_transaction(
            normalized_provider, query.season, query_key
        ) as connection:
            set_table = ClosingProjectionSet.__table__
            existing = (
                connection.execute(
                    select(set_table).where(
                        set_table.c.provider == normalized_provider,
                        set_table.c.season == query.season,
                        set_table.c.query_key == query_key,
                        set_table.c.canonical_game_id == game_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                count = connection.execute(
                    select(func.count()).select_from(
                        ClosingProjectionMembership.__table__
                    ).where(
                        ClosingProjectionMembership.__table__.c.closing_set_id
                        == existing["closing_set_id"]
                    )
                ).scalar_one()
                return ClosingProjectionSetResult(
                    closing_set_id=str(existing["closing_set_id"]),
                    provider=normalized_provider,
                    season=query.season,
                    canonical_game_id=game_id,
                    started_at=assume_utc(existing["started_at"]),
                    observation_count=int(count),
                    created=False,
                )

            closing_set_id = _digest(
                "close",
                normalized_provider,
                query.season,
                query_key,
                game_id,
                started.isoformat(),
            )
            rows = self._closing_materialization_rows(
                connection,
                provider=normalized_provider,
                season=query.season,
                query_key=query_key,
                canonical_game_id=game_id,
                started_at=started,
            )
            connection.execute(
                insert(set_table).values(
                    closing_set_id=closing_set_id,
                    provider=normalized_provider,
                    season=query.season,
                    query_key=query_key,
                    canonical_game_id=game_id,
                    started_at=started,
                    created_at=created,
                )
            )
            if rows:
                connection.execute(
                    insert(ClosingProjectionMembership.__table__),
                    [
                        {
                            "closing_set_id": closing_set_id,
                            "market_reference": row["market_reference"],
                            "observation_id": row["observation_id"],
                            "generation_id": row["generation_id"],
                        }
                        for row in rows
                    ],
                )
            return ClosingProjectionSetResult(
                closing_set_id=closing_set_id,
                provider=normalized_provider,
                season=query.season,
                canonical_game_id=game_id,
                started_at=started,
                observation_count=len(rows),
                created=True,
            )

    @staticmethod
    def _closing_materialization_rows(
        connection: Any,
        *,
        provider: str,
        season: str,
        query_key: str,
        canonical_game_id: str,
        started_at: datetime,
    ) -> list[Any]:
        """Replay the bounded promoted suffix committed before the start fence."""

        generation_table = ProjectionMaterializationGeneration.__table__
        snapshot_table = ProjectionProviderSnapshot.__table__
        poll_table = ProviderPoll.__table__
        observation_table = ProjectionObservation.__table__
        generation_snapshot_poll = generation_table.join(
            snapshot_table,
            generation_table.c.snapshot_id == snapshot_table.c.snapshot_id,
        ).join(
            poll_table,
            generation_table.c.source_poll_id == poll_table.c.poll_id,
        )
        eligible = (
            generation_table.c.provider == provider,
            generation_table.c.season == season,
            generation_table.c.query_key == query_key,
            generation_table.c.outcome == MaterializationOutcome.ADVANCED.value,
            poll_table.c.completed_at <= started_at,
        )
        baseline = (
            connection.execute(
                select(
                    generation_table.c.generation_id,
                    generation_table.c.retrieved_at,
                    generation_table.c.created_at,
                )
                .select_from(generation_snapshot_poll)
                .where(
                    *eligible,
                    snapshot_table.c.snapshot_status == SnapshotStatus.COMPLETE.value,
                    generation_table.c.is_replay.is_(False),
                )
                .order_by(
                    generation_table.c.retrieved_at.desc(),
                    generation_table.c.created_at.desc(),
                    generation_table.c.generation_id.desc(),
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        lower_bound = ()
        if baseline is not None:
            lower_bound = (
                or_(
                    generation_table.c.retrieved_at > baseline["retrieved_at"],
                    and_(
                        generation_table.c.retrieved_at
                        == baseline["retrieved_at"],
                        generation_table.c.created_at > baseline["created_at"],
                    ),
                    and_(
                        generation_table.c.retrieved_at
                        == baseline["retrieved_at"],
                        generation_table.c.created_at == baseline["created_at"],
                        generation_table.c.generation_id
                        >= baseline["generation_id"],
                    ),
                ),
            )
        joined_rows = (
            connection.execute(
                select(
                    generation_table.c.generation_id,
                    generation_table.c.retrieved_at,
                    generation_table.c.created_at,
                    generation_table.c.is_replay,
                    snapshot_table.c.snapshot_status,
                    poll_table.c.completed_at,
                    observation_table.c.observation_id,
                    observation_table.c.market_reference,
                    observation_table.c.canonical_game_id,
                    observation_table.c.canonical_player_id,
                    observation_table.c.canonical_player_name,
                    observation_table.c.canonical_team_id,
                    observation_table.c.canonical_statistic_id,
                    observation_table.c.market_category,
                    observation_table.c.targetable,
                    observation_table.c.observed_at,
                    observation_table.c.ordinal,
                )
                .select_from(
                    generation_snapshot_poll.outerjoin(
                        observation_table,
                        and_(
                            observation_table.c.generation_id
                            == generation_table.c.generation_id,
                            observation_table.c.canonical_game_id
                            == canonical_game_id,
                        ),
                    )
                )
                .where(
                    *eligible,
                    *lower_bound,
                )
                .order_by(
                    generation_table.c.retrieved_at,
                    generation_table.c.created_at,
                    generation_table.c.generation_id,
                    observation_table.c.ordinal,
                )
            )
            .mappings()
            .all()
        )
        generations: list[dict[str, Any]] = []
        for row in joined_rows:
            generation_id = str(row["generation_id"])
            if not generations or generations[-1]["generation_id"] != generation_id:
                generations.append(
                    {
                        "generation_id": generation_id,
                        "is_replay": bool(row["is_replay"]),
                        "snapshot_status": row["snapshot_status"],
                        "rows": [],
                    }
                )
            if row["observation_id"] is not None:
                generations[-1]["rows"].append(row)

        state: dict[str, Any] = {}
        for generation in generations:
            rows = generation["rows"]
            if (
                generation["snapshot_status"] == SnapshotStatus.COMPLETE.value
                and not generation["is_replay"]
            ):
                state.clear()
            else:
                for reference in {str(row["market_reference"]) for row in rows}:
                    state.pop(reference, None)
            selected: set[str] = set()
            for row in rows:
                if not row["targetable"]:
                    continue
                reference = str(row["market_reference"])
                if reference in selected:
                    continue
                state[reference] = row
                selected.add(reference)
        return [state[key] for key in sorted(state)]
    def replay_athlete_mapping(
        self,
        *,
        provider: str,
        provider_athlete_id: str,
        canonical_player_id: int,
        canonical_player_name: str,
        canonical_team_id: int,
        replayed_at: datetime | None = None,
    ) -> ProjectionReplayResult | None:
        """Replay a governed athlete decision without retrieving a provider board."""

        if isinstance(canonical_player_id, bool) or not isinstance(
            canonical_player_id, int
        ):
            raise ValueError("canonical projection player ID must be an integer")
        if isinstance(canonical_team_id, bool) or not isinstance(canonical_team_id, int):
            raise ValueError("canonical projection team ID must be an integer")
        if not isinstance(canonical_player_name, str) or not canonical_player_name.strip():
            raise ValueError("canonical projection player name is required")
        return self._replay_mapping(
            kind="athlete",
            provider=provider,
            identity=provider_athlete_id,
            canonical={
                "canonical_player_id": canonical_player_id,
                "canonical_player_name": canonical_player_name.strip(),
                "canonical_team_id": canonical_team_id,
            },
            replayed_at=replayed_at,
        )

    def replay_athlete_decision(self, result: Any) -> ProjectionReplayResult | None:
        """Adapt an athlete repository decision to the archive replay seam."""

        mapping = getattr(result, "mapping", None)
        if mapping is None or not getattr(result, "persisted", False):
            return None
        if mapping.canonical_name is None or mapping.canonical_team_id is None:
            return None
        return self.replay_athlete_mapping(
            provider=mapping.provider,
            provider_athlete_id=mapping.provider_athlete_id,
            canonical_player_id=mapping.canonical_player_id,
            canonical_player_name=mapping.canonical_name,
            canonical_team_id=mapping.canonical_team_id,
        )

    def replay_event_mapping(
        self,
        *,
        provider: str,
        provider_event_id: str,
        canonical_game_id: str,
        replayed_at: datetime | None = None,
    ) -> ProjectionReplayResult | None:
        """Replay a governed event decision without retrieving a provider board."""

        if not isinstance(canonical_game_id, str):
            raise ValueError("canonical projection game ID is required")
        game_id = canonical_game_id.strip()
        if not game_id:
            raise ValueError("canonical projection game ID is required")
        return self._replay_mapping(
            kind="event",
            provider=provider,
            identity=provider_event_id,
            canonical={"canonical_game_id": game_id},
            replayed_at=replayed_at,
        )

    def replay_event_decision(self, result: Any) -> ProjectionReplayResult | None:
        """Adapt an event repository decision to the archive replay seam."""

        mapping = getattr(result, "mapping", None)
        if mapping is None or not getattr(result, "persisted", False):
            return None
        return self.replay_event_mapping(
            provider=mapping.provider,
            provider_event_id=mapping.provider_event_id,
            canonical_game_id=mapping.canonical_event_id,
        )

    def replay_statistic_mapping(
        self,
        *,
        provider: str,
        canonical_statistic_id: str,
        market_category: str | None = None,
        provider_statistic_id: str | None = None,
        provider_statistic_label: str | None = None,
        replayed_at: datetime | None = None,
    ) -> ProjectionReplayResult | None:
        """Replay a reviewed statistic mapping over archived observations."""

        if (
            provider_statistic_id is None
            and provider_statistic_label is None
        ):
            raise ValueError(
                "a provider statistic ID or label is required for projection replay"
            )
        if not isinstance(canonical_statistic_id, str):
            raise ValueError("canonical projection statistic ID is required")
        statistic_id = canonical_statistic_id.strip()
        if not statistic_id:
            raise ValueError("canonical projection statistic ID is required")
        normalized_statistic_id = (
            None
            if provider_statistic_id is None
            else str(provider_statistic_id).strip()
        )
        normalized_statistic_label = (
            None
            if provider_statistic_label is None
            else str(provider_statistic_label).strip()
        )
        if normalized_statistic_id == "":
            normalized_statistic_id = None
        if normalized_statistic_label == "":
            normalized_statistic_label = None
        if normalized_statistic_id is None and normalized_statistic_label is None:
            raise ValueError(
                "a provider statistic ID or label is required for projection replay"
            )
        category = market_category or self.market_categories.get(statistic_id)
        return self._replay_mapping(
            kind="statistic",
            provider=provider,
            identity=normalized_statistic_id,
            canonical={
                "canonical_statistic_id": statistic_id,
                "market_category": category,
                "provider_statistic_label": normalized_statistic_label,
            },
            replayed_at=replayed_at,
        )

    def _replay_mapping(
        self,
        *,
        kind: str,
        provider: str,
        identity: str | None,
        canonical: dict[str, Any],
        replayed_at: datetime | None,
    ) -> ProjectionReplayResult | None:
        if not isinstance(provider, str):
            raise ValueError("projection replay provider is required")
        normalized_provider = provider.strip().casefold()
        if identity is not None and not isinstance(identity, str):
            raise ValueError("projection replay provider identity is required")
        normalized_identity = None if identity is None else identity.strip()
        if not normalized_provider or (
            not normalized_identity and kind != "statistic"
        ):
            raise ValueError("projection replay provider identity is required")
        scopes = self._replay_scopes(normalized_provider)
        result: ProjectionReplayResult | None = None
        for season, query_key in scopes:
            replay = self._replay_scope(
                kind=kind,
                provider=normalized_provider,
                season=season,
                query_key=query_key,
                identity=normalized_identity,
                canonical=canonical,
                replayed_at=replayed_at,
            )
            if replay is not None:
                result = replay
        return result

    def _replay_scopes(self, provider: str) -> tuple[tuple[str, str], ...]:
        table = ProjectionMaterializationGeneration.__table__
        with self.engine.connect() as connection:
            return tuple(
                connection.execute(
                    select(table.c.season, table.c.query_key)
                    .where(table.c.provider == provider)
                    .distinct()
                    .order_by(table.c.season, table.c.query_key)
                ).all()
            )

    def _replay_scope(
        self,
        *,
        kind: str,
        provider: str,
        season: str,
        query_key: str,
        identity: str | None,
        canonical: dict[str, Any],
        replayed_at: datetime | None,
    ) -> ProjectionReplayResult | None:
        with self._scope_transaction(provider, season, query_key) as connection:
            current = self._current_materialization(
                connection,
                provider=provider,
                season=season,
                query_key=query_key,
            )
            if current is None:
                return None
            observation_table = ProjectionObservation.__table__
            generation_table = ProjectionMaterializationGeneration.__table__
            latest_table = LatestPlayerProjection.__table__
            identity_predicate = self._mapping_identity_predicate(
                observation_table,
                kind=kind,
                identity=identity,
                canonical=canonical,
                provider=provider,
            )
            source_rows = connection.execute(
                select(observation_table)
                .join(
                    generation_table,
                    generation_table.c.generation_id
                    == observation_table.c.generation_id,
                )
                .where(
                    observation_table.c.snapshot_id == current["snapshot_id"],
                    generation_table.c.outcome
                    == MaterializationOutcome.ADVANCED.value,
                    generation_table.c.created_at <= current["created_at"],
                    identity_predicate,
                )
                .order_by(
                    generation_table.c.created_at,
                    generation_table.c.generation_id,
                    observation_table.c.observation_id,
                )
            ).mappings().all()
            generation_rows = connection.execute(
                select(observation_table).where(
                    observation_table.c.generation_id == current["generation_id"],
                    identity_predicate,
                )
            ).mappings().all()
            latest_observation_ids = select(latest_table.c.observation_id).where(
                latest_table.c.provider == provider,
                latest_table.c.season == season,
                latest_table.c.query_key == query_key,
            )
            current_latest_rows = connection.execute(
                select(observation_table).where(
                    observation_table.c.observation_id.in_(latest_observation_ids)
                )
            ).mappings().all()
            latest_rows = connection.execute(
                select(observation_table).where(
                    observation_table.c.observation_id.in_(latest_observation_ids),
                    identity_predicate,
                )
            ).mappings().all()
            candidates: dict[str, dict[str, Any]] = {}
            for rows in (source_rows, generation_rows, latest_rows):
                for row in rows:
                    candidates[str(row["source_observation_id"])] = dict(row)
            if not candidates:
                return None
            source_markets = self._replay_source_markets(
                connection, tuple(candidates.values())
            )

            changed_rows: list[dict[str, Any]] = []
            replaced_references: set[str] = set()
            for row in candidates.values():
                if not self._observation_matches_mapping(
                    row, kind=kind, identity=identity, canonical=canonical
                ):
                    continue
                rematerialized = self._rematerialize_observation(
                    row,
                    kind=kind,
                    canonical=canonical,
                    source_market=source_markets[
                        (str(row["snapshot_id"]), int(row["source_ordinal"]))
                    ],
                )
                if self._replay_fields(row) != self._replay_fields(rematerialized):
                    changed_rows.append(rematerialized)
                    replaced_references.add(str(row["market_reference"]))

            if not changed_rows:
                latest_generation = str(current["generation_id"])
                return ProjectionReplayResult(
                    generation_id=latest_generation,
                    changed=False,
                    observation_count=0,
                    materialization_outcome=MaterializationOutcome.UNCHANGED,
                )

            changed_rows.sort(
                key=lambda row: (
                    str(row["snapshot_id"]),
                    int(row["ordinal"]),
                    str(row["observation_id"]),
                )
            )
            affected_references = replaced_references | {
                str(row["market_reference"]) for row in changed_rows
            }
            latest_state = [
                dict(row)
                for row in current_latest_rows
                if str(row["market_reference"]) not in affected_references
            ]
            changed_by_reference: dict[str, dict[str, Any]] = {}
            for row in changed_rows:
                reference = str(row["market_reference"])
                previous = changed_by_reference.get(reference)
                if previous is None or (
                    row["targetable"]
                    and not previous["targetable"]
                ) or (
                    row["targetable"] == previous["targetable"]
                    and int(row["ordinal"]) < int(previous["ordinal"])
                ):
                    changed_by_reference[reference] = row
            for row in changed_by_reference.values():
                if row["targetable"]:
                    latest_state.append(row)
            latest_state.sort(
                key=lambda row: (
                    str(row["provider"]),
                    str(row["canonical_game_id"]),
                    int(row["canonical_player_id"]),
                    str(row["market_reference"]),
                )
            )
            checksum_rows = [
                {
                    "ordinal": index,
                    **{
                        field: row[field]
                        for field in (
                            "market_reference",
                            "canonical_game_id",
                            "canonical_player_id",
                            "canonical_player_name",
                            "canonical_team_id",
                            "canonical_statistic_id",
                            "market_category",
                            "targetable",
                        )
                    },
                }
                for index, row in enumerate(latest_state)
            ]
            materialization_checksum = _materialization_checksum(checksum_rows)
            generation_id = _digest(
                "gen_replay",
                current["snapshot_id"],
                materialization_checksum,
                assume_utc(current["retrieved_at"]).isoformat(),
            )
            existing_generation_id = connection.execute(
                select(ProjectionMaterializationGeneration.generation_id).where(
                    ProjectionMaterializationGeneration.snapshot_id
                    == current["snapshot_id"],
                    ProjectionMaterializationGeneration.materialization_checksum
                    == materialization_checksum,
                    ProjectionMaterializationGeneration.retrieved_at
                    == current["retrieved_at"],
                )
            ).scalar_one_or_none()
            requested_created_at = assume_utc(
                replayed_at or datetime.now(timezone.utc)
            )
            created_at = max(
                requested_created_at,
                assume_utc(current["created_at"]) + timedelta(microseconds=1),
            )
            new_rows: list[dict[str, Any]]
            if existing_generation_id is None:
                new_rows = []
                for ordinal, row in enumerate(changed_rows):
                    new_row = dict(row)
                    new_row.update(
                        generation_id=generation_id,
                        ordinal=ordinal,
                        observation_id=_digest(
                            "obs_replay", generation_id, row["observation_id"]
                        ),
                    )
                    new_rows.append(new_row)
                connection.execute(
                    insert(ProjectionMaterializationGeneration.__table__).values(
                        generation_id=generation_id,
                        provider=provider,
                        season=season,
                        query_key=query_key,
                        snapshot_id=current["snapshot_id"],
                        source_poll_id=current["source_poll_id"],
                        created_at=created_at,
                        retrieved_at=current["retrieved_at"],
                        materialization_checksum=materialization_checksum,
                        outcome=MaterializationOutcome.ADVANCED.value,
                        is_replay=True,
                    )
                )
                connection.execute(insert(observation_table), new_rows)
            else:
                generation_id = str(existing_generation_id)
                existing_rows = connection.execute(
                    select(observation_table).where(
                        observation_table.c.generation_id == generation_id
                    )
                ).mappings().all()
                desired = {
                    (str(row["market_reference"]), self._replay_fields(row))
                    for row in changed_by_reference.values()
                }
                new_rows = [
                    dict(row)
                    for row in existing_rows
                    if (
                        str(row["market_reference"]),
                        self._replay_fields(row),
                    )
                    in desired
                ]
                if len(new_rows) != len(desired):
                    raise RuntimeError(
                        "existing projection materialization lacks replay observations"
                    )
            deleted_references = tuple(sorted(affected_references))
            if deleted_references:
                connection.execute(
                    delete(latest_table).where(
                        latest_table.c.provider == provider,
                        latest_table.c.season == season,
                        latest_table.c.query_key == query_key,
                        latest_table.c.market_reference.in_(deleted_references),
                    )
                )
            latest_values = []
            for row in new_rows:
                if not row["targetable"]:
                    continue
                selected = changed_by_reference[str(row["market_reference"])]
                if self._replay_fields(row) != self._replay_fields(selected):
                    continue
                latest_values.append(self._latest_row_values(
                    row,
                    season=season,
                    query_key=query_key,
                    generation_id=generation_id,
                    confirmed_at=row["observed_at"],
                ))
            if latest_values:
                connection.execute(insert(latest_table), latest_values)
            self._activate_generation(
                connection,
                provider=provider,
                season=season,
                query_key=query_key,
                generation_id=generation_id,
                mapping_replayed_at=created_at,
                mapping_replayed_retrieved_at=current["retrieved_at"],
            )
            return ProjectionReplayResult(
                generation_id=generation_id,
                changed=True,
                observation_count=len(new_rows),
                materialization_outcome=MaterializationOutcome.ADVANCED,
            )

    @staticmethod
    def _mapping_identity_predicate(
        table: Any,
        *,
        kind: str,
        identity: str | None,
        canonical: Mapping[str, Any],
        provider: str,
    ) -> Any:
        provider_predicate = table.c.provider == provider
        if kind == "athlete":
            return provider_predicate & (table.c.athlete_provider_id == identity)
        if kind == "event":
            return provider_predicate & (table.c.event_provider_id == identity)
        predicates = []
        if identity is not None:
            predicates.append(table.c.statistic_provider_id == identity)
        label = canonical.get("provider_statistic_label")
        if label is not None:
            predicates.append(
                func.lower(table.c.statistic_provider_label) == str(label).casefold()
            )
        return provider_predicate & or_(*predicates)

    @staticmethod
    def _observation_matches_mapping(
        row: Mapping[str, Any],
        *,
        kind: str,
        identity: str | None,
        canonical: Mapping[str, Any],
    ) -> bool:
        if kind == "athlete":
            return row["athlete_provider_id"] == identity
        if kind == "event":
            return row["event_provider_id"] == identity
        return (
            (identity is not None and row["statistic_provider_id"] == identity)
            or (
                canonical.get("provider_statistic_label") is not None
                and isinstance(row["statistic_provider_label"], str)
                and row["statistic_provider_label"].casefold()
                == str(canonical["provider_statistic_label"]).casefold()
            )
        )

    def _rematerialize_observation(
        self,
        row: Mapping[str, Any],
        *,
        kind: str,
        canonical: Mapping[str, Any],
        source_market: PlayerProjectionMarket,
    ) -> dict[str, Any]:
        rematerialized = dict(row)
        athlete_team_id = None
        if kind == "athlete":
            athlete_team_id = canonical["canonical_team_id"]
            rematerialized.update(
                canonical_player_id=canonical["canonical_player_id"],
                canonical_team_id=canonical["canonical_team_id"],
            )
        elif kind == "event":
            rematerialized["canonical_game_id"] = canonical["canonical_game_id"]
        else:
            rematerialized["canonical_statistic_id"] = canonical[
                "canonical_statistic_id"
            ]
            rematerialized["market_category"] = canonical["market_category"]
        if kind != "athlete" and source_market.athlete is not None:
            athlete_team_id = source_market.athlete.team.canonical_id if (
                source_market.athlete.team is not None
            ) else None
        if kind == "athlete" and rematerialized["canonical_player_name"] is None:
            rematerialized["canonical_player_name"] = canonical["canonical_player_name"]
        unresolved = ProjectionArchive._unresolved_identities(
            canonical_player_id=rematerialized["canonical_player_id"],
            canonical_player_name=rematerialized["canonical_player_name"],
            canonical_game_id=rematerialized["canonical_game_id"],
            canonical_statistic_id=rematerialized["canonical_statistic_id"],
        )
        rematerialized["resolution_state"] = (
            "resolved" if not unresolved else "unresolved"
        )
        rematerialized["unresolved_identities"] = ",".join(unresolved)
        rematerialized["targetable"] = bool(
            rematerialized["market_status"] == MarketStatus.AVAILABLE.value
            and rematerialized["market_variant"] == MarketVariant.STANDARD.value
            and rematerialized["scoring_period"] == ScoringPeriod.FULL_GAME.value
            and not unresolved
            and rematerialized["canonical_team_id"] is not None
            and rematerialized["market_category"] is not None
        )
        rematerialized["market_reference"] = market_reference(
            self._market_with_replayed_identities(
                source_market,
                rematerialized,
                athlete_team_id=athlete_team_id,
            )
        )
        return rematerialized

    @staticmethod
    def _replay_source_markets(
        connection: Any,
        rows: tuple[Mapping[str, Any], ...],
    ) -> dict[tuple[str, int], PlayerProjectionMarket]:
        ordinals_by_snapshot: dict[str, set[int]] = {}
        for row in rows:
            ordinals_by_snapshot.setdefault(str(row["snapshot_id"]), set()).add(
                int(row["source_ordinal"])
            )
        table = ProjectionProviderSnapshot.__table__
        markets: dict[tuple[str, int], PlayerProjectionMarket] = {}
        for snapshot in connection.execute(
            select(table.c.snapshot_id, table.c.evidence_document).where(
                table.c.snapshot_id.in_(ordinals_by_snapshot)
            )
        ).mappings():
            snapshot_id = str(snapshot["snapshot_id"])
            document = json.loads(str(snapshot["evidence_document"]))
            raw_markets = document.get("markets")
            if not isinstance(raw_markets, list):
                raise RuntimeError(
                    "projection replay snapshot has no immutable markets"
                )
            for source_ordinal in sorted(ordinals_by_snapshot[snapshot_id]):
                if source_ordinal >= len(raw_markets):
                    raise RuntimeError(
                        "projection replay observation has no immutable source market"
                    )
                candidate_document = dict(document)
                candidate_document["markets"] = [raw_markets[source_ordinal]]
                candidate_snapshot = deserialize_provider_snapshot(
                    json.dumps(candidate_document, separators=(",", ":"), sort_keys=True),
                    allow_partial=True,
                )
                markets[(snapshot_id, source_ordinal)] = candidate_snapshot.markets[0]
        if len(markets) != len({
            (str(row["snapshot_id"]), int(row["source_ordinal"])) for row in rows
        }):
            raise RuntimeError("projection replay observation has no immutable source market")
        return markets

    def _market_with_replayed_identities(
        self,
        source_market: PlayerProjectionMarket,
        row: Mapping[str, Any],
        *,
        athlete_team_id: int | None = None,
    ) -> PlayerProjectionMarket:
        athlete = source_market.athlete
        if athlete is not None:
            team = athlete.team
            canonical_team_id = (
                athlete_team_id
                if athlete_team_id is not None
                else row["canonical_team_id"]
            )
            if team is None and canonical_team_id is not None:
                team = TeamEvidence(canonical_id=int(canonical_team_id))
            elif team is not None and (
                team.canonical_id is None and canonical_team_id is not None
            ):
                team = replace(team, canonical_id=int(canonical_team_id))
            athlete = replace(
                athlete,
                canonical_id=row["canonical_player_id"],
                name=athlete.name or row["canonical_player_name"],
                team=team,
            )
        event = source_market.event
        if event is not None:
            event = replace(event, canonical_id=row["canonical_game_id"])
        statistic = source_market.statistic
        if statistic is not None:
            statistic = replace(
                statistic,
                canonical_id=row["canonical_statistic_id"],
            )
        rematerialized = replace(
            source_market,
            athlete=athlete,
            event=event,
            statistic=statistic,
            statistic_match=None,
        )
        canonical_statistic_id = row["canonical_statistic_id"]
        canonical_statistic = self.statistic_catalog.by_id.get(
            str(canonical_statistic_id)
        ) if canonical_statistic_id is not None else None
        if statistic is not None and canonical_statistic is not None:
            rematerialized = replace(
                rematerialized,
                statistic_match=StatisticMatch(
                    state=MatchState.CANONICAL,
                    evidence=statistic,
                    scoring_period=ScoringPeriod(str(row["scoring_period"])),
                    canonical=canonical_statistic,
                    provider=source_market.provider,
                    unit=canonical_statistic.unit,
                ),
            )
        return rematerialized

    @staticmethod
    def _replay_fields(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(
            row[field]
            for field in (
                "canonical_game_id",
                "canonical_player_id",
                "canonical_player_name",
                "canonical_team_id",
                "canonical_statistic_id",
                "market_category",
                "targetable",
                "resolution_state",
                "unresolved_identities",
            )
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
        lock_table = ProjectionArchiveScopeLock.__table__
        active_generation_id = connection.execute(
            select(lock_table.c.active_generation_id).where(
                lock_table.c.provider == provider,
                lock_table.c.season == season,
                lock_table.c.query_key == query_key,
            )
        ).scalar_one_or_none()
        statement = (
            select(
                generation_table.c.generation_id,
                snapshot_table.c.snapshot_id,
                generation_table.c.source_poll_id,
                generation_table.c.created_at,
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
        )
        if active_generation_id is not None:
            statement = statement.where(
                generation_table.c.generation_id == active_generation_id
            )
        else:
            statement = statement.order_by(
                generation_table.c.retrieved_at.desc(),
                generation_table.c.created_at.desc(),
                generation_table.c.generation_id.desc(),
            )
        return (
            connection.execute(
                statement.limit(1)
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _activate_generation(
        connection: Any,
        *,
        provider: str,
        season: str,
        query_key: str,
        generation_id: str,
        mapping_replayed_at: datetime | None = None,
        mapping_replayed_retrieved_at: datetime | None = None,
    ) -> None:
        table = ProjectionArchiveScopeLock.__table__
        values: dict[str, Any] = {"active_generation_id": generation_id}
        existing = connection.execute(
            select(
                table.c.mapping_replayed_at,
                table.c.mapping_replayed_retrieved_at,
            ).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
            )
        ).mappings().one()
        if mapping_replayed_at is not None:
            prior = existing["mapping_replayed_at"]
            if prior is None or assume_utc(mapping_replayed_at) > assume_utc(prior):
                values["mapping_replayed_at"] = mapping_replayed_at
        if mapping_replayed_retrieved_at is not None:
            prior = existing["mapping_replayed_retrieved_at"]
            if (
                prior is None
                or assume_utc(mapping_replayed_retrieved_at) > assume_utc(prior)
            ):
                values["mapping_replayed_retrieved_at"] = mapping_replayed_retrieved_at
        connection.execute(
            update(table)
            .where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
            )
            .values(**values)
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
        poll_fence = connection.execute(
            select(func.max(table.c.retrieved_at)).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
                table.c.promoted.is_(True),
            )
        ).scalar_one()
        lock_table = ProjectionArchiveScopeLock.__table__
        replay_fence = connection.execute(
            select(lock_table.c.mapping_replayed_retrieved_at).where(
                lock_table.c.provider == provider,
                lock_table.c.season == season,
                lock_table.c.query_key == query_key,
            )
        ).scalar_one_or_none()
        fences = tuple(
            assume_utc(value)
            for value in (poll_fence, replay_fence)
            if value is not None
        )
        return max(fences) if fences else None

    @staticmethod
    def _pre_decision_unresolved_fence(
        connection: Any,
        prepared: _PreparedProjectionIngestion,
        *,
        provider: str,
        season: str,
        query_key: str,
    ) -> datetime | None:
        """Keep pre-decision unresolved cache evidence from erasing recovery."""

        if not any(
            row["resolution_state"] == "unresolved"
            for row in prepared.observation_rows
        ):
            return None
        table = ProjectionArchiveScopeLock.__table__
        replayed_at = connection.execute(
            select(table.c.mapping_replayed_at).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
            )
        ).scalar_one_or_none()
        if replayed_at is None or assume_utc(prepared.snapshot.retrieved_at) >= assume_utc(
            replayed_at
        ):
            return None
        return assume_utc(replayed_at)

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
        request: _ProjectionPollWrite,
    ) -> None:
        connection.execute(
            insert(ProviderPoll.__table__).values(
                poll_id=request.poll_id,
                provider=request.provider,
                season=request.season,
                query_key=request.query_key,
                started_at=request.started_at,
                completed_at=request.completed_at,
                retrieved_at=request.retrieved_at,
                outcome=request.outcome.value,
                promoted=request.promoted,
                snapshot_id=request.snapshot_id,
                generation_id=request.generation_id,
                observation_count=request.observation_count,
                duration_ms=request.duration_ms,
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
            unresolved_identities = self._unresolved_identities(
                canonical_player_id=canonical_player_id,
                canonical_player_name=canonical_player_name,
                canonical_game_id=canonical_game_id,
                canonical_statistic_id=canonical_statistic_id,
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
                    "athlete_provider_id": (
                        None
                        if market.athlete is None
                        else market.athlete.provider_id
                    ),
                    "event_provider_id": (
                        None if market.event is None else market.event.provider_id
                    ),
                    "statistic_provider_id": (
                        None
                        if market.statistic is None
                        else market.statistic.provider_id
                    ),
                    "statistic_provider_label": (
                        None
                        if market.statistic is None
                        else market.statistic.label
                    ),
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
                    "resolution_state": (
                        "resolved" if not unresolved_identities else "unresolved"
                    ),
                    "unresolved_identities": ",".join(unresolved_identities),
                    "observed_at": snapshot.retrieved_at,
                    "canonical_player_name": canonical_player_name,
                }
            )
        return rows

    @staticmethod
    def _unresolved_identities(
        *,
        canonical_player_id: int | None,
        canonical_player_name: str | None,
        canonical_game_id: str | None,
        canonical_statistic_id: str | None,
    ) -> tuple[str, ...]:
        unresolved: list[str] = []
        if canonical_player_id is None or canonical_player_name is None:
            unresolved.append("athlete")
        if canonical_game_id is None:
            unresolved.append("event")
        if canonical_statistic_id is None:
            unresolved.append("statistic")
        return tuple(unresolved)

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
        confirmed_at: datetime | None = None,
    ) -> None:
        connection.execute(
            insert(LatestPlayerProjection.__table__),
            ProjectionArchive._latest_row_values(
                row,
                season=season,
                query_key=query_key,
                generation_id=generation_id,
                confirmed_at=confirmed_at,
            ),
        )

    @staticmethod
    def _latest_row_values(
        row: dict[str, Any],
        *,
        season: str,
        query_key: str,
        generation_id: str,
        confirmed_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": row["provider"],
            "season": season,
            "query_key": query_key,
            "canonical_game_id": row["canonical_game_id"],
            "canonical_player_id": row["canonical_player_id"],
            "market_reference": row["market_reference"],
            "observation_id": row["observation_id"],
            "generation_id": generation_id,
            "canonical_team_id": row["canonical_team_id"],
            "canonical_player_name": row["canonical_player_name"],
            "canonical_statistic_id": row["canonical_statistic_id"],
            "market_category": row["market_category"],
            "observed_at": row["observed_at"],
            "confirmed_at": confirmed_at or row["observed_at"],
        }

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
        connection.execute(
            update(table)
            .where(*predicates)
            .values(observed_at=confirmed_at, confirmed_at=confirmed_at)
        )


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
        event_reader: Any | None = None,
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
        self.event_reader = event_reader

    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool:
        pool, _is_closing = self.get_pool_for_game_with_closing_state(
            season=season, game_id=game_id
        )
        return pool

    def get_pool_for_game_with_closing_state(
        self, *, season: str, game_id: str
    ) -> tuple[PlayerPool, bool]:
        if season != self.scope.query.season:
            raise ValueError("projection archive read season is outside its scope")
        requested_games = (str(game_id),)
        started_games = self._started_games(season, requested_games)
        return (
            self._pool_for_requested_games(
                season=season,
                requested_games=requested_games,
                started_games=started_games,
            ),
            bool(started_games),
        )

    def is_closing_game(self, *, season: str, game_id: str) -> bool:
        """Return whether the governed event has crossed its start fence."""

        return bool(self._started_games(season, (str(game_id),)))

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        if season != self.scope.query.season:
            raise ValueError("projection archive read season is outside its scope")
        requested_games = tuple(sorted({str(game_id) for game_id in game_ids}))
        if not requested_games:
            return PlayerPool((), {}, PlayerPool.missing_projection_freshness(), {})
        started_games = self._started_games(season, requested_games)
        return self._pool_for_requested_games(
            season=season,
            requested_games=requested_games,
            started_games=started_games,
        )

    def _pool_for_requested_games(
        self,
        *,
        season: str,
        requested_games: tuple[str, ...],
        started_games: dict[str, datetime],
    ) -> PlayerPool:
        if started_games:
            return self._get_pool_with_closing_sets(
                season=season,
                requested_games=requested_games,
                started_games=started_games,
            )
        return self._get_live_pool(season=season, requested_games=requested_games)

    def _get_live_pool(
        self,
        *,
        season: str,
        requested_games: tuple[str, ...],
    ) -> PlayerPool:
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

    def _started_games(
        self, season: str, requested_games: tuple[str, ...]
    ) -> dict[str, datetime]:
        reader = self.event_reader
        if reader is None:
            return {}
        get_events_by_ids = getattr(reader, "get_events_by_ids", None)
        if not callable(get_events_by_ids):
            return {}
        events = get_events_by_ids(season, requested_games)
        started: dict[str, datetime] = {}
        for event in events:
            game_id = str(event.get("nba_game_id", ""))
            if game_id not in requested_games:
                continue
            fence = _projection_closing_start_fence(event)
            if fence is not None:
                started[game_id] = fence
        return started

    def _get_pool_with_closing_sets(
        self,
        *,
        season: str,
        requested_games: tuple[str, ...],
        started_games: dict[str, datetime],
    ) -> PlayerPool:
        live_games = tuple(game for game in requested_games if game not in started_games)
        live_pool = (
            self._get_live_pool(season=season, requested_games=live_games)
            if live_games
            else None
        )
        closing_pool = self._read_closing_pool(
            season=season,
            requested_games=tuple(started_games),
        )
        if live_pool is None:
            return closing_pool
        return self._combine_pools(live_pool, closing_pool)

    @staticmethod
    def _combine_pools(*pools: PlayerPool) -> PlayerPool:
        contributions: dict[int, dict[str, Any]] = {}
        team_counts: dict[int, int] = {}
        game_states: dict[str, Mapping[str, Any]] = {}
        freshnesses = [pool.freshness for pool in pools]
        for pool in pools:
            game_states.update(pool.game_states)
            for player in pool.players:
                entry = contributions.setdefault(
                    player.canonical_player_id,
                    {
                        "name": player.name,
                        "team_id": player.team_id,
                        "categories": set(),
                        "provenance": {},
                    },
                )
                entry["categories"].update(player.market_categories)
                for provider, categories in player.provenance.items():
                    entry["provenance"].setdefault(provider, set()).update(categories)
        players = tuple(
            PoolPlayer(
                canonical_player_id=player_id,
                name=entry["name"],
                team_id=entry["team_id"],
                market_categories=tuple(sorted(entry["categories"])),
                provenance={
                    provider: tuple(sorted(categories))
                    for provider, categories in sorted(entry["provenance"].items())
                },
            )
            for player_id, entry in sorted(contributions.items())
        )
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
        states = {freshness.get("state") for freshness in freshnesses}
        if "live" in states:
            state = "live"
        elif "closing" in states:
            state = "closing"
        else:
            state = "missing"
        authoritative = [
            freshness
            for freshness in freshnesses
            if freshness.get("state") == state
        ]
        observed = [
            freshness.get("observed_at")
            for freshness in authoritative
            if freshness.get("observed_at") is not None
        ]
        retrieved = [
            freshness.get("retrieved_at")
            for freshness in authoritative
            if freshness.get("retrieved_at") is not None
        ]
        providers: dict[str, dict[str, Any]] = {}
        for freshness in authoritative:
            for provider, value in freshness.get("providers", {}).items():
                current = providers.get(provider)
                if current is None or (
                    value.get("retrieved_at") is not None
                    and (
                        current.get("retrieved_at") is None
                        or value["retrieved_at"] < current["retrieved_at"]
                    )
                ):
                    providers[provider] = dict(value)
        freshness: dict[str, Any] = {
            "state": state,
            "observed_at": min(observed) if observed else None,
            "retrieved_at": min(retrieved) if retrieved else None,
            "providers": dict(sorted(providers.items())),
        }
        if len(states) == 1 and state != "closing":
            statuses = [
                value["status"] for value in authoritative if "status" in value
            ]
            if statuses and len(set(statuses)) == 1:
                freshness["status"] = statuses[0]
        return PlayerPool(players, team_counts, freshness, game_states)

    def _read_closing_pool(
        self, *, season: str, requested_games: tuple[str, ...]
    ) -> PlayerPool:
        set_table = ClosingProjectionSet.__table__
        membership_table = ClosingProjectionMembership.__table__
        observation_table = ProjectionObservation.__table__
        providers = tuple(scope.provider for scope in self.scopes)
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ"
                )
            with connection.begin():
                sets = tuple(
                    connection.execute(
                        select(set_table).where(
                            set_table.c.provider.in_(providers),
                            set_table.c.season == season,
                            set_table.c.query_key == self.scope.query_key,
                            set_table.c.canonical_game_id.in_(requested_games),
                        )
                    )
                    .mappings()
                    .all()
                )
                rows = tuple(
                    connection.execute(
                        select(
                            membership_table.c.closing_set_id,
                            membership_table.c.market_reference,
                            membership_table.c.generation_id,
                            observation_table.c.observation_id,
                            observation_table.c.provider,
                            observation_table.c.canonical_game_id,
                            observation_table.c.canonical_player_id,
                            observation_table.c.canonical_player_name,
                            observation_table.c.canonical_team_id,
                            observation_table.c.canonical_statistic_id,
                            observation_table.c.market_category,
                            observation_table.c.observed_at.label("confirmed_at"),
                        )
                        .select_from(
                            membership_table.join(
                                observation_table,
                                membership_table.c.observation_id
                                == observation_table.c.observation_id,
                            )
                        )
                        .where(
                            membership_table.c.closing_set_id.in_(
                                [row["closing_set_id"] for row in sets]
                            ),
                            observation_table.c.targetable.is_(True),
                        )
                        .order_by(
                            observation_table.c.provider,
                            observation_table.c.canonical_player_id,
                            membership_table.c.market_reference,
                        )
                    )
                    .mappings()
                    .all()
                    if sets
                    else ()
                )
        game_states: dict[str, dict[str, Any]] = {}
        rows_by_game: dict[str, list[Any]] = {}
        for row in rows:
            rows_by_game.setdefault(str(row["canonical_game_id"]), []).append(row)
        for game_id in requested_games:
            game_rows = rows_by_game.get(game_id, [])
            game_states[game_id] = (
                {
                    "state": "closing",
                    "observed_at": min(
                        assume_utc(row["confirmed_at"]) for row in game_rows
                    ).isoformat(),
                }
                if game_rows
                else {"state": "missing", "observed_at": None}
            )
        if not rows:
            freshness = PlayerPool.missing_projection_freshness()
            freshness["providers"] = {
                provider: {"status": "missing", "retrieved_at": None}
                for provider in sorted(providers)
            }
            return PlayerPool((), {}, freshness, game_states)
        observed = min(assume_utc(row["confirmed_at"]) for row in rows).isoformat()
        freshness = {
            "state": "closing",
            "observed_at": observed,
            "retrieved_at": observed,
            "providers": {},
        }
        for provider in sorted(providers):
            provider_rows = [row for row in rows if row["provider"] == provider]
            freshness["providers"][provider] = {
                "status": "closing" if provider_rows else "missing",
                "retrieved_at": (
                    min(assume_utc(row["confirmed_at"]) for row in provider_rows).isoformat()
                    if provider_rows
                    else None
                ),
            }
        players = self._players_from_rows(list(rows))
        team_counts: dict[int, int] = {}
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
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
            row_observed_at = assume_utc(row["observed_at"])
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
        observed_at = min(assume_utc(row["observed_at"]) for row in rows)
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
        pool, is_closing = self.reader.get_pool_for_game_with_closing_state(
            season=season, game_id=game_id
        )
        if pool.freshness.get("state") == "missing" and not is_closing:
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
        if projection_query_key(query) != scope.query_key:
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
        duration_ms: int | None = None,
    ) -> ProjectionArchiveResult:
        return self._record_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
            duration_ms=duration_ms,
            require_complete=False,
        )

    def _record_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None,
        poll_started_at: datetime | None,
        duration_ms: int | None,
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
            duration_ms=duration_ms,
        )

    def record_complete_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> ProjectionArchiveResult:
        return self._record_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
            duration_ms=duration_ms,
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
        duration_ms: int | None = None,
    ) -> ProjectionPollResult:
        require_projection_archive_schema(self.archive.engine)
        self._scope_for(provider, query)
        return self.archive.record_failed_poll(
            provider=provider,
            query=query,
            completed_at=completed_at,
            poll_started_at=poll_started_at,
            failure_reason=failure_reason,
            duration_ms=duration_ms,
        )

    def freeze_closing_projection_sets(
        self,
        *,
        events: Iterable[Mapping[str, Any]],
        query: NBAMarketQuery,
        created_at: datetime,
    ) -> tuple[ClosingProjectionSetResult, ...]:
        """Idempotently close configured provider scopes for started games."""

        require_projection_archive_schema(self.archive.engine)
        created = assume_utc(created_at)
        query_key = projection_query_key(query)
        scopes = {
            provider: self._scope_for(provider, query)
            for provider in sorted(self.scopes)
        }
        started_events = tuple(
            event
            for event in events
            if str(event.get("nba_game_id", "")).strip()
            and _projection_closing_start_fence(event) is not None
        )
        game_ids = tuple(
            dict.fromkeys(str(event["nba_game_id"]).strip() for event in started_events)
        )
        existing: set[tuple[str, str]] = set()
        if game_ids and scopes:
            table = ClosingProjectionSet.__table__
            with self.archive.engine.connect() as connection:
                existing = {
                    (str(row["provider"]), str(row["canonical_game_id"]))
                    for row in connection.execute(
                        select(table.c.provider, table.c.canonical_game_id).where(
                            table.c.season == query.season,
                            table.c.query_key == query_key,
                            table.c.provider.in_(tuple(scopes)),
                            table.c.canonical_game_id.in_(game_ids),
                        )
                    ).mappings()
                }
        results: list[ClosingProjectionSetResult] = []
        for event in started_events:
            game_id = str(event.get("nba_game_id", "")).strip()
            fence = _projection_closing_start_fence(event)
            if not game_id or fence is None:
                continue
            for provider, scope in scopes.items():
                if (provider, game_id) in existing:
                    continue
                results.append(
                    self.archive.freeze_closing_projection_set(
                        provider=provider,
                        query=scope.query,
                        canonical_game_id=game_id,
                        started_at=fence,
                        created_at=created,
                    )
                )
        return tuple(results)


__all__ = [
    "ClosingProjectionSetResult",
    "LatestProjectionPlayerPoolReader",
    "ProjectionArchive",
    "ProjectionArchiveReadScope",
    "ProjectionArchiveResult",
    "ProjectionPollResult",
    "ProjectionReplayResult",
    "ProjectionRecordingService",
    "ProjectionSelectionPlayerPoolReader",
    "projection_query_key",
    "require_projection_archive_schema",
]
