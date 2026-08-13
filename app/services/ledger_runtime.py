"""Executable orchestration for bounded ledger collection and composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select, update

from app.models.collection_control import ActiveSeason, CollectionManifest, CompositionJob
from app.models.event_catalog import EventCatalogEntry
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


class LedgerGovernanceReader(Protocol):
    def read(self, season: str, cutoff: datetime) -> LedgerGovernance: ...


class ActiveManifestLedgerGovernanceReader:
    """Derive exact composition truth only from active control-plane state."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def read(self, season: str, cutoff: datetime) -> LedgerGovernance:
        with self.engine.connect() as connection:
            active = connection.execute(select(ActiveSeason).where(
                ActiveSeason.season == season,
                ActiveSeason.status == "active",
                ActiveSeason.phase == "Regular Season",
            )).first()
            manifest = connection.execute(select(CollectionManifest).where(
                CollectionManifest.season == season,
                CollectionManifest.cutoff == cutoff,
                CollectionManifest.status == "active",
            )).first()
            events = connection.execute(select(EventCatalogEntry).where(
                EventCatalogEntry.season == season,
                EventCatalogEntry.classification == "Regular Season",
                EventCatalogEntry.status_code == 3,
                EventCatalogEntry.scheduled_at <= cutoff,
            ).order_by(EventCatalogEntry.scheduled_at, EventCatalogEntry.nba_game_id)).mappings().all()
        if active is None or manifest is None or not events:
            raise ValueError("active manifest and completed Event Catalog governance are required")
        team_ids = frozenset(
            int(team_id)
            for event in events
            for team_id in (event["home_team_id"], event["away_team_id"])
        )
        if len(team_ids) != 30:
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
        if any(len(game_ids) < 15 for game_ids in by_team.values()):
            raise ValueError("governed L15 requires 15 games for every team")
        return LedgerGovernance(
            season=season,
            cutoff=cutoff,
            expected_game_ids=expected,
            team_ids=team_ids,
            expected_l15_game_ids={
                team_id: frozenset(game_ids[:15]) for team_id, game_ids in by_team.items()
            },
        )


class LedgerRuntime:
    """Run one resumable collection pass and queued materialization work."""

    def __init__(
        self,
        *,
        backfill: LedgerBackfillService,
        repository: CanonicalGameLedgerRepository,
        materialization: LedgerMaterializationService,
        governance: LedgerGovernanceReader,
        clock=None,
    ) -> None:
        self.backfill = backfill
        self.repository = repository
        self.materialization = materialization
        self.governance = governance
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(
        self,
        season: str,
        *,
        max_games: int | None = None,
        historical_repair: bool = False,
    ) -> BackfillResult:
        return self.backfill.refresh(
            season,
            max_games=max_games,
            historical_repair=historical_repair,
        )

    def compose_queued(self, season: str) -> int:
        table = CompositionJob.__table__
        with self.repository.engine.connect() as connection:
            jobs = connection.execute(select(table).where(
                table.c.season == season,
                table.c.status == "queued",
            ).order_by(table.c.cutoff, table.c.created_at)).mappings().all()
        cutoffs = sorted({row["cutoff"] for row in jobs})
        completed = 0
        for cutoff in cutoffs:
            governance = self.governance.read(season, cutoff)
            games = tuple(
                game
                for summary in self.repository.list_games(season, through=cutoff.date())
                if (game := self.repository.get_game(summary.game_id)) is not None
            )
            self.materialization.compose(
                games,
                season=season,
                as_of=cutoff.date(),
                expected_game_ids=governance.expected_game_ids,
                expected_l15_game_ids=governance.expected_l15_game_ids,
                team_ids=governance.team_ids,
            )
            with self.repository.engine.begin() as connection:
                result = connection.execute(update(table).where(
                    table.c.season == season,
                    table.c.cutoff == cutoff,
                    table.c.status == "queued",
                ).values(status="succeeded", updated_at=self.clock()))
                completed += int(result.rowcount or 0)
        return completed


__all__ = [
    "ActiveManifestLedgerGovernanceReader",
    "LedgerGovernance",
    "LedgerGovernanceReader",
    "LedgerRuntime",
]
