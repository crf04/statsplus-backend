"""Database-only request-time player game logs behind one injectable seam."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

import pandas as pd

from app.services.game_log_frame import GAME_LOG_FRAME_COLUMNS, derive_game_log_frame
from app.services.publication_snapshot_calls import call_with_read_scope


class PlayerGameLogReader(Protocol):
    """Durable player-game-log read seam used by the stored source."""

    def list_player_rows(
        self,
        season: str,
        player_id: int,
        *,
        publication_snapshot: Any | None = None,
    ) -> tuple[Any, ...]: ...

    def list_players_rows(
        self,
        season: str,
        player_ids: Iterable[int],
        *,
        publication_snapshot: Any | None = None,
    ) -> dict[int, tuple[Any, ...] | None]: ...

    def has_complete_publication(self, season: str) -> bool: ...

    def get_read_freshness(self, season: str) -> Any: ...

    def read_publication_snapshot(self, season: str) -> Any | None: ...


class GameLogsSource(Protocol):
    """Return the canonical request-time frame for one player and season."""

    def get_player_logs(
        self,
        player_id: int,
        season: str,
        *,
        publication_snapshot: Any | None = None,
    ) -> pd.DataFrame: ...


class StoredGameLogsSource:
    """Build a frame from one complete durable publication, else return empty.

    ``tracking`` (optional) joins NBA.com player-tracking values onto each row
    by (game, player); a game without stored tracking serves ``null``.
    """

    def __init__(self, repository: PlayerGameLogReader, tracking: Any | None = None) -> None:
        self.repository = repository
        self.tracking = tracking

    def get_player_logs(
        self,
        player_id: int,
        season: str,
        *,
        publication_snapshot: Any | None = None,
        connection: Any | None = None,
    ) -> pd.DataFrame:
        complete, publication_snapshot = self._complete(season, publication_snapshot)
        records = (
            call_with_read_scope(
                self.repository.list_player_rows,
                season,
                player_id,
                publication_snapshot=publication_snapshot,
                connection=connection,
            )
            if complete
            else ()
        )
        tracking = self._tracking_for(season, (player_id,), connection) if records else {}
        return _player_frame(records, tracking.get(player_id))

    def get_players_logs(
        self,
        player_ids: Iterable[int],
        season: str,
        *,
        publication_snapshot: Any | None = None,
    ) -> dict[int, pd.DataFrame]:
        """Several players' frames for a season from one repository read.

        Each frame is exactly what ``get_player_logs`` returns for that player,
        including the empty frame for a player with no rows or unreadable ones.
        """

        ids = tuple(sorted(set(player_ids)))
        complete, publication_snapshot = self._complete(season, publication_snapshot)
        found = (
            self.repository.list_players_rows(season, ids, publication_snapshot=publication_snapshot)
            if complete and ids
            else {}
        )
        stored = self._tracking_for(
            season, tuple(player_id for player_id in ids if found.get(player_id)), None
        )
        return {
            player_id: _player_frame(found.get(player_id) or (), stored.get(player_id)) for player_id in ids
        }

    def _tracking_for(
        self, season: str, player_ids: tuple[int, ...], connection: Any | None
    ) -> dict[int, dict[str, dict[str, int | None]]]:
        """Stored tracking by player then game; every requested player has a map."""

        found = (
            self.tracking.for_players(season, player_ids, connection=connection)
            if self.tracking is not None
            else {}
        )
        return {player_id: found.get(player_id, {}) for player_id in player_ids}

    def _complete(self, season: str, publication_snapshot: Any | None) -> tuple[bool, Any | None]:
        if publication_snapshot is None:
            read_snapshot = getattr(self.repository, "read_publication_snapshot", None)
            publication_snapshot = read_snapshot(season) if callable(read_snapshot) else None
        if publication_snapshot is not None:
            publication = publication_snapshot.read("player_game_logs")
            return (not publication.legacy_fallback_allowed and publication.available), publication_snapshot
        return self.repository.has_complete_publication(season), publication_snapshot


def _player_frame(
    records: tuple[Any, ...], tracking: dict[str, dict[str, int | None]] | None = None
) -> pd.DataFrame:
    # The request-time contract serves Regular Season games only.
    records = tuple(record for record in records if record.season_type == "Regular Season")
    return _recency_frame(
        derive_game_log_frame(
            _with_tracking(_records_to_primitive_frame(records), tracking or {}),
            round_minutes=True,
        )
    )


#: Frame column to stored tracking column.  Whole counts or null, never 0 for
#: a value that was not collected, so the columns use pandas' nullable integer.
TRACKING_FRAME_COLUMNS = {
    "REB_CHANCES": "reb_chances",
    "OREB_CHANCES": "oreb_chances",
    "DREB_CHANCES": "dreb_chances",
    "POTENTIAL_AST": "potential_ast",
}


def _with_tracking(frame: pd.DataFrame, tracking: dict[str, dict[str, int | None]]) -> pd.DataFrame:
    game_ids = frame["GAME_ID"] if "GAME_ID" in frame else pd.Series([], dtype=object)
    for column, stored in TRACKING_FRAME_COLUMNS.items():
        frame[column] = pd.Series(
            [(tracking.get(game_id) or {}).get(stored) for game_id in game_ids],
            index=frame.index,
            dtype="Int64",
        )
    return frame


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
        "USG_PCT": record.usage_pct,
    }


_RECORD_PRIMITIVE_COLUMNS = GAME_LOG_FRAME_COLUMNS


__all__ = [
    "GameLogsSource",
    "PlayerGameLogReader",
    "StoredGameLogsSource",
]
