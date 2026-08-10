"""Build the slate Player Pool from one live, normalized DFS board."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from app.domain.statistics import MatchState, ScoringPeriod
from app.providers.dfs import MarketStatus, MarketVariant, NBAMarketQuery
from app.services.statistic_catalog import StatisticCatalog
from app.utils.telemetry import (
    BoundedPlayerPoolTelemetryRecorder,
    PlayerPoolTelemetryRecorder,
    PlayerPoolTelemetryEvent,
)


@dataclass(frozen=True, slots=True)
class PoolPlayer:
    """One canonical targetable player and source-board provenance."""

    canonical_player_id: int
    name: str
    team_id: int
    market_categories: tuple[str, ...]
    provenance: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PlayerPool:
    """The targetable union and the provider observations that produced it."""

    players: tuple[PoolPlayer, ...]
    team_counts: Mapping[int, int]
    freshness: Mapping[str, Any]

    @staticmethod
    def unavailable_freshness() -> dict[str, Any]:
        return {"status": "unavailable", "retrieved_at": None, "providers": {}}


@dataclass(slots=True)
class _PlayerContribution:
    team_id: int
    name: str
    providers: dict[str, set[str]]


class PlayerPoolService:
    """Collect a live DFS board and retain only qualifying joined markets."""

    def __init__(
        self,
        board_service: Any,
        statistic_catalog: StatisticCatalog,
        *,
        telemetry_recorder: PlayerPoolTelemetryRecorder | None = None,
    ) -> None:
        if not callable(getattr(board_service, "get_board", None)):
            raise TypeError("player pool board service must expose get_board")
        self.board_service = board_service
        if not isinstance(statistic_catalog, StatisticCatalog):
            raise TypeError("player pool requires a StatisticCatalog")
        self.market_categories = {
            statistic.id: statistic.market_category
            for statistic in statistic_catalog.statistics
            if statistic.comparable and statistic.market_category is not None
        }
        self.telemetry_recorder = (
            telemetry_recorder or BoundedPlayerPoolTelemetryRecorder()
        )

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        board = self.board_service.get_board(
            NBAMarketQuery(
                season=season,
                market_statuses=(MarketStatus.AVAILABLE,),
            )
        )
        slate_games = frozenset(str(game_id) for game_id in game_ids)
        athlete_mappings = self._athlete_mappings(board)
        event_mappings = self._event_mappings(board)
        contributions: dict[int, _PlayerContribution] = {}
        unknown_label_count = 0
        unjoined_athletes: set[tuple[str, str | None]] = set()

        for provider_outcome in board.provider_outcomes:
            if not provider_outcome.usable or provider_outcome.snapshot is None:
                continue
            provider = provider_outcome.provider
            for market in provider_outcome.snapshot.markets:
                if not self._qualifying_shape(market):
                    continue
                category = self._market_category(market)
                if category is None:
                    unknown_label_count += 1
                    continue
                event_key = self._evidence_key(provider, market.event)
                if event_mappings.get(event_key) not in slate_games:
                    continue
                athlete_key = self._evidence_key(provider, market.athlete)
                canonical = athlete_mappings.get(athlete_key)
                if canonical is None:
                    unjoined_athletes.add(athlete_key)
                    continue
                player_id, team_id, name = canonical
                entry = contributions.setdefault(
                    player_id,
                    _PlayerContribution(team_id=team_id, name=name, providers={}),
                )
                categories = entry.providers.setdefault(provider, set())
                categories.add(category)

        players = tuple(
            PoolPlayer(
                canonical_player_id=player_id,
                name=entry.name,
                team_id=entry.team_id,
                market_categories=tuple(
                    sorted(
                        {
                            category
                            for categories in entry.providers.values()
                            for category in categories
                        }
                    )
                ),
                provenance={
                    provider: tuple(sorted(categories))
                    for provider, categories in sorted(entry.providers.items())
                },
            )
            for player_id, entry in sorted(contributions.items())
        )
        team_counts: dict[int, int] = {}
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1

        self.telemetry_recorder.record(
            PlayerPoolTelemetryEvent(
                unknown_stat_label_count=unknown_label_count,
                unjoined_athlete_count=len(unjoined_athletes),
            )
        )
        return PlayerPool(
            players=players,
            team_counts=team_counts,
            freshness=self._freshness(board.provider_outcomes),
        )

    @staticmethod
    def _qualifying_shape(market: Any) -> bool:
        return (
            market.status is MarketStatus.AVAILABLE
            and market.variant is MarketVariant.STANDARD
            and market.scoring_period is ScoringPeriod.FULL_GAME
        )

    def _market_category(self, market: Any) -> str | None:
        match = market.statistic_match
        if (
            match is None
            or match.state is not MatchState.CANONICAL
            or not match.is_comparable
        ):
            return None
        return self.market_categories.get(match.canonical_id)

    @staticmethod
    def _evidence_key(provider: str, evidence: Any | None) -> tuple[str, str | None]:
        provider_id = (
            None if evidence is None else getattr(evidence, "provider_id", None)
        )
        return provider, provider_id

    @classmethod
    def _athlete_mappings(
        cls, board: Any
    ) -> dict[tuple[str, str | None], tuple[int, int, str]]:
        result = {}
        for outcome in board.mapping_outcomes:
            player_id = getattr(outcome, "canonical_player_id", None)
            resolution = getattr(outcome, "resolution", None)
            canonical = getattr(resolution, "canonical_athlete", None)
            provider_id = getattr(outcome, "provider_athlete_id", None)
            if provider_id is None:
                provider_id = getattr(resolution, "provider_athlete_id", None)
            if (
                player_id is None
                or canonical is None
                or canonical.team_id is None
                or canonical.player_id != player_id
            ):
                continue
            result[(resolution.provider, provider_id)] = (
                int(player_id),
                int(canonical.team_id),
                str(canonical.display_name),
            )
        return result

    @staticmethod
    def _event_mappings(board: Any) -> dict[tuple[str, str | None], str]:
        result = {}
        for outcome in board.event_mapping_outcomes:
            resolution = getattr(outcome, "resolution", None)
            provider_id = getattr(outcome, "provider_event_id", None)
            if provider_id is None:
                provider_id = getattr(resolution, "provider_event_id", None)
            game_id = getattr(outcome, "canonical_event_id", None)
            if game_id is not None:
                result[(resolution.provider, provider_id)] = str(game_id)
        return result

    @staticmethod
    def _freshness(outcomes: Iterable[Any]) -> dict[str, Any]:
        providers = {}
        retrieved: list[datetime] = []
        for outcome in sorted(outcomes, key=lambda value: value.provider):
            if outcome.usable and outcome.snapshot is not None:
                observed_at = outcome.snapshot.retrieved_at.astimezone(timezone.utc)
                retrieved.append(observed_at)
                providers[outcome.provider] = {
                    "status": "fresh",
                    "retrieved_at": observed_at.isoformat(),
                }
            else:
                providers[outcome.provider] = {
                    "status": "missing",
                    "retrieved_at": None,
                }
        freshness = {
            "retrieved_at": min(retrieved).isoformat() if retrieved else None,
            "providers": providers,
        }
        if not retrieved:
            freshness["status"] = "unavailable"
        elif len(retrieved) == len(providers):
            freshness["status"] = "fresh"
        return freshness


__all__ = ["PlayerPool", "PlayerPoolService", "PoolPlayer"]
