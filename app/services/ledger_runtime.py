"""Executable orchestration for bounded ledger collection and composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Mapping, Protocol

from sqlalchemy import select, update

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.nba_events import is_completed_non_postponed_event
from app.domain.slate_time import slate_date_for_instant, slate_day_bounds_utc
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
    CompositionJob,
)
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import BackfillResult, LedgerBackfillService
from app.services.ledger_materialization import LedgerMaterialization, LedgerMaterializationService
from app.services.team_matchup_publications import NBA_PUBLICATION_STREAM_KEYS


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
            raise ValueError(
                "active manifest and completed Event Catalog governance are required"
            )
        if window == "season":
            return governance.expected_season_game_ids
        if window == "l15":
            return governance.expected_l15_game_ids
        raise ValueError("active manifest and completed Event Catalog governance are required")

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
                raise ValueError("active manifest and completed Event Catalog governance are required")
            if governed_cutoff.tzinfo is None:
                governed_cutoff = governed_cutoff.replace(tzinfo=timezone.utc)
        else:
            raise ValueError("active manifest and completed Event Catalog governance are required")
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
            raise ValueError("active manifest and completed Event Catalog governance are required")
        events = self._immutable_catalog_events(manifest)
        if not events:
            raise ValueError("completed Regular Season Event Catalog governance is required")
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
            raise ValueError(
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
            raise ValueError("immutable Event Catalog governance is inconsistent")
        try:
            document = json.loads(payload)
            rows = document.get("events", document.get("games"))
        except (AttributeError, TypeError, json.JSONDecodeError):
            raise ValueError("immutable Event Catalog governance is inconsistent") from None
        if not isinstance(rows, list) or not rows:
            raise ValueError("immutable Event Catalog governance is inconsistent")
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
                    raise ValueError("event identity required")
                game_id = str(raw_game_id).strip()
                home_team_id = int(row["home_team_id"])
                away_team_id = int(row["away_team_id"])
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
                raise ValueError(
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
                raise ValueError("immutable Event Catalog governance is inconsistent")
            seen.add(game_id)
            if (
                is_completed_non_postponed_event(row)
                and scheduled_at <= manifest_cutoff
            ):
                eligible.append({
                    "nba_game_id": game_id,
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
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
        publication_service: PublicationService | None = None,
        clock=None,
    ) -> None:
        self.backfill = backfill
        self.repository = repository
        self.materialization = materialization
        self.governance = governance
        self.matchup_materialization = matchup_materialization
        self.publication_service = publication_service
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
            slice_jobs = [
                row for row in jobs
                if row["cutoff"] == stored_cutoff
                and row["manifest_id"] == manifest_id
            ]
            nba_jobs = [
                row for row in slice_jobs
                if row["stream_key"] in NBA_PUBLICATION_STREAM_KEYS
            ]
            publication_service = self.publication_service
            if nba_jobs and publication_service is None:
                publication_service = PublicationService(
                    self.repository.engine,
                    l15_expectation_resolver=self.governance,
                )
            ledger_jobs = [
                row for row in slice_jobs
                if row["stream_key"] not in NBA_PUBLICATION_STREAM_KEYS
            ]
            if (
                nba_jobs
                and not ledger_jobs
                and self.matchup_materialization is not None
            ):
                succeeded_jobs = []
                failed_jobs = {}
                try:
                    with publication_service.session() as session, session.begin():
                        # Establish the outer write transaction before SQLite
                        # savepoints; otherwise releasing the first savepoint
                        # can commit it independently of a later projection
                        # rollback.
                        session.execute(update(table).where(
                            table.c.job_id.in_([
                                job["job_id"] for job in nba_jobs
                            ]),
                        ).values(updated_at=self.clock()))
                        for job in nba_jobs:
                            try:
                                if not manifest_id:
                                    raise ControlPlaneError(
                                        "publication_governance_unavailable"
                                    )
                                with session.begin_nested():
                                    publication_service.compose_from_observations(
                                        job["stream_key"], season=season,
                                        cutoff=cutoff, manifest_id=manifest_id,
                                        session=session,
                                    )
                            except (ControlPlaneError, ValueError) as error:
                                failed_jobs[job["job_id"]] = str(error)[:255]
                            else:
                                succeeded_jobs.append(job)
                        if succeeded_jobs:
                            governance = self.governance.read_for_composition(
                                season, cutoff, manifest_id,
                            )
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
                        now = self.clock()
                        if succeeded_jobs:
                            session.execute(update(table).where(
                                table.c.job_id.in_([
                                    job["job_id"] for job in succeeded_jobs
                                ]),
                            ).values(
                                status="succeeded", updated_at=now,
                                last_error=None,
                            ))
                        for job_id, last_error in failed_jobs.items():
                            session.execute(update(table).where(
                                table.c.job_id == job_id,
                            ).values(
                                status="failed", updated_at=now,
                                last_error=last_error,
                            ))
                except Exception as error:
                    # Publication pointers, projections, and Matchups rows were
                    # rolled back together.  Keep successfully composed jobs
                    # queued so the existing unique job can be retried.
                    with self.repository.engine.begin() as connection:
                        if succeeded_jobs:
                            connection.execute(update(table).where(
                                table.c.job_id.in_([
                                    job["job_id"] for job in succeeded_jobs
                                ]),
                            ).values(
                                status="queued", updated_at=self.clock(),
                                last_error=str(error)[:255],
                            ))
                        for job_id, last_error in failed_jobs.items():
                            connection.execute(update(table).where(
                                table.c.job_id == job_id,
                            ).values(
                                status="failed", updated_at=self.clock(),
                                last_error=last_error,
                            ))
                else:
                    completed += len(succeeded_jobs)
                continue
            nba_succeeded = False
            for job in nba_jobs:
                try:
                    if not manifest_id:
                        raise ControlPlaneError("publication_governance_unavailable")
                    publication_service.compose_from_observations(
                        job["stream_key"], season=season, cutoff=cutoff,
                        manifest_id=manifest_id,
                    )
                except (ControlPlaneError, ValueError) as error:
                    status, last_error = "failed", str(error)[:255]
                else:
                    status, last_error = "succeeded", None
                    nba_succeeded = True
                    completed += 1
                with self.repository.engine.begin() as connection:
                    connection.execute(update(table).where(
                        table.c.job_id == job["job_id"],
                    ).values(
                        status=status, updated_at=self.clock(),
                        last_error=last_error,
                    ))
            if not ledger_jobs:
                if nba_succeeded and self.matchup_materialization is not None:
                    governance = self.governance.read_for_composition(
                        season, cutoff, manifest_id,
                    )
                    self.matchup_materialization.refresh_publication_surfaces(
                        season,
                        as_of=slate_date_for_instant(cutoff),
                        expected_game_ids_by_team=(
                            governance.expected_season_game_ids
                        ),
                        expected_l15_game_ids=governance.expected_l15_game_ids,
                        team_ids=governance.team_ids,
                    )
                continue
            governance = self.governance.read_for_composition(
                season,
                cutoff,
                manifest_id,
            )
            slate_date = slate_date_for_instant(cutoff)
            if self.matchup_materialization is not None:
                # Publish the disposable ledger-owned matchup read model at the
                # exact composition cutoff before composing publication streams,
                # so an incomplete Season/L15 publishes explicit unavailable
                # observations instead of approximating a league window.
                self.matchup_materialization.materialize(
                    season,
                    as_of=slate_date,
                    expected_game_ids=governance.expected_game_ids,
                    expected_l15_game_ids=governance.expected_l15_game_ids,
                    team_ids=governance.team_ids,
                )
            games = tuple(
                game
                for summary in self.repository.list_games(season, through=slate_date)
                if (game := self.repository.get_game(summary.game_id)) is not None
            )
            materialized = self.materialization.compose(
                games,
                season=season,
                as_of=slate_date,
                cutoff=cutoff,
                expected_game_ids=governance.expected_game_ids,
                expected_l15_game_ids=governance.expected_l15_game_ids,
                team_ids=governance.team_ids,
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
            with self.repository.engine.begin() as connection:
                for job in ledger_jobs:
                    success = job["stream_key"] in succeeded
                    connection.execute(update(table).where(
                        table.c.job_id == job["job_id"],
                    ).values(
                        status="succeeded" if success else "failed",
                        updated_at=self.clock(),
                        last_error=(
                            None if success
                            else _composition_failure_reason(job["stream_key"], materialized)
                        ),
                    ))
                    completed += int(success)
        return completed


__all__ = [
    "ActiveManifestLedgerGovernanceReader",
    "LedgerGovernance",
    "LedgerGovernanceReader",
    "LedgerRuntime",
]
