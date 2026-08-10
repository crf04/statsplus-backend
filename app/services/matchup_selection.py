"""Stored-log matchup selection tables and per-market rate deltas."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
    PlayerSeasonRate,
)
from app.services.player_game_log_values import (
    selected_player_game_log_market_values,
    validate_player_game_log_components,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog


_WIRE_PRECISION = 6


class EventCatalogReader(Protocol):
    def count_events(self, season: str) -> int: ...

    def get_events(self, season: str) -> Sequence[Mapping[str, Any]]: ...


class ArchetypeReader(Protocol):
    def list_peer_ids(self, player_id: int) -> tuple[int, ...]: ...


class StoredPlayerPoolReader(Protocol):
    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool | None: ...


@dataclass(frozen=True, slots=True)
class _RatedRow:
    record: PlayerGameLogRecord
    values: Mapping[str, float]
    deltas: Mapping[str, float]
    rates: Mapping[str, float]


class MatchupSelectionService:
    """Build one selection response without request-time stats-provider reads."""

    def __init__(
        self,
        *,
        event_catalog: EventCatalogReader | None,
        player_pool: StoredPlayerPoolReader | None,
        player_logs: PlayerGameLogRepository,
        archetypes: ArchetypeReader,
        statistic_catalog: StatisticCatalog,
        settings: RuntimeSettings,
        h2h_thin_min_games: int | None = None,
        archetype_thin_min_games: int | None = None,
    ) -> None:
        h2h_thin_min_games = (
            settings.catalog.matchup_selection_h2h_min_games
            if h2h_thin_min_games is None
            else h2h_thin_min_games
        )
        archetype_thin_min_games = (
            settings.catalog.matchup_selection_archetype_min_games
            if archetype_thin_min_games is None
            else archetype_thin_min_games
        )
        if h2h_thin_min_games < 1 or archetype_thin_min_games < 1:
            raise ValueError("matchup selection thin thresholds must be positive")
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        self.player_logs = player_logs
        self.archetypes = archetypes
        self.settings = settings
        self.h2h_thin_min_games = h2h_thin_min_games
        self.archetype_thin_min_games = archetype_thin_min_games
        self._statistics = {
            statistic.market_category: statistic
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        }
        validate_player_game_log_components(
            (
                component
                for statistic in self._statistics.values()
                for component in statistic.components
            ),
            stored_components=PlayerGameLogRecord.__dataclass_fields__,
        )

    def get_selection(self, *, game_id: str, player_id: int) -> dict[str, Any]:
        season = self.settings.nba.current_season
        event = self._event(season, game_id)
        pool = (
            None
            if self.player_pool is None
            else self.player_pool.get_pool_for_game(season=season, game_id=game_id)
        )
        if pool is None:
            raise ProviderUnavailableError(
                "The stored matchup Player Pool is currently unavailable."
            )
        player = next(
            (
                candidate
                for candidate in pool.players
                if candidate.canonical_player_id == player_id
            ),
            None,
        )
        if player is None:
            raise ResourceNotFoundError("The requested matchup player was not found.")
        opponent_team_id = self._opponent_team_id(event, player)
        markets = player.market_categories
        missing_markets = sorted(set(markets) - self._statistics.keys())
        if missing_markets:
            raise ProviderUnavailableError(
                "The stored Player Pool categories are incompatible with the current "
                "Statistic Catalog."
            )
        log_freshness = self.player_logs.get_read_freshness(season)
        rate = self.player_logs.get_season_rate(season, player_id)
        h2h_records = self.player_logs.list_h2h_rows(
            season, player_id, opponent_team_id
        )
        h2h_rated = self._rate_rows(h2h_records, markets, {player_id: rate})

        peer_ids = self.archetypes.list_peer_ids(player_id)
        archetype_records = self.player_logs.list_archetype_rows(
            season, peer_ids, opponent_team_id
        )
        summaries = self.player_logs.get_player_summaries(season, peer_ids)
        peer_rates = {
            peer_id: summary.season_rate for peer_id, summary in summaries.items()
        }
        archetype_rated = self._rate_rows(archetype_records, markets, peer_rates)
        return {
            "player_id": player_id,
            "freshness": {
                "player_pool": dict(pool.freshness),
                "player_game_logs": {
                    "status": log_freshness.status,
                    "retrieved_at": (
                        log_freshness.retrieved_at.isoformat()
                        if log_freshness.retrieved_at is not None
                        else None
                    ),
                },
            },
            "h2h": self._table(h2h_rated, self.h2h_thin_min_games),
            "archetype": self._table(archetype_rated, self.archetype_thin_min_games),
        }

    def _event(self, season: str, game_id: str) -> Mapping[str, Any]:
        if self.event_catalog is None:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        if self.event_catalog.count_events(season) == 0:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        for event in self.event_catalog.get_events(season):
            if str(event.get("nba_game_id")) == game_id:
                return event
        raise ResourceNotFoundError("The requested matchup game was not found.")

    @staticmethod
    def _opponent_team_id(event: Mapping[str, Any], player: PoolPlayer) -> int:
        home_team_id = int(event["home_team_id"])
        away_team_id = int(event["away_team_id"])
        if player.team_id == home_team_id:
            return away_team_id
        if player.team_id == away_team_id:
            return home_team_id
        raise ResourceNotFoundError("The requested matchup player was not found.")

    def _rate_rows(
        self,
        records: Iterable[PlayerGameLogRecord],
        markets: tuple[str, ...],
        rates: Mapping[int, PlayerSeasonRate | None],
    ) -> tuple[_RatedRow, ...]:
        rated: list[_RatedRow] = []
        for record in records:
            rate = rates.get(record.player_id)
            if record.minutes <= 0 or rate is None:
                continue
            values = self._market_values(record, markets)
            if any(market not in rate.per_minute for market in markets):
                continue
            rated.append(
                _RatedRow(
                    record=record,
                    values=values,
                    deltas={
                        market: values[market] / record.minutes
                        - rate.per_minute[market]
                        for market in markets
                    },
                    rates={market: rate.per_minute[market] for market in markets},
                )
            )
        return tuple(rated)

    def _market_values(
        self, record: PlayerGameLogRecord, markets: tuple[str, ...]
    ) -> dict[str, float]:
        return selected_player_game_log_market_values(
            record, markets, self._statistics
        )

    @classmethod
    def _table(cls, rows: tuple[_RatedRow, ...], thin_min_games: int) -> dict[str, Any]:
        if not rows:
            return {"thin": True, "rows": []}
        return {
            "thin": len(rows) < thin_min_games,
            "rows": [
                *(cls._game_row(row) for row in rows),
                cls._average_row(rows),
            ],
        }

    @classmethod
    def _game_row(cls, row: _RatedRow) -> dict[str, Any]:
        record = row.record
        separator = "vs." if record.is_home else "@"
        return {
            "row_type": "game",
            "player_id": record.player_id,
            "player_name": record.player_name,
            "game_date": record.game_date.isoformat(),
            "matchup": (
                f"{record.team_tricode} {separator} {record.opponent_team_tricode}"
            ),
            "minutes": cls._number(record.minutes),
            "stats": cls._numbers(row.values),
            "deltas": cls._numbers(row.deltas),
        }

    @classmethod
    def _average_row(cls, rows: tuple[_RatedRow, ...]) -> dict[str, Any]:
        markets = tuple(rows[0].values)
        count = len(rows)
        total_minutes = sum(row.record.minutes for row in rows)
        return {
            "row_type": "average",
            "player_id": None,
            "player_name": None,
            "game_date": None,
            "matchup": None,
            "minutes": cls._number(total_minutes / count),
            "stats": cls._numbers(
                {
                    market: sum(row.values[market] for row in rows) / count
                    for market in markets
                }
            ),
            "deltas": cls._numbers(
                {
                    market: (
                        sum(row.values[market] for row in rows)
                        - sum(row.rates[market] * row.record.minutes for row in rows)
                    )
                    / total_minutes
                    for market in markets
                }
            ),
        }

    @staticmethod
    def _number(value: float) -> float:
        return round(float(value), _WIRE_PRECISION)

    @classmethod
    def _numbers(cls, values: Mapping[str, float]) -> dict[str, float]:
        return {key: cls._number(value) for key, value in values.items()}


__all__ = [
    "ArchetypeReader",
    "MatchupSelectionService",
    "StoredPlayerPoolReader",
]
