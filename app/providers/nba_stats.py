"""The boundary between game-log business logic and ``stats.nba.com``.

``nba_api`` is a synchronous client for ``stats.nba.com``.  The rest of the
application should not need to know which endpoint class is used, which
timeout is configured, or how the provider's response columns are shaped.
This module owns those details and exposes one small, injectable interface.

The adapter deliberately returns a pandas ``DataFrame`` because the existing
game-log service applies filters with pandas.  The returned frame uses the
canonical columns documented by :func:`normalize_player_game_logs`; provider
extras are discarded and the derived columns used by the service are added at
this boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import pandas as pd
from nba_api.stats import endpoints

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError
from app.services.nba_stats_adapter import (
    CANONICAL_SCHEDULE_COLUMNS,
    SCHEDULE_REQUIRED_COLUMNS,
    NBAStatsAdapter as _InstrumentedNBAStatsAdapter,
    normalize_whole_season_schedule,
    parse_recorded_schedule,
    validate_canonical_season,
)

logger = logging.getLogger(__name__)

DEFAULT_SEASON_TYPE = "Regular Season"


class NBAStatsProvider(Protocol):
    """Interface consumed by NBA-backed application services.

    Implementations return normalized game logs and translate upstream
    failures into :class:`~app.errors.ProviderUnavailableError`.  A fake can
    implement these methods for offline service tests without importing or
    patching ``nba_api``.
    """

    def get_player_game_logs(
        self,
        *,
        player_id: int,
        season: str,
        season_type: str = DEFAULT_SEASON_TYPE,
    ) -> pd.DataFrame:
        """Return canonical game logs for one player and season."""

    def get_archetype_game_logs(
        self,
        *,
        player_ids: Sequence[int],
        opponent_team_id: int,
        season: str,
        season_type: str = DEFAULT_SEASON_TYPE,
    ) -> pd.DataFrame:
        """Return normalized logs for cluster members against one opponent."""

    def fetch_whole_season_schedule(self, *, season: str) -> pd.DataFrame:
        """Return canonical schedule facts for one explicit NBA season."""


# These fields are required by GameService's filters, summaries, and output.
# Keep this list at the adapter boundary so a provider schema change fails
# with a useful provider error rather than deep inside a business filter.
REQUIRED_GAME_LOG_COLUMNS = (
    "PLAYER_NAME",
    "GAME_ID",
    "GAME_DATE",
    "MATCHUP",
    "TEAM_ID",
    "TEAM_ABBREVIATION",
    "MIN",
    "PTS",
    "REB",
    "AST",
    "FGM",
    "FGA",
    "FG_PCT",
    "FG3M",
    "FG3A",
    "FTM",
    "FTA",
    "FT_PCT",
    "OREB",
    "DREB",
    "TOV",
    "STL",
    "BLK",
    "PF",
    "PLUS_MINUS",
    "NBA_FANTASY_PTS",
)

# These are useful provider fields that are part of the existing API response
# but are not needed to calculate the service's derived metrics.  Keeping
# known optional fields preserves the response shape without leaking arbitrary
# columns introduced by a future provider response.
OPTIONAL_GAME_LOG_COLUMNS = (
    "WL",
    "FG3_PCT",
    "VIDEO_AVAILABLE",
    "MIN_SEC",
)

DERIVED_GAME_LOG_COLUMNS = (
    "PRA",
    "PA",
    "PR",
    "RA",
    "STKS",
    "FD_PTS",
    "+/-",
    "FG2M",
    "FG2A",
)

_DROP_PROVIDER_COLUMNS = frozenset(
    {
        "SEASON_YEAR",
        "PLAYER_ID",
        "GP_RANK",
        "W_RANK",
        "L_RANK",
        "W_PCT_RANK",
        "MIN_RANK",
        "FGM_RANK",
        "FGA_RANK",
        "FG_PCT_RANK",
        "FG3M_RANK",
        "FG3A_RANK",
        "FG3_PCT_RANK",
        "FTM_RANK",
        "FTA_RANK",
        "FT_PCT_RANK",
        "OREB_RANK",
        "DREB_RANK",
        "REB_RANK",
        "AST_RANK",
        "TOV_RANK",
        "STL_RANK",
        "BLK_RANK",
        "BLKA_RANK",
        "PF_RANK",
        "PFD_RANK",
        "PTS_RANK",
        "PLUS_MINUS_RANK",
        "NBA_FANTASY_PTS_RANK",
        "DD2_RANK",
        "TD3_RANK",
        "WNBA_FANTASY_PTS_RANK",
        "AVAILABLE_FLAG",
        "NICKNAME",
        "TEAM_NAME",
        "DD2",
        "TD3",
        "WNBA_FANTASY_PTS",
        "BLKA",
        "PFD",
    }
)

_COLUMN_ALIASES = {
    "PLAYER_NAME": "PLAYER_NAME",
    "PLAYER": "PLAYER_NAME",
    "GAME_ID": "GAME_ID",
    "GAMEID": "GAME_ID",
    "GAME_DATE": "GAME_DATE",
    "GAME_DATE_EST": "GAME_DATE",
    "GAME_DATE_ESTIMATE": "GAME_DATE",
    "MATCHUP": "MATCHUP",
    "MATCH_UP": "MATCHUP",
    "TEAM_ID": "TEAM_ID",
    "TEAM_ABBREVIATION": "TEAM_ABBREVIATION",
    "TEAM_ABBR": "TEAM_ABBREVIATION",
}

_NUMERIC_GAME_LOG_COLUMNS = (
    "MIN",
    "PTS",
    "REB",
    "AST",
    "FGM",
    "FGA",
    "FG_PCT",
    "FG3M",
    "FG3A",
    "FTM",
    "FTA",
    "FT_PCT",
    "OREB",
    "DREB",
    "TOV",
    "STL",
    "BLK",
    "PF",
    "PLUS_MINUS",
    "NBA_FANTASY_PTS",
)


def _canonicalize_column_names(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename supported provider aliases without mutating the source frame."""

    rename_map: dict[str, str] = {}
    for column in frame.columns:
        normalized_name = str(column).strip().upper().replace(" ", "_")
        canonical_name = _COLUMN_ALIASES.get(normalized_name)
        if canonical_name and canonical_name not in frame.columns:
            rename_map[column] = canonical_name
    return frame.rename(columns=rename_map)


def _provider_response_error(message: str, *, detail: Any = None) -> ProviderUnavailableError:
    """Create the one public error used for unavailable/malformed responses."""

    return ProviderUnavailableError(message, detail=detail)


def _normalize_game_logs(
    raw_frame: pd.DataFrame,
    *,
    preserve_player_id: bool,
) -> pd.DataFrame:
    """Normalize one ``nba_api`` game-log response to an app-facing schema.

    The provider occasionally adds columns and has historically varied the
    names of a few date/matchup fields.  Extra columns are ignored, supported
    aliases are canonicalized, and required-column drift is reported as a
    provider-unavailable error before any business filters run.
    """

    if not isinstance(raw_frame, pd.DataFrame):
        raise _provider_response_error(
            "The NBA Stats provider returned an invalid game-log response.",
            detail=f"expected DataFrame, got {type(raw_frame).__name__}",
        )

    frame = _canonicalize_column_names(raw_frame.copy())
    required_columns = (
        ("PLAYER_ID",) if preserve_player_id else ()
    ) + REQUIRED_GAME_LOG_COLUMNS
    missing_columns = [
        column for column in required_columns if column not in frame.columns
    ]
    if missing_columns:
        raise _provider_response_error(
            "The NBA Stats provider returned an unsupported game-log schema.",
            detail=f"missing columns: {', '.join(missing_columns)}",
        )

    # Remove endpoint rank/metadata columns and any unknown additions.  A
    # stable projection is important: filters should not accidentally start
    # depending on a provider-only column after a schema update.
    allowed_columns = set(required_columns) | set(OPTIONAL_GAME_LOG_COLUMNS)
    drop_columns = _DROP_PROVIDER_COLUMNS - ({"PLAYER_ID"} if preserve_player_id else set())
    frame = frame.drop(columns=list(drop_columns), errors="ignore")
    frame = frame.loc[:, [column for column in frame.columns if column in allowed_columns]]

    numeric_columns = (
        ("PLAYER_ID",) if preserve_player_id else ()
    ) + _NUMERIC_GAME_LOG_COLUMNS
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame[column].isna().any():
            raise _provider_response_error(
                "The NBA Stats provider returned invalid game-log values.",
                detail=f"non-numeric values in {column}",
            )

    # GameService historically exposed whole-minute values.  Keep that
    # contract at the provider boundary and avoid a pandas dtype surprise for
    # callers using an empty response.
    frame["MIN"] = frame["MIN"].round().astype(int)
    if preserve_player_id:
        frame["PLAYER_ID"] = frame["PLAYER_ID"].round().astype(int)
    frame["GAME_DATE"] = frame["GAME_DATE"].astype(str)

    frame["PRA"] = frame["PTS"] + frame["REB"] + frame["AST"]
    frame["PA"] = frame["PTS"] + frame["AST"]
    frame["PR"] = frame["PTS"] + frame["REB"]
    frame["RA"] = frame["REB"] + frame["AST"]
    frame["STKS"] = frame["STL"] + frame["BLK"]
    frame["FD_PTS"] = frame["NBA_FANTASY_PTS"]
    frame["+/-"] = frame["PLUS_MINUS"]
    frame["FG2M"] = frame["FGM"] - frame["FG3M"]
    frame["FG2A"] = frame["FGA"] - frame["FG3A"]

    ordered_columns = [
        *(required_columns if preserve_player_id else REQUIRED_GAME_LOG_COLUMNS),
        *[column for column in OPTIONAL_GAME_LOG_COLUMNS if column in frame.columns],
        *DERIVED_GAME_LOG_COLUMNS,
    ]
    return frame.loc[:, ordered_columns]


def normalize_player_game_logs(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize one player game-log response for :class:`GameService`."""

    return _normalize_game_logs(raw_frame, preserve_player_id=False)


def normalize_archetype_game_logs(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize game logs while retaining ``PLAYER_ID`` for cluster filters."""

    return _normalize_game_logs(raw_frame, preserve_player_id=True)


class NBAStatsAdapter(_InstrumentedNBAStatsAdapter):
    """Concrete :class:`NBAStatsProvider` backed by ``nba_api``.

    ``nba_api`` calls ``stats.nba.com`` directly.  This adapter owns the
    provider-specific endpoint constructor, the configured
    ``NBA_STATS_TIMEOUT_SECONDS`` timeout, response normalization, and
    translation of provider failures into ``ProviderUnavailableError``.

    ``endpoint_factory`` is an intentionally small constructor seam for
    offline adapter tests.  Production callers should leave it unset.
    """

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        endpoint_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(settings=settings, endpoint_factory=endpoint_factory)
        self._endpoint_factory = (
            endpoint_factory or endpoints.playergamelogs.PlayerGameLogs
        )

    def get_player_game_logs(
        self,
        *,
        player_id: int,
        season: str,
        season_type: str = DEFAULT_SEASON_TYPE,
    ) -> pd.DataFrame:
        """Fetch and normalize one player's regular-season game logs."""

        return self._get_normalized_game_logs(
            normalize=normalize_player_game_logs,
            operation="player_game_logs",
            player_id_nullable=player_id,
            season_nullable=season,
            season_type_nullable=season_type,
        )

    def get_archetype_game_logs(
        self,
        *,
        player_ids: Sequence[int],
        opponent_team_id: int,
        season: str,
        season_type: str = DEFAULT_SEASON_TYPE,
    ) -> pd.DataFrame:
        """Fetch normalized logs for archetype members against one opponent."""

        frame = self._get_normalized_game_logs(
            normalize=normalize_archetype_game_logs,
            operation="player_gamelogs_against",
            season_nullable=season,
            season_type_nullable=season_type,
            opp_team_id_nullable=opponent_team_id,
        )
        return frame[frame["PLAYER_ID"].isin(player_ids)].reset_index(drop=True)

    def _get_normalized_game_logs(
        self,
        *,
        operation: str,
        normalize: Callable[[pd.DataFrame], pd.DataFrame],
        **endpoint_kwargs: Any,
    ) -> pd.DataFrame:
        """Run the game-log endpoint through the shared instrumented seam."""

        def build(timeout: float) -> object:
            return self._endpoint_factory(timeout=timeout, **endpoint_kwargs)

        # Validate the normalized contract inside the provider telemetry
        # context.  ``run_endpoint`` returns the raw frame after validation so
        # callers still receive the canonical provider-facing columns.
        frame = self.run_endpoint(
            operation,
            build,
            required_columns=REQUIRED_GAME_LOG_COLUMNS,
            validator=normalize,
        )
        return normalize(frame)


__all__ = [
    "DEFAULT_SEASON_TYPE",
    "DERIVED_GAME_LOG_COLUMNS",
    "NBAStatsAdapter",
    "NBAStatsProvider",
    "OPTIONAL_GAME_LOG_COLUMNS",
    "REQUIRED_GAME_LOG_COLUMNS",
    "SCHEDULE_REQUIRED_COLUMNS",
    "CANONICAL_SCHEDULE_COLUMNS",
    "normalize_archetype_game_logs",
    "normalize_player_game_logs",
    "normalize_whole_season_schedule",
    "parse_recorded_schedule",
    "validate_canonical_season",
]
