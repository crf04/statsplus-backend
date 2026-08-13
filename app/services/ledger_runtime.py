"""Executable orchestration for bounded ledger collection and composition."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.models.collection_control import CompositionJob
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import BackfillResult, LedgerBackfillService
from app.services.ledger_materialization import LedgerMaterializationService


class LedgerRuntime:
    """Run one resumable collection pass and queued materialization work."""

    def __init__(
        self,
        *,
        backfill: LedgerBackfillService,
        repository: CanonicalGameLedgerRepository,
        materialization: LedgerMaterializationService,
        clock=None,
    ) -> None:
        self.backfill = backfill
        self.repository = repository
        self.materialization = materialization
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
            games = tuple(
                game
                for summary in self.repository.list_games(season, through=cutoff.date())
                if (game := self.repository.get_game(summary.game_id)) is not None
            )
            expected = frozenset(game.game_id for game in games)
            teams = frozenset(
                fact.team_id for game in games for fact in game.team_facts
            )
            expected_l15 = {
                team_id: tuple(
                    game.game_id
                    for game in sorted(games, key=lambda item: (item.game_date, item.game_id), reverse=True)
                    if team_id in {game.home_team_id, game.away_team_id}
                )
                for team_id in teams
            }
            expected_l15 = {
                team_id: frozenset(game_ids[:15])
                for team_id, game_ids in expected_l15.items()
            }
            self.materialization.compose(
                games,
                season=season,
                as_of=cutoff.date(),
                expected_game_ids=expected,
                expected_l15_game_ids=expected_l15,
                team_ids=teams,
            )
            with self.repository.engine.begin() as connection:
                result = connection.execute(update(table).where(
                    table.c.season == season,
                    table.c.cutoff == cutoff,
                    table.c.status == "queued",
                ).values(status="succeeded", updated_at=self.clock()))
                completed += int(result.rowcount or 0)
        return completed


__all__ = ["LedgerRuntime"]
