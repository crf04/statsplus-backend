"""Executable orchestration for bounded ledger collection and composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import logging
from typing import Mapping, Protocol
from uuid import uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.orm import sessionmaker

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.nba_events import (
    is_completed_non_postponed_event,
)
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.slate_time import slate_date_for_instant, slate_day_bounds_utc
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    ReconciliationItem,
)
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import BackfillResult, LedgerBackfillService
from app.services.ledger_materialization import LedgerMaterialization, LedgerMaterializationService
from app.services.ledger_materialization import LedgerCorrectionQueue
from app.services.ledger_materialization import LedgerMaterializationUnavailable
from app.services.ledger_derivations import LedgerDerivationUnavailable
from app.services.ledger_lineage import LedgerLineage
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.team_matchup_publications import (
    NBA_PUBLICATION_STREAM_KEYS,
    PublicationGovernanceUnavailable,
)

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
    event_catalog_publication_id: str | None = None
    event_catalog_checksum: str | None = None

    @property
    def expected_season_game_ids(self) -> dict[int, frozenset[str]]:
        return {
            team_id: frozenset(
                str(event["nba_game_id"])
                for event in self.events
                if team_id in {
                    int(event["home_team_id"]),
                    int(event["away_team_id"]),
                }
            )
            for team_id in self.team_ids
        }

    @property
    def expected_l15_date_from_by_team(self) -> dict[int, str]:
        """Inclusive NBA endpoint boundary for each exact governed L15."""

        boundaries: dict[int, str] = {}
        for team_id, game_ids in self.expected_l15_game_ids.items():
            if len(game_ids) != 15:
                continue
            dates = [
                slate_date_for_instant(event["scheduled_at"])
                for event in self.events
                if str(event["nba_game_id"]) in game_ids
            ]
            if len(dates) == 15:
                boundaries[team_id] = min(dates).strftime("%m/%d/%Y")
        return boundaries


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
        return self._read(
            season,
            cutoff,
            require_collection_authorization=False,
        )

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

    def resolve_l15_game_ids(self, season: str, cutoff: date | datetime):
        """Return the exact governed L15 IDs for one season and cutoff.

        Query surfaces carry a calendar-date cutoff while operator activation
        carries the manifest's full timestamp.  Resolve the manifest timestamp
        for a date before reading the same active-manifest governance used by
        ledger composition; never derive an expectation from stored matchup
        facts.
        """

        return self.resolve_team_game_ids(season, cutoff, window="l15")

    def resolve_team_game_ids(
        self,
        season: str,
        cutoff: date | datetime,
        *,
        window: str,
        manifest_id: str | None = None,
        event_catalog_publication_id: str | None = None,
        event_catalog_checksum: str | None = None,
    ):
        governance = self._governance_at_cutoff(
            season,
            cutoff,
            manifest_id=manifest_id,
        )
        if (
            event_catalog_publication_id is not None
            and governance.event_catalog_publication_id
            != event_catalog_publication_id
        ) or (
            event_catalog_checksum is not None
            and governance.event_catalog_checksum != event_catalog_checksum
        ):
            raise PublicationGovernanceUnavailable(
                "active manifest and completed Event Catalog governance are required"
            )
        if window == "season":
            return governance.expected_season_game_ids
        if window == "l15":
            return governance.expected_l15_game_ids
        raise PublicationGovernanceUnavailable(
            "active manifest and completed Event Catalog governance are required"
        )

    def resolve_l15_date_from_by_team(
        self,
        season: str,
        cutoff: date | datetime,
        *,
        manifest_id: str | None = None,
        event_catalog_publication_id: str | None = None,
        event_catalog_checksum: str | None = None,
    ) -> dict[int, str]:
        governance = self._governance_at_cutoff(
            season, cutoff, manifest_id=manifest_id,
        )
        if (
            event_catalog_publication_id is not None
            and governance.event_catalog_publication_id
            != event_catalog_publication_id
        ) or (
            event_catalog_checksum is not None
            and governance.event_catalog_checksum != event_catalog_checksum
        ):
            raise PublicationGovernanceUnavailable()
        return governance.expected_l15_date_from_by_team

    def resolve_season_game_ids(self, season: str, cutoff: date | datetime):
        return self.resolve_team_game_ids(season, cutoff, window="season")

    def _governance_at_cutoff(
        self,
        season: str,
        cutoff: date | datetime,
        *,
        manifest_id: str | None = None,
    ) -> LedgerGovernance:
        """Resolve date shorthand to the exact immutable manifest cutoff."""

        if isinstance(cutoff, datetime):
            governed_cutoff = cutoff
        elif isinstance(cutoff, date):
            start, end = slate_day_bounds_utc(cutoff)
            with self.engine.connect() as connection:
                statement = select(CollectionManifest.cutoff).where(
                        CollectionManifest.season == season,
                        CollectionManifest.cutoff >= start,
                        CollectionManifest.cutoff < end,
                    )
                if manifest_id is not None:
                    statement = statement.where(
                        CollectionManifest.manifest_id == manifest_id
                    )
                governed_cutoff = connection.scalar(
                    statement.order_by(CollectionManifest.cutoff.desc()).limit(1)
                )
            if governed_cutoff is None:
                raise PublicationGovernanceUnavailable(
                    "active manifest and completed Event Catalog governance are required"
                )
            if governed_cutoff.tzinfo is None:
                governed_cutoff = governed_cutoff.replace(tzinfo=timezone.utc)
        else:
            raise PublicationGovernanceUnavailable(
                "active manifest and completed Event Catalog governance are required"
            )
        return self.read_for_composition(
            season,
            governed_cutoff,
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
        if (
            active is None
            or manifest is None
            or "canonical_game_ledger" not in set(json.loads(manifest["scopes"]))
            or 1 not in set(json.loads(manifest["accepted_versions"]))
        ):
            raise PublicationGovernanceUnavailable(
                "active manifest and completed Event Catalog governance are required"
            )
        if (
            manifest["event_catalog_publication_id"]
            and manifest["event_catalog_checksum"]
        ):
            events = self._immutable_catalog_events(manifest)
        else:
            raise PublicationGovernanceUnavailable(
                "active manifest and immutable Event Catalog governance are required"
            )
        if not events:
            raise PublicationGovernanceUnavailable(
                "completed Regular Season Event Catalog governance is required"
            )
        team_ids = frozenset(
            int(team_id)
            for event in events
            for team_id in (event["home_team_id"], event["away_team_id"])
        )
        expected = frozenset(str(event["nba_game_id"]) for event in events)
        by_team = {
            team_id: tuple(
                str(event["nba_game_id"])
                for event in reversed(events)
                if team_id in {
                    event["home_team_id"], event["away_team_id"]
                }
            )
            for team_id in team_ids
        }
        return LedgerGovernance(
            season=season,
            cutoff=cutoff,
            expected_game_ids=expected,
            team_ids=team_ids,
            expected_l15_game_ids={
                team_id: frozenset(game_ids[:15])
                for team_id, game_ids in by_team.items()
            },
            events=events,
            manifest_id=str(manifest["manifest_id"]),
            collect_before=(
                manifest["collect_before"].replace(tzinfo=timezone.utc)
                if manifest["collect_before"].tzinfo is None
                else manifest["collect_before"]
            ),
            accepted_versions=frozenset(int(value) for value in json.loads(manifest["accepted_versions"])),
            event_catalog_publication_id=manifest[
                "event_catalog_publication_id"
            ],
            event_catalog_checksum=manifest["event_catalog_checksum"],
        )

    def _immutable_catalog_events(self, manifest) -> tuple[Mapping[str, object], ...]:
        """Read the exact Event Catalog snapshot bound to this manifest."""

        with self.engine.connect() as connection:
            publication_id = manifest.get("event_catalog_publication_id")
            catalog = (
                connection.execute(
                    select(CatalogPublication.__table__).where(
                        CatalogPublication.publication_id == publication_id,
                    )
                ).mappings().one_or_none()
                if publication_id
                else None
            )
        if catalog is None:
            raise PublicationGovernanceUnavailable(
                "active manifest and immutable Event Catalog governance are required"
            )
        payload = catalog["payload"]
        if (
            not isinstance(payload, str)
            or catalog["publication_id"]
            != manifest["event_catalog_publication_id"]
            or catalog["checksum"] != manifest.get("event_catalog_checksum")
            or catalog["season"] != manifest["season"]
            or catalog["catalog_type"] != "event"
            or catalog["cutoff"] != manifest["cutoff"]
            or not catalog["complete"]
            or not publication_payload_matches_checksum(payload, catalog["checksum"])
        ):
            raise PublicationGovernanceUnavailable(
                "immutable Event Catalog governance is inconsistent"
            )
        try:
            document = json.loads(payload)
            rows = document.get("events", document.get("games"))
        except (AttributeError, TypeError, json.JSONDecodeError):
            raise PublicationGovernanceUnavailable(
                "immutable Event Catalog governance is inconsistent"
            ) from None
        if not isinstance(rows, list) or not rows:
            raise PublicationGovernanceUnavailable(
                "immutable Event Catalog governance is inconsistent"
            )
        manifest_cutoff = manifest["cutoff"]
        if manifest_cutoff.tzinfo is None:
            manifest_cutoff = manifest_cutoff.replace(tzinfo=timezone.utc)
        seen: set[str] = set()
        eligible = []
        for row in rows:
            try:
                raw_game_id = row.get(
                    "nba_game_id", row.get("game_id", row.get("id"))
                )
                if raw_game_id in (None, ""):
                    raise PublicationGovernanceUnavailable("event identity required")
                game_id = str(raw_game_id).strip()
                home_team_id = int(row["home_team_id"])
                away_team_id = int(row["away_team_id"])
                home_team_tricode = NBA_TEAM_ID_TO_TRICODE.get(home_team_id)
                away_team_tricode = NBA_TEAM_ID_TO_TRICODE.get(away_team_id)
                if home_team_tricode is None:
                    home_team_tricode = str(row.get("home_team_tricode") or "").strip().upper()
                if away_team_tricode is None:
                    away_team_tricode = str(row.get("away_team_tricode") or "").strip().upper()
                phase = str(
                    row.get("phase", row.get("season_phase", row.get("season_type")))
                ).strip().lower().replace("_", " ")
                scheduled_text = row.get(
                    "scheduled_at", row.get("date", row.get("game_date"))
                )
                scheduled_at = datetime.fromisoformat(
                    str(scheduled_text).replace("Z", "+00:00")
                )
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                raise PublicationGovernanceUnavailable(
                    "immutable Event Catalog governance is inconsistent"
                ) from None
            if (
                not game_id
                or game_id in seen
                or home_team_id <= 0
                or away_team_id <= 0
                or home_team_id == away_team_id
                or phase not in {"regular season", "regular"}
                or scheduled_at.tzinfo is None
            ):
                raise PublicationGovernanceUnavailable(
                    "immutable Event Catalog governance is inconsistent"
                )
            seen.add(game_id)
            if (
                is_completed_non_postponed_event(row)
                and scheduled_at <= manifest_cutoff
            ):
                eligible.append({
                    **dict(row),
                    "nba_game_id": game_id,
                    "season": str(manifest["season"]),
                    "phase": "Regular Season",
                    "classification": "Regular Season",
                    "home_team_id": home_team_id,
                    **({"home_team_tricode": home_team_tricode} if home_team_tricode else {}),
                    "away_team_id": away_team_id,
                    **({"away_team_tricode": away_team_tricode} if away_team_tricode else {}),
                    "scheduled_at": scheduled_at,
                })
        return tuple(sorted(
            eligible,
            key=lambda event: (event["scheduled_at"], event["nba_game_id"]),
        ))

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
            raise PublicationGovernanceUnavailable(
                "active manifest and completed Event Catalog governance are required"
            )
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


def _succeeded_ledger_streams(
    materialization: LedgerMaterialization,
) -> set[str]:
    succeeded = set()
    if materialization.season_window.complete:
        succeeded |= {
            "player_game_logs",
            "traditional_opponent_season",
            "player_per36",
        }
    if materialization.l15_window.complete:
        succeeded.add("traditional_opponent_l15")
    if (
        materialization.assist_location_season is not None
        and materialization.assist_location_l15 is not None
    ):
        if materialization.season_window.complete:
            succeeded.add("assist_locations_season")
        if materialization.l15_window.complete:
            succeeded.add("assist_locations_l15")
    return succeeded


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
        publication_service: PublicationService | None = None,
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
                    nba_jobs = tuple(
                        row for row in slice_jobs
                        if row["stream_key"] in NBA_PUBLICATION_STREAM_KEYS
                    )
                    ledger_jobs = tuple(
                        row for row in slice_jobs
                        if row["stream_key"] not in NBA_PUBLICATION_STREAM_KEYS
                    )
                    active_jobs = ledger_jobs or slice_jobs
                    reason = next(
                        (
                            str(row["recomposition_reason"])
                            for row in active_jobs
                            if row.get("recomposition_reason")
                        ),
                        "scheduled_reconciliation",
                    )
                    affected_team_ids = frozenset(
                        int(team_id)
                        for row in active_jobs
                        for team_id in _json_list(row.get("affected_team_ids"))
                        if str(team_id).isdigit()
                    )
                    trigger_game_ids = frozenset(
                        str(game_id)
                        for row in active_jobs
                        for game_id in _json_list(row.get("trigger_game_ids"))
                    )
                    if not trigger_game_ids:
                        trigger_game_ids = frozenset(
                            str(row["trigger_game_id"])
                            for row in active_jobs
                            if row.get("trigger_game_id")
                        )
                    trigger_game_id = next(iter(sorted(trigger_game_ids)), None)
                    pending_source_observation_ids = {
                        str(source_observation_id)
                        for row in active_jobs
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
                    composition_as_of = slate_date_for_instant(cutoff)
                    read_connection = session.connection()
                    try:
                        summaries = self.repository.list_games(
                            season,
                            through=composition_as_of,
                            connection=read_connection,
                        )
                    except TypeError as error:
                        if "connection" not in str(error):
                            raise
                        summaries = self.repository.list_games(
                            season,
                            through=composition_as_of,
                        )
                    games = tuple(
                        game
                        for summary in summaries
                        if (game := self.repository.get_game(
                            summary.game_id,
                            connection=read_connection,
                        )) is not None
                    )
                    games_by_id = {game.game_id: game for game in games}
                    for row in ledger_jobs:
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
                    nba_succeeded_streams: set[str] = set()
                    nba_failures: dict[str, str] = {}
                    publication_service = self.publication_service
                    if nba_jobs and publication_service is None:
                        publication_service = PublicationService(
                            self.repository.engine,
                            l15_expectation_resolver=self.governance,
                        )
                    for job in nba_jobs:
                        try:
                            if not manifest_id:
                                raise ControlPlaneError(
                                    "publication_governance_unavailable"
                                )
                            with session.begin_nested():
                                publication_service.compose_from_observations(
                                    job["stream_key"],
                                    season=season,
                                    cutoff=cutoff,
                                    manifest_id=manifest_id,
                                    session=session,
                                )
                        except ControlPlaneError as error:
                            nba_failures[job["job_id"]] = str(error)[:255]
                        else:
                            nba_succeeded_streams.add(job["stream_key"])
                    if nba_jobs and not ledger_jobs:
                        if (
                            nba_succeeded_streams
                            and self.matchup_materialization is not None
                        ):
                            self.matchup_materialization.refresh_publication_surfaces(
                                season,
                                as_of=slate_date_for_instant(cutoff),
                                expected_game_ids_by_team=(
                                    governance.expected_season_game_ids
                                ),
                                expected_l15_game_ids=(
                                    governance.expected_l15_game_ids
                                ),
                                team_ids=governance.team_ids,
                                session=session,
                            )
                        cas_failed = False
                        for job in nba_jobs:
                            success = job["stream_key"] in nba_succeeded_streams
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
                                    else nba_failures.get(
                                        job["job_id"],
                                        "publication_composition_failed",
                                    )
                                ),
                            ))
                            if result.rowcount != 1:
                                cas_failed = True
                            else:
                                completed += int(success)
                        if cas_failed:
                            raise ControlPlaneError(
                                "stale_composition_generation"
                            )
                        continue
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
                                as_of=composition_as_of,
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
                        as_of=composition_as_of,
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
                        if job["stream_key"] in NBA_PUBLICATION_STREAM_KEYS:
                            success = job["stream_key"] in nba_succeeded_streams
                            failure_reason = nba_failures.get(
                                job["job_id"],
                                "publication_composition_failed",
                            )
                        else:
                            success = job["stream_key"] in succeeded
                            failure_reason = _composition_failure_reason(
                                job["stream_key"], materialized
                            )
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
                                else failure_reason
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
