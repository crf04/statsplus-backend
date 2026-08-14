"""Executable orchestration for bounded ledger collection and composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping, Protocol

from sqlalchemy import select, update

from app.models.collection_control import ActiveSeason, CollectionManifest, CompositionJob
from app.models.event_catalog import EventCatalogEntry
from app.domain.nba_events import is_final_event, is_postponed_event
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import BackfillResult, LedgerBackfillService
from app.services.ledger_materialization import LedgerMaterializationService


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
            require_l15=True,
            require_collection_authorization=False,
            manifest_id=manifest_id,
        )

    def _read(
        self,
        season: str,
        cutoff: datetime,
        *,
        require_l15: bool,
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
        if require_l15 and len(team_ids) != 30:
            raise ValueError("governed Event Catalog must contain exactly 30 teams")
        expected = frozenset(str(event["nba_game_id"]) for event in events)
        by_team = {
            team_id: tuple(
                str(event["nba_game_id"])
                for event in reversed(events)
                if team_id in {event["home_team_id"], event["away_team_id"]}
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
            require_l15=False,
            require_collection_authorization=True,
            manifest_id=None,
        )

    # Compatibility alias for internal callers written before collection and
    # composition authorization became distinct operations.
    read_active = read_for_collection


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
        clock=None,
    ) -> None:
        self.backfill = backfill
        self.repository = repository
        self.materialization = materialization
        self.governance = governance
        self.matchup_materialization = matchup_materialization
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
            jobs = connection.execute(select(table).where(
                table.c.season == season,
                table.c.status == "queued",
            ).order_by(table.c.cutoff, table.c.created_at)).mappings().all()
        slices = sorted({
            (row["cutoff"], row["manifest_id"])
            for row in jobs
        }, key=lambda item: (item[0], item[1] or ""))
        completed = 0
        for stored_cutoff, manifest_id in slices:
            cutoff = (
                stored_cutoff.replace(tzinfo=timezone.utc)
                if stored_cutoff.tzinfo is None
                else stored_cutoff
            )
            governance = self.governance.read_for_composition(
                season,
                cutoff,
                manifest_id,
            )
            if self.matchup_materialization is not None:
                # Publish the disposable ledger-owned matchup read model at the
                # exact composition cutoff before composing publication streams,
                # so an incomplete Season/L15 publishes explicit unavailable
                # observations instead of approximating a league window.
                self.matchup_materialization.materialize(
                    season,
                    as_of=cutoff.date(),
                    expected_game_ids=governance.expected_game_ids,
                    expected_l15_game_ids=governance.expected_l15_game_ids,
                    team_ids=governance.team_ids,
                )
            games = tuple(
                game
                for summary in self.repository.list_games(season, through=cutoff.date())
                if (game := self.repository.get_game(summary.game_id)) is not None
            )
            materialized = self.materialization.compose(
                games,
                season=season,
                as_of=cutoff.date(),
                cutoff=cutoff,
                expected_game_ids=governance.expected_game_ids,
                expected_l15_game_ids=governance.expected_l15_game_ids,
                team_ids=governance.team_ids,
            )
            succeeded = {
                "player_game_logs",
                "traditional_opponent_season",
                "traditional_opponent_l15",
                "player_per36",
            }
            if (
                materialized.assist_location_season is not None
                and materialized.assist_location_l15 is not None
            ):
                succeeded |= {"assist_locations_season", "assist_locations_l15"}
            with self.repository.engine.begin() as connection:
                for job in (
                    row for row in jobs
                    if row["cutoff"] == stored_cutoff
                    and row["manifest_id"] == manifest_id
                ):
                    success = job["stream_key"] in succeeded
                    connection.execute(update(table).where(
                        table.c.job_id == job["job_id"],
                    ).values(
                        status="succeeded" if success else "failed",
                        updated_at=self.clock(),
                        last_error=None if success else "assist_location_evidence_incomplete",
                    ))
                    completed += int(success)
        return completed


__all__ = [
    "ActiveManifestLedgerGovernanceReader",
    "LedgerGovernance",
    "LedgerGovernanceReader",
    "LedgerRuntime",
]
