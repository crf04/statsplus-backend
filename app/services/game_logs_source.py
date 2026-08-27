"""Database-only request-time player game logs behind one injectable seam."""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd

from app.services.game_log_frame import GAME_LOG_FRAME_COLUMNS, derive_game_log_frame


class PlayerGameLogReader(Protocol):
    """Durable player-game-log read seam used by the stored source."""

    def list_player_rows(
        self,
        season: str,
        player_id: int,
        *,
        publication_snapshot: Any | None = None,
    ) -> tuple[Any, ...]: ...

    def has_complete_publication(self, season: str) -> bool: ...

    def get_read_freshness(self, season: str) -> Any: ...

    def read_publication_snapshot(self, season: str) -> Any | None: ...


class GameLogsSource(Protocol):
    """Return the canonical request-time frame for one player and season."""

    def get_player_logs(
        self,
        player_id: int,
        season: str,
    ) -> pd.DataFrame: ...


class StoredGameLogsSource:
    """Build a frame from one complete durable publication, else return empty."""

    def __init__(self, repository: PlayerGameLogReader) -> None:
        self.repository = repository

    def get_player_logs(
        self,
        player_id: int,
        season: str,
    ) -> pd.DataFrame:
        read_snapshot = getattr(self.repository, "read_publication_snapshot", None)
        publication_snapshot = (
            read_snapshot(season) if callable(read_snapshot) else None
        )
        if publication_snapshot is not None:
            publication = publication_snapshot.read("player_game_logs")
            complete = not publication.legacy_fallback_allowed and publication.available
        else:
            complete = self.repository.has_complete_publication(season)
        records = (
            self.repository.list_player_rows(
                season,
                player_id,
                publication_snapshot=publication_snapshot,
            )
            if complete
            else ()
        )
        # The request-time contract serves Regular Season games only.
        records = tuple(
            record for record in records if record.season_type == "Regular Season"
        )
        return _recency_frame(
            derive_game_log_frame(
                _records_to_primitive_frame(records),
                round_minutes=True,
            )
        )


def _recency_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Order a canonical frame deterministically, most recent game first.

    The legacy endpoint returned games newest first and the last-N
    ``game_filter`` keeps the leading rows, so durable reads retain that order.
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
    }


_RECORD_PRIMITIVE_COLUMNS = GAME_LOG_FRAME_COLUMNS


__all__ = [
    "GameLogsSource",
    "PlayerGameLogReader",
    "StoredGameLogsSource",
]
