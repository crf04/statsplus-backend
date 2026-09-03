"""Stored-log matchup selection tables and per-market rate deltas."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.domain.matchup_experience import (
    experience_mode,
    is_historical_matchup,
    player_source,
)
from app.domain.nba_events import resolve_stored_event_classification
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
    PlayerSeasonRate,
)
from app.services.player_game_log_values import (
    player_game_log_focal_line,
    selected_player_game_log_market_values,
    validate_player_game_log_components,
)
from app.services.player_pool import (
    PlayerPool,
    PoolPlayer,
    SingleGamePlayerPoolReader,
)
from app.services.publication_snapshot_calls import (
    accepts_keyword,
    call_with_read_scope,
)
from app.services.request_reads import request_read_scope
from app.services.statistic_catalog import StatisticCatalog


_WIRE_PRECISION = 6
_PROJECTION_ONLY_STREAM_KEYS = frozenset({"player_game_logs"})


class EventCatalogReader(Protocol):
    def count_events(self, season: str) -> int: ...

    def get_event(self, season: str, nba_game_id: str) -> Mapping[str, Any] | None: ...


class ArchetypeReader(Protocol):
    def list_peer_ids(self, player_id: int) -> tuple[int, ...]: ...


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
        player_pool: SingleGamePlayerPoolReader | None,
        player_logs: PlayerGameLogRepository,
        archetypes: ArchetypeReader,
        statistic_catalog: StatisticCatalog,
        settings: RuntimeSettings,
        publication_reader: Any | None = None,
        h2h_thin_min_games: int | None = None,
        archetype_thin_min_games: int | None = None,
        engine: Engine | None = None,
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
        self.publication_reader = publication_reader
        # One request checks out one connection for every read it composes;
        # without an engine each seam keeps opening its own.
        self._engine = engine
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
        with request_read_scope(self._engine) as (connection, session):
            return self._compose_selection(
                game_id=game_id,
                player_id=player_id,
                connection=connection,
                session=session,
            )

    def _compose_selection(
        self,
        *,
        game_id: str,
        player_id: int,
        connection: Connection | None,
        session: Session | None,
    ) -> dict[str, Any]:
        season = self.settings.nba.current_season
        publication_snapshot = self._publication_snapshot(season, session=session)
        event = self._event(season, game_id, connection=connection)
        pool = (
            None
            if self.player_pool is None
            else call_with_read_scope(
                self.player_pool.get_pool_for_game,
                season=season,
                game_id=game_id,
                connection=connection,
            )
        )
        team_ids = (int(event["away_team_id"]), int(event["home_team_id"]))
        pool_players = (
            ()
            if pool is None
            else tuple(
                candidate
                for candidate in pool.players
                if candidate.team_id in team_ids
            )
        )
        player = next(
            (
                candidate
                for candidate in pool_players
                if candidate.canonical_player_id == player_id
            ),
            None,
        )
        # A Historical Matchup keeps a canonical game-log participant
        # selectable without any Player Pool membership. The eligibility rule
        # is the one the Matchup response declares.
        historical = is_historical_matchup(event, pool_players)
        focal_record = None
        if historical:
            focal_record = self._focal_record(
                season,
                game_id,
                player_id,
                publication_snapshot=publication_snapshot,
                connection=connection,
            )
            opponent_team_id = int(focal_record.opponent_team_id)
            markets = tuple(sorted(self._statistics))
        else:
            if pool is None:
                raise ProviderUnavailableError(
                    "The stored matchup Player Pool is currently unavailable."
                )
            if player is None:
                raise ResourceNotFoundError(
                    "The requested matchup player was not found."
                )
            opponent_team_id = self._opponent_team_id(event, player)
            markets = player.market_categories
        missing_markets = sorted(set(markets) - self._statistics.keys())
        if missing_markets:
            raise ProviderUnavailableError(
                "The stored Player Pool categories are incompatible with the current "
                "Statistic Catalog."
            )
        # Pregame samples use games strictly before the focal game, and the
        # baseline excludes the focal result, so the analysis never grades
        # itself with the outcome it is contextualizing.
        sample_scope = (
            {"before_date": focal_record.game_date} if historical else {}
        )
        baseline_scope = {"exclude_game_id": game_id} if historical else {}
        log_freshness = call_with_read_scope(
            self.player_logs.get_read_freshness,
            season,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        rate = call_with_read_scope(
            self.player_logs.get_season_rate,
            season,
            player_id,
            publication_snapshot=publication_snapshot,
            connection=connection,
            **baseline_scope,
        )
        h2h_records = call_with_read_scope(
            self.player_logs.list_h2h_rows,
            season,
            player_id,
            opponent_team_id,
            publication_snapshot=publication_snapshot,
            connection=connection,
            **sample_scope,
        )
        h2h_rated = self._rate_rows(h2h_records, markets, {player_id: rate})

        peer_ids = call_with_read_scope(
            self.archetypes.list_peer_ids, player_id, connection=connection
        )
        archetype_records = call_with_read_scope(
            self.player_logs.list_archetype_rows,
            season,
            peer_ids,
            opponent_team_id,
            publication_snapshot=publication_snapshot,
            connection=connection,
            **sample_scope,
        )
        summaries = call_with_read_scope(
            self.player_logs.get_player_summaries,
            season,
            peer_ids,
            publication_snapshot=publication_snapshot,
            connection=connection,
            **baseline_scope,
        )
        peer_rates = {
            peer_id: summary.season_rate for peer_id, summary in summaries.items()
        }
        archetype_rated = self._rate_rows(archetype_records, markets, peer_rates)
        return {
            "player_id": player_id,
            "experience": self._experience(historical, focal_record, markets),
            "freshness": {
                "player_pool": (
                    PlayerPool.missing_projection_freshness()
                    if pool is None
                    else dict(pool.freshness)
                ),
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

    def _focal_record(
        self,
        season: str,
        game_id: str,
        player_id: int,
        *,
        publication_snapshot=None,
        connection: Connection | None = None,
    ) -> PlayerGameLogRecord:
        # The Matchup route withholds Participants entirely until this game's
        # canonical synchronization is complete, so selection reads the same
        # evidence rather than resolving a participant from partial rows.
        sync = call_with_read_scope(
            self.player_logs.get_sync_status,
            season,
            game_id,
            connection=connection,
        )
        if sync is None or sync.status != "complete":
            raise ProviderUnavailableError(
                "The stored canonical game logs for this matchup are incomplete."
            )
        rows = call_with_read_scope(
            self.player_logs.list_game_rows,
            season,
            game_id,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        record = next(
            (row for row in rows if int(row.player_id) == player_id), None
        )
        if record is None:
            raise ResourceNotFoundError("The requested matchup player was not found.")
        return record

    def _experience(
        self,
        historical: bool,
        focal_record: PlayerGameLogRecord | None,
        markets: Sequence[str],
    ) -> dict[str, Any]:
        """Declare the selection's mode, focal line, and sample provenance."""

        if not historical or focal_record is None:
            return {
                "mode": experience_mode(False),
                "player_source": player_source(False),
                "focal_game": None,
                "samples": {
                    "context": "season_to_date",
                    "excludes_focal_game": False,
                },
                "baseline": {
                    "context": "season_to_date",
                    "hindsight": False,
                },
            }
        return {
            "mode": experience_mode(True),
            "player_source": player_source(True),
            "focal_game": player_game_log_focal_line(
                focal_record, markets, self._statistics
            ),
            "samples": {"context": "pregame", "excludes_focal_game": True},
            # The stored Regular Season baseline spans the completed season,
            # so it is labeled hindsight rather than pregame evidence.
            "baseline": {"context": "completed_season", "hindsight": True},
        }

    def _publication_snapshot(self, season: str, *, session: Session | None = None):
        if self.publication_reader is None:
            return None
        snapshot = getattr(self.publication_reader, "snapshot", None)
        if not callable(snapshot):
            snapshot = getattr(self.publication_reader, "read_snapshot", None)
        if not callable(snapshot):
            return None
        keyword = {}
        if accepts_keyword(snapshot, "projection_only_keys"):
            # One card resolves its own rows from the projection, so the
            # season-wide game-log payload is never worth shipping.
            keyword["projection_only_keys"] = _PROJECTION_ONLY_STREAM_KEYS
        if session is not None and accepts_keyword(snapshot, "session"):
            keyword["session"] = session
        return snapshot(("player_game_logs",), season=season, **keyword)

    def _event(
        self,
        season: str,
        game_id: str,
        *,
        connection: Connection | None = None,
    ) -> Mapping[str, Any]:
        if self.event_catalog is None:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        event = call_with_read_scope(
            self.event_catalog.get_event, season, game_id, connection=connection
        )
        if event is not None:
            classification = resolve_stored_event_classification(
                game_id,
                str(event.get("classification", event.get("season_type", ""))),
            )
            if classification.kind != "Regular Season":
                raise ResourceNotFoundError(
                    "The requested matchup is outside the Regular Season window."
                )
            return event
        if call_with_read_scope(
            self.event_catalog.count_events, season, connection=connection
        ) == 0:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
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
]
