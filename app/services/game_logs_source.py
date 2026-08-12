"""Request-time game-log sources behind one injectable seam.

``GameService`` reads one :class:`GameLogsSource` for the player/season frame it
filters and serializes.  Stage 1 introduces the live PBP source that replaces
the request-time NBA Stats call; Stage 3 adds the stored source and a
database-first router that serves a season from Postgres only when its complete
publication is present and valid, preserving the cached live PBP path for
seasons that have not yet been durably covered.
"""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd

from app.errors import ProviderUnavailableError
from app.services.game_log_frame import GAME_LOG_FRAME_COLUMNS, derive_game_log_frame
from app.services.pbp_game_log_normalization import (
    PBPJoinCounts,
    normalize_pbp_game_logs,
)
from app.utils.telemetry import CACHE_MISS


class EventCatalogReader(Protocol):
    """Owner read seam for governed season games."""

    def get_events(self, season: str) -> list[dict[str, Any]]: ...


class PlayerGameLogReader(Protocol):
    """Durable player-game-log read seam used by the stored source."""

    def list_player_rows(self, season: str, player_id: int) -> tuple[Any, ...]: ...

    def has_complete_publication(self, season: str) -> bool: ...

    def get_read_freshness(self, season: str) -> Any: ...


class GameLogsSource(Protocol):
    """Return the canonical request-time frame for one player and season."""

    def get_player_logs(
        self,
        player_id: int,
        season: str,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame: ...

    def cached(self, season: str) -> bool:
        """Whether Redis caching applies to this season's source result."""

    def record_cache_hit(self, operation: str) -> None: ...


class LivePBPGameLogsSource:
    """Request-time PBP source with the governed Event Catalog join."""

    def __init__(
        self,
        pbp_provider: Any,
        event_catalog: EventCatalogReader | None,
    ) -> None:
        self.pbp_provider = pbp_provider
        self.event_catalog = event_catalog

    def get_player_logs(
        self,
        player_id: int,
        season: str,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame:
        if self.event_catalog is None:
            raise ProviderUnavailableError(
                "The Event Catalog is required to resolve game-log identity."
            )
        events = self.event_catalog.get_events(season)
        if not events:
            raise ProviderUnavailableError(
                "The Event Catalog is unavailable for the requested season."
            )
        observations = self.pbp_provider.fetch_player_game_logs(
            player_id,
            season,
            season_type="Regular Season",
            cache_status=cache_status,
        )
        frame, counts = normalize_pbp_game_logs(
            observations,
            events,
            season_type="Regular Season",
            round_minutes=True,
        )
        if counts.source_row_count > 0 and frame.empty:
            raise ProviderUnavailableError(
                "PBP Stats returned no canonically joinable game logs."
            )
        return _recency_frame(frame)

    def cached(self, season: str) -> bool:
        del season
        return True

    def record_cache_hit(self, operation: str) -> None:
        self.pbp_provider.record_cache_hit(operation)


class StoredGameLogsSource:
    """Build the canonical request-time frame from durable player-game facts."""

    def __init__(self, repository: PlayerGameLogReader) -> None:
        self.repository = repository

    def get_player_logs(
        self,
        player_id: int,
        season: str,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame:
        del cache_status
        records = self.repository.list_player_rows(season, player_id)
        # The legacy request-time contract serves Regular Season games only;
        # the live PBP path requests that phase explicitly, so the stored path
        # must project the same phase for parity.
        records = tuple(
            record
            for record in records
            if record.season_type == "Regular Season"
        )
        return _recency_frame(
            derive_game_log_frame(
                _records_to_primitive_frame(records),
                round_minutes=True,
            )
        )

    def cached(self, season: str) -> bool:
        del season
        return False

    def record_cache_hit(self, operation: str) -> None:
        del operation


class DatabaseFirstGameLogsSource:
    """Serve a durably complete season from Postgres, else the live PBP path."""

    def __init__(
        self,
        live_source: GameLogsSource,
        stored_source: GameLogsSource,
        repository: PlayerGameLogReader,
    ) -> None:
        self.live_source = live_source
        self.stored_source = stored_source
        self.repository = repository

    def get_player_logs(
        self,
        player_id: int,
        season: str,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame:
        source = self._source_for(season)
        return source.get_player_logs(
            player_id,
            season,
            cache_status=cache_status,
        )

    def cached(self, season: str) -> bool:
        return not self.repository.has_complete_publication(season)

    def record_cache_hit(self, operation: str) -> None:
        self.live_source.record_cache_hit(operation)

    def _source_for(self, season: str) -> GameLogsSource:
        if self.repository.has_complete_publication(season):
            return self.stored_source
        return self.live_source


def _recency_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Order a canonical frame deterministically, most recent game first.

    The legacy NBA provider returned games newest first and the last-N
    ``game_filter`` keeps the leading rows, so the live PBP and stored paths
    must agree on the same newest-first order for identical response documents.
    """
    if frame.empty:
        return frame
    return frame.sort_values(
        ["GAME_DATE", "GAME_ID"],
        ascending=[False, False],
        kind="stable",
    ).reset_index(drop=True)


def _records_to_primitive_frame(records: tuple[Any, ...]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=GAME_LOG_FRAME_COLUMNS)
    rows = [_record_to_primitive(record) for record in records]
    return pd.DataFrame(rows)


def _record_to_primitive(record: Any) -> dict[str, Any]:
    matchup = (
        f"{record.team_tricode} vs. {record.opponent_team_tricode}"
        if record.is_home
        else f"{record.team_tricode} @ {record.opponent_team_tricode}"
    )
    return {
        "PLAYER_ID": record.player_id,
        "PLAYER_NAME": record.player_name,
        "GAME_ID": record.game_id,
        "GAME_DATE": record.game_date.isoformat(),
        "MATCHUP": matchup,
        "TEAM_ID": record.team_id,
        "TEAM_ABBREVIATION": record.team_tricode,
        "MIN": record.minutes,
        "PTS": record.points,
        "REB": record.rebounds,
        "AST": record.assists,
        "FGM": record.field_goals_made,
        "FGA": record.field_goals_attempted,
        "FG3M": record.three_pointers_made,
        "FG3A": record.three_pointers_attempted,
        "FTM": record.free_throws_made,
        "FTA": record.free_throws_attempted,
        "OREB": record.offensive_rebounds,
        "DREB": record.defensive_rebounds,
        "TOV": record.turnovers,
        "STL": record.steals,
        "BLK": record.blocks,
        "PF": record.personal_fouls,
        "PLUS_MINUS": record.plus_minus,
    }


_RECORD_PRIMITIVE_COLUMNS = GAME_LOG_FRAME_COLUMNS


__all__ = [
    "DatabaseFirstGameLogsSource",
    "EventCatalogReader",
    "GameLogsSource",
    "LivePBPGameLogsSource",
    "PlayerGameLogReader",
    "StoredGameLogsSource",
    "PBPJoinCounts",
]
