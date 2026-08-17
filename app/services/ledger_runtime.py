"""Executable orchestration for bounded ledger collection and composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Mapping, Protocol
from uuid import uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.orm import sessionmaker

from app.models.collection_control import (
    ActiveSeason,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    ReconciliationItem,
)
from app.models.event_catalog import EventCatalogEntry
from app.domain.nba_events import (
    is_final_event,
    is_postponed_event,
    l15_game_ids_by_team,
)
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import BackfillResult, LedgerBackfillService
from app.services.ledger_materialization import LedgerMaterialization, LedgerMaterializationService
from app.services.ledger_materialization import LedgerCorrectionQueue
from app.services.ledger_materialization import LedgerMaterializationUnavailable
from app.services.ledger_derivations import LedgerDerivationUnavailable
from app.services.ledger_lineage import LedgerLineage
from app.services.collection_control import ControlPlaneError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LedgerGovernance:
    season: str
    cutoff: datetime
    expected_game_ids: frozenset[str]
    team_ids: frozenset[int]
    expected_l15_game_ids: dict[int, frozenset[str]]
    events: tuple[Mapping[str, object], ...] = ()
    manifest_id: str | None = None
    manifest_scope: str = "canonical_game_ledger"
    collect_before: datetime | None = None
    accepted_versions: frozenset[int] = frozenset({1})


class LedgerGovernanceReader(Protocol):
    def read_for_collection(self, season: str) -> LedgerGovernance: ...

    def read_for_composition(
        self,
        season: str,
        cutoff: datetime,
        manifest_id: str | None = None,
    ) -> LedgerGovernance: ...


class ActiveManifestLedgerGovernanceReader:
    """Derive exact composition truth only from active control-plane state."""

    def __init__(self, engine, *, clock=None) -> None:
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read(self, season: str, cutoff: datetime) -> LedgerGovernance:
        return self.read_for_composition(season, cutoff)

    def read_for_composition(
        self,
        season: str,
        cutoff: datetime,
        manifest_id: str | None = None,
    ) -> LedgerGovernance:
        return self._read(
            season,
            cutoff,
            require_collection_authorization=False,
            manifest_id=manifest_id,
        )

    def _read(
        self,
        season: str,
        cutoff: datetime,
        *,
        require_collection_authorization: bool,
        manifest_id: str | None = None,
    ) -> LedgerGovernance:
        with self.engine.connect() as connection:
            active = connection.execute(select(ActiveSeason).where(
                ActiveSeason.season == season,
                ActiveSeason.status == "active",
                ActiveSeason.phase == "Regular Season",
            )).first() if require_collection_authorization else True
            manifest_query = select(CollectionManifest).where(
                CollectionManifest.season == season,
                CollectionManifest.cutoff == cutoff,
            )
            if manifest_id is not None:
                manifest_query = manifest_query.where(
                    CollectionManifest.manifest_id == manifest_id,
                )
            if require_collection_authorization:
                manifest_query = manifest_query.where(
                    CollectionManifest.status == "active",
                    CollectionManifest.collect_before > self.clock(),
                )
            manifest = connection.execute(
                manifest_query.order_by(CollectionManifest.created_at.desc()).limit(1)
            ).mappings().one_or_none()
            events = connection.execute(select(EventCatalogEntry).where(
                EventCatalogEntry.season == season,
                EventCatalogEntry.classification == "Regular Season",
                EventCatalogEntry.scheduled_at <= cutoff,
            ).order_by(EventCatalogEntry.scheduled_at, EventCatalogEntry.nba_game_id)).mappings().all()
        if (
            active is None
            or manifest is None
            or "canonical_game_ledger" not in set(json.loads(manifest["scopes"]))
            or 1 not in set(json.loads(manifest["accepted_versions"]))
        ):
            raise ValueError("active manifest and completed Event Catalog governance are required")
        events = tuple(
            event for event in events
            if is_final_event(event) and not is_postponed_event(event)
        )
        if not events:
            raise ValueError("completed Regular Season Event Catalog governance is required")
        team_ids = frozenset(
            int(team_id)
            for event in events
            for team_id in (event["home_team_id"], event["away_team_id"])
        )
        expected = frozenset(str(event["nba_game_id"]) for event in events)
        return LedgerGovernance(
            season=season,
            cutoff=cutoff,
            expected_game_ids=expected,
            team_ids=team_ids,
            expected_l15_game_ids=l15_game_ids_by_team(events),
            events=events,
            manifest_id=str(manifest["manifest_id"]),
            collect_before=(
                manifest["collect_before"].replace(tzinfo=timezone.utc)
                if manifest["collect_before"].tzinfo is None
                else manifest["collect_before"]
            ),
            accepted_versions=frozenset(int(value) for value in json.loads(manifest["accepted_versions"])),
        )

    def read_for_collection(self, season: str) -> LedgerGovernance:
        """Resolve the newest executable manifest before any provider I/O."""

        now = self.clock()
        with self.engine.connect() as connection:
            cutoff = connection.scalar(select(CollectionManifest.cutoff).where(
                CollectionManifest.season == season,
                CollectionManifest.status == "active",
                CollectionManifest.collect_before > now,
            ).order_by(CollectionManifest.cutoff.desc()).limit(1))
        if cutoff is None:
            raise ValueError("active manifest and completed Event Catalog governance are required")
        return self._read(
            season,
            cutoff,
            require_collection_authorization=True,
            manifest_id=None,
        )

    # Compatibility alias for internal callers written before collection and
    # composition authorization became distinct operations.
    read_active = read_for_collection


_SEASON_STREAMS = frozenset({
    "player_game_logs",
    "traditional_opponent_season",
    "player_per36",
    "assist_locations_season",
})

_MATCHUP_STREAMS = frozenset({
    "traditional_opponent_season",
    "traditional_opponent_l15",
    "assist_locations_season",
    "assist_locations_l15",
})


def _composition_failure_reason(
    stream_key: str,
    materialization: LedgerMaterialization,
) -> str:
    season_reason = materialization.season_window.reason or ""
    if "governed team roster" in season_reason or "League Complete" in season_reason:
        return "governed_team_roster_incomplete"
    if stream_key in {"assist_locations_season", "assist_locations_l15"} and (
        materialization.assist_location_season is None
        or materialization.assist_location_l15 is None
    ):
        return "assist_location_evidence_incomplete"
    window = (
        materialization.season_window
        if stream_key in _SEASON_STREAMS
        else materialization.l15_window
    )
    reason = window.reason or "ledger_window_incomplete"
    if "15 eligible games" in reason:
        return "insufficient_governed_games"
    if "governed team roster" in reason or "League Complete" in reason:
        return "governed_team_roster_incomplete"
    return reason


class LedgerRuntime:
    """Run one resumable collection pass and queued materialization work."""

    def __init__(
        self,
        *,
        backfill: LedgerBackfillService,
        repository: CanonicalGameLedgerRepository,
        materialization: LedgerMaterializationService,
        governance: LedgerGovernanceReader,
        matchup_materialization=None,
        publication_service=None,
        clock=None,
    ) -> None:
        self.backfill = backfill
        self.repository = repository
        self.materialization = materialization
        self.governance = governance
        self.matchup_materialization = matchup_materialization
        self.publication_service = publication_service or getattr(
            materialization, "publication_service", None
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(
        self,
        season: str,
        *,
        max_games: int | None = None,
        historical_repair: bool = False,
    ) -> BackfillResult:
        governance = self.governance.read_for_collection(season)
        return self.backfill.refresh(
            season,
            cutoff=governance.cutoff,
            max_games=max_games,
            historical_repair=historical_repair,
            governed_events=governance.events,
            manifest_id=governance.manifest_id,
            manifest_scope=governance.manifest_scope,
            collect_before=governance.collect_before,
            accepted_versions=governance.accepted_versions,
        )

    def compose_queued(self, season: str) -> int:
        table = CompositionJob.__table__
        with self.repository.engine.connect() as connection:
            queued = connection.execute(select(table).where(
                table.c.season == season,
                table.c.status == "queued",
            ).order_by(table.c.cutoff, table.c.created_at)).mappings().all()
        slices = sorted({
            (row["cutoff"], row["manifest_id"])
            for row in queued
        }, key=lambda item: (item[0], item[1] or ""))
        completed = 0
        for stored_cutoff, manifest_id in slices:
            cutoff = (
                stored_cutoff.replace(tzinfo=timezone.utc)
                if stored_cutoff.tzinfo is None
                else stored_cutoff
            )
            # Claim the complete slice under a row lock.  ``generation`` is
            # lineage versioning, not an attempt count: acceptance can bump it
            # while composition runs and leave the row queued for the next
            # worker. Keep the claim transaction open through the input
            # snapshot, every read-model write, and the completion CAS.
            # PostgreSQL row locks then serialize acceptance behind this
            # transaction; the final CAS remains a defensive fence for
            # deployments without row-level locking.
            slice_jobs = None
            try:
                with self._session_factory() as session, session.begin():
                    rows = session.scalars(select(CompositionJob).where(
                        CompositionJob.season == season,
                        CompositionJob.cutoff == stored_cutoff,
                        CompositionJob.manifest_id == manifest_id,
                        CompositionJob.status == "queued",
                    ).with_for_update().order_by(
                        case(
                            *[(CompositionJob.stream_key == stream, index)
                              for stream, index in LedgerCorrectionQueue.STREAM_ORDER.items()],
                            else_=len(LedgerCorrectionQueue.STREAM_ORDER),
                        ),
                        CompositionJob.created_at,
                        CompositionJob.job_id,
                    )).all()
                    if not rows:
                        continue
                    slice_jobs = tuple(
                        {
                            "job_id": str(row.job_id),
                            "stream_key": str(row.stream_key),
                            "cutoff": row.cutoff,
                            "manifest_id": row.manifest_id,
                            "generation": int(row.generation or 1),
                            "trigger_game_id": row.trigger_game_id,
                            "trigger_game_ids": row.trigger_game_ids,
                            "affected_team_ids": row.affected_team_ids,
                            "source_observation_ids": row.source_observation_ids,
                            "ledger_checksum": row.ledger_checksum,
                            "game_set_checksum": row.game_set_checksum,
                            "ledger_evidence": row.ledger_evidence,
                            "recomposition_reason": row.recomposition_reason,
                        }
                        for row in rows
                    )
                    reason = next(
                        (
                            str(row["recomposition_reason"])
                            for row in slice_jobs
                            if row.get("recomposition_reason")
                        ),
                        "scheduled_reconciliation",
                    )
                    affected_team_ids = frozenset(
                        int(team_id)
                        for row in slice_jobs
                        for team_id in _json_list(row.get("affected_team_ids"))
                        if str(team_id).isdigit()
                    )
                    trigger_game_ids = frozenset(
                        str(game_id)
                        for row in slice_jobs
                        for game_id in _json_list(row.get("trigger_game_ids"))
                    )
                    if not trigger_game_ids:
                        trigger_game_ids = frozenset(
                            str(row["trigger_game_id"])
                            for row in slice_jobs
                            if row.get("trigger_game_id")
                        )
                    trigger_game_id = next(iter(sorted(trigger_game_ids)), None)
                    pending_source_observation_ids = {
                        str(source_observation_id)
                        for row in slice_jobs
                        for source_observation_id in _json_list(
                            row.get("source_observation_ids")
                        )
                    }
                    source_observation_ids_by_game = {}
                    if pending_source_observation_ids:
                        source_rows = session.scalars(
                            select(CollectionObservation).where(
                                CollectionObservation.observation_id.in_(
                                    pending_source_observation_ids
                                )
                            ).order_by(
                                CollectionObservation.accepted_at,
                                CollectionObservation.observation_id,
                            )
                        ).all()
                        for source_row in source_rows:
                            try:
                                source_scope = json.loads(source_row.scope)
                            except (TypeError, ValueError):
                                continue
                            if not isinstance(source_scope, Mapping):
                                continue
                            source_game_id = str(
                                source_scope.get("game_id") or ""
                            )
                            if source_game_id:
                                source_observation_ids_by_game[source_game_id] = (
                                    str(source_row.observation_id)
                                )
                    governance = self.governance.read_for_composition(
                        season,
                        cutoff,
                        manifest_id,
                    )
                    read_connection = session.connection()
                    games = tuple(
                        game
                        for summary in self.repository.list_games(
                            season,
                            through=cutoff.date(),
                            connection=read_connection,
                        )
                        if (game := self.repository.get_game(
                            summary.game_id,
                            connection=read_connection,
                        )) is not None
                    )
                    games_by_id = {game.game_id: game for game in games}
                    for row in slice_jobs:
                        raw_evidence = row.get("ledger_evidence")
                        evidence = (
                            json.loads(raw_evidence)
                            if isinstance(raw_evidence, str) and raw_evidence
                            else raw_evidence if isinstance(raw_evidence, dict) else {}
                        )
                        pending_ids = set(_json_list(row.get("trigger_game_ids")))
                        if not pending_ids and row.get("trigger_game_id"):
                            pending_ids = {str(row["trigger_game_id"])}
                        if pending_ids != set(evidence):
                            raise ControlPlaneError("pending_ledger_evidence_mismatch")
                        if evidence:
                            expected_ledger_checksum = (
                                next(iter(evidence.values()))
                                if len(evidence) == 1
                                else LedgerLineage.evidence_checksum(evidence)
                            )
                            expected_game_set_checksum = LedgerLineage.for_game_ids(
                                evidence
                            )
                            if (
                                str(row.get("ledger_checksum") or "")
                                != expected_ledger_checksum
                                or str(row.get("game_set_checksum") or "")
                                != expected_game_set_checksum
                            ):
                                raise ControlPlaneError("pending_ledger_evidence_mismatch")
                        if any(
                            game_id in games_by_id
                            and str(checksum) != str(games_by_id[game_id].checksum)
                            for game_id, checksum in evidence.items()
                        ):
                            raise ControlPlaneError("queued_ledger_evidence_stale")
                    if not trigger_game_ids:
                        trigger_game_ids = frozenset(
                            game.game_id
                            for game in games
                            if game.source_observation_id
                            in pending_source_observation_ids
                        )
                        trigger_game_id = next(
                            iter(sorted(trigger_game_ids)), None
                        )
                    # Publish the claim only after the complete input
                    # snapshot is loaded, avoiding a SQLite writer lock while
                    # repository reads establish the transaction snapshot.
                    for row in rows:
                        row.status = "running"
                        row.claimed_generation = int(row.generation or 1)
                        row.updated_at = self.clock()
                    session.flush()
                    claimed_streams = {
                        str(row["stream_key"])
                        for row in slice_jobs
                    }
                    if (
                        self.matchup_materialization is not None
                        and claimed_streams & _MATCHUP_STREAMS
                    ):
                        # Matchup facts, surface observations, ledger metadata,
                        # candidates, enabled publications, and pointer fences
                        # all use this same session transaction.
                        with (
                            self.matchup_materialization._runtime_write_authority(
                                session,
                                claimed_job_generations={
                                    str(row["job_id"]): int(row["generation"])
                                    for row in slice_jobs
                                },
                                season=season,
                                cutoff=cutoff,
                                manifest_id=manifest_id,
                            )
                            as issued
                        ):
                            write_authority = issued
                            self.matchup_materialization.materialize(
                                season,
                                as_of=cutoff.date(),
                                cutoff=cutoff,
                                recomposition_reason=reason,
                                affected_team_ids=(affected_team_ids or None),
                                trigger_game_id=trigger_game_id,
                                trigger_game_ids=(trigger_game_ids or None),
                                expected_game_ids=governance.expected_game_ids,
                                expected_l15_game_ids=governance.expected_l15_game_ids,
                                team_ids=governance.team_ids,
                                write_authority=write_authority,
                                claimed_streams=frozenset(claimed_streams),
                                session=session,
                            )
                    materialized = self.materialization.compose(
                        games,
                        season=season,
                        as_of=cutoff.date(),
                        cutoff=cutoff,
                        expected_game_ids=governance.expected_game_ids,
                        expected_l15_game_ids=governance.expected_l15_game_ids,
                        team_ids=governance.team_ids,
                        activate=self.publication_service is not None,
                        recomposition_reason=reason,
                        source_observation_ids_by_game=(
                            source_observation_ids_by_game or None
                        ),
                        session=session,
                    )
                    succeeded = set()
                    if materialized.season_window.complete:
                        succeeded |= {
                            "player_game_logs",
                            "traditional_opponent_season",
                            "player_per36",
                        }
                    if materialized.l15_window.complete:
                        succeeded |= {"traditional_opponent_l15"}
                    if (
                        materialized.assist_location_season is not None
                        and materialized.assist_location_l15 is not None
                    ):
                        if materialized.season_window.complete:
                            succeeded |= {"assist_locations_season"}
                        if materialized.l15_window.complete:
                            succeeded |= {"assist_locations_l15"}
                    cas_failed = False
                    for job in slice_jobs:
                        success = job["stream_key"] in succeeded
                        result = session.execute(update(table).where(
                            table.c.job_id == job["job_id"],
                            table.c.status == "running",
                            table.c.generation == job["generation"],
                            table.c.claimed_generation == job["generation"],
                        ).values(
                            status="succeeded" if success else "failed",
                            updated_at=self.clock(),
                            claimed_generation=None,
                            last_error=(
                                None if success
                                else _composition_failure_reason(job["stream_key"], materialized)
                            ),
                            # These columns are a pending-work envelope, not
                            # a second publication audit log.  Successful
                            # provenance is retained by PublicationObservation
                            # for the version that was composed; leaving it on
                            # a succeeded job makes reconciliation mistake an
                            # already-composed source for new work.
                            trigger_game_id=None if success else table.c.trigger_game_id,
                            trigger_game_ids="[]" if success else table.c.trigger_game_ids,
                            affected_team_ids="[]" if success else table.c.affected_team_ids,
                            source_observation_ids="[]" if success else table.c.source_observation_ids,
                            recomposition_reason=None if success else table.c.recomposition_reason,
                            ledger_checksum=None if success else table.c.ledger_checksum,
                            game_set_checksum=None if success else table.c.game_set_checksum,
                            ledger_evidence="{}" if success else table.c.ledger_evidence,
                        ))
                        if result.rowcount != 1:
                            cas_failed = True
                        else:
                            completed += int(success)
                    if cas_failed:
                        raise ControlPlaneError("stale_composition_generation")
            except (ControlPlaneError, LedgerMaterializationUnavailable, LedgerDerivationUnavailable):
                self._mark_slice_failed(
                    season=season,
                    cutoff=stored_cutoff,
                    manifest_id=manifest_id,
                    jobs=slice_jobs or (),
                    reason="recomposition_failed",
                )
                continue
            except Exception:
                self._mark_slice_failed(
                    season=season,
                    cutoff=stored_cutoff,
                    manifest_id=manifest_id,
                    jobs=slice_jobs or (),
                    reason="recomposition_failed",
                )
                logger.exception("unexpected ledger recomposition failure", extra={"season": season})
                raise
        return completed

    def _session_factory(self):
        publication_service = self.publication_service
        if publication_service is not None:
            return publication_service.session()
        return sessionmaker(bind=self.repository.engine, expire_on_commit=False)()

    def _mark_slice_failed(
        self,
        *,
        season: str,
        cutoff,
        manifest_id: str | None,
        jobs,
        reason: str,
    ) -> None:
        """Keep the last publication readable and leave a retryable marker."""
        now = self.clock()
        dedupe_key = f"ledger-recomposition:{season}:{cutoff}:{manifest_id or ''}"[:128]
        table = CompositionJob.__table__
        with self.repository.engine.begin() as connection:
            for job in jobs:
                connection.execute(update(table).where(
                    table.c.job_id == job["job_id"],
                    table.c.generation == job["generation"],
                    or_(
                        and_(
                            table.c.status == "running",
                            table.c.claimed_generation == job["generation"],
                        ),
                        and_(
                            table.c.status == "queued",
                            table.c.claimed_generation.is_(None),
                        ),
                    ),
                ).values(
                    status="failed",
                    attempts=table.c.attempts + 1,
                    updated_at=now,
                    last_error=reason,
                ))
            existing = connection.execute(select(ReconciliationItem.__table__).where(
                ReconciliationItem.__table__.c.dedupe_key == dedupe_key,
            )).mappings().first()
            details = json.dumps({
                "cutoff": str(cutoff),
                "manifest_id": manifest_id,
                "job_ids": [str(job["job_id"]) for job in jobs],
            }, sort_keys=True, separators=(",", ":"))
            if existing is None:
                connection.execute(ReconciliationItem.__table__.insert().values(
                    item_id=str(uuid4()),
                    season=season,
                    kind="ledger_recomposition",
                    reason=reason,
                    details=details,
                    dedupe_key=dedupe_key,
                    status="open",
                    created_at=now,
                ))
            else:
                connection.execute(update(ReconciliationItem.__table__).where(
                    ReconciliationItem.__table__.c.item_id == existing["item_id"],
                ).values(
                    status="open",
                    resolved_at=None,
                    details=details,
                ))


def _json_list(value) -> tuple[object, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if isinstance(parsed, list):
        return tuple(parsed)
    if isinstance(parsed, (str, int)) and not isinstance(parsed, bool):
        return (parsed,)
    return ()


__all__ = [
    "ActiveManifestLedgerGovernanceReader",
    "LedgerGovernance",
    "LedgerGovernanceReader",
    "LedgerRuntime",
]
