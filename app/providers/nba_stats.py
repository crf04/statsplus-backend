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
import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import pandas as pd
from nba_api.stats import endpoints

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError
from app.services.nba_stats_adapter import NBAStatsAdapter as _InstrumentedNBAStatsAdapter
from app.utils.telemetry import ProviderResponseError

logger = logging.getLogger(__name__)

DEFAULT_SEASON_TYPE = "Regular Season"
_CANONICAL_SEASON = re.compile(r"^(?P<start>\d{4})-(?P<end>\d{2})$")

ROSTER_COLUMNS = (
    "player_id",
    "display_name",
    "roster_status",
    "is_active",
    "is_active_for_season",
    "season",
    "team_id",
    "team_name",
    "team_abbreviation",
)

_ROSTER_COLUMN_ALIASES = {
    "PERSON_ID": "player_id",
    "PLAYER_ID": "player_id",
    "DISPLAY_FIRST_LAST": "display_name",
    "DISPLAY_LAST_COMMA_FIRST": "display_name",
    "DISPLAY_NAME": "display_name",
    "PLAYER_NAME": "display_name",
    "ROSTERSTATUS": "roster_status_raw",
    "ROSTER_STATUS": "roster_status_raw",
    "FROM_YEAR": "from_year",
    "TO_YEAR": "to_year",
    "TEAM_ID": "team_id",
    "TEAM_NAME": "team_name",
    "TEAM_ABBREVIATION": "team_abbreviation",
    "TEAM_ABBR": "team_abbreviation",
}


def validate_canonical_season(season: str) -> str:
    """Validate and return one explicit NBA season (for example ``2024-25``)."""

    if not isinstance(season, str):
        raise ValueError("season must be a string in YYYY-YY form")
    normalized = season.strip()
    match = _CANONICAL_SEASON.fullmatch(normalized)
    if match is None:
        raise ValueError("season must be a canonical YYYY-YY value")
    start_year = int(match.group("start"))
    if int(match.group("end")) != (start_year + 1) % 100:
        raise ValueError("season must span consecutive calendar years")
    return normalized


def _roster_status_is_active(value: Any) -> bool:
    """Interpret the provider's documented 0/1 roster status values."""

    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "active", "current", "true", "yes"}


def _nullable_int(value: Any) -> int | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ProviderResponseError("NBA Stats returned an invalid roster value.") from error
    if not number.is_integer():
        raise ProviderResponseError("NBA Stats returned an invalid roster value.")
    return int(number)


def _nullable_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _canonicalize_roster_columns(raw_frame: pd.DataFrame) -> pd.DataFrame:
    rename_map: dict[Any, str] = {}
    source_columns = {
        str(column).strip().upper().replace(" ", "_")
        for column in raw_frame.columns
    }
    canonical_columns = set(ROSTER_COLUMNS)
    for column in raw_frame.columns:
        if str(column) in canonical_columns:
            continue
        normalized_name = str(column).strip().upper().replace(" ", "_")
        if normalized_name == "DISPLAY_LAST_COMMA_FIRST" and "DISPLAY_FIRST_LAST" in source_columns:
            continue
        canonical_name = _ROSTER_COLUMN_ALIASES.get(normalized_name)
        if canonical_name and canonical_name not in raw_frame.columns:
            rename_map[column] = canonical_name
    frame = raw_frame.rename(columns=rename_map).copy()
    missing = [column for column in ("player_id", "display_name") if column not in frame.columns]
    if missing:
        raise ProviderResponseError("NBA Stats returned an unsupported roster schema.")
    for column in ("roster_status_raw", "from_year", "to_year", "team_id", "team_name", "team_abbreviation"):
        if column not in frame.columns:
            frame[column] = None
    return frame


def _classify_roster_status(row: dict[str, Any], start_year: int) -> str:
    existing_status = _nullable_text(row.get("roster_status"))
    if existing_status in {"active", "inactive", "historical"}:
        return existing_status
    from_year = _nullable_int(row.get("from_year"))
    to_year = _nullable_int(row.get("to_year"))
    covers_season = (
        (from_year is None or from_year <= start_year)
        and (to_year is None or to_year >= start_year)
    )
    if not covers_season:
        return "historical"
    return "active" if _roster_status_is_active(row.get("roster_status_raw")) else "inactive"


def _roster_record(row: dict[str, Any], season: str, start_year: int) -> dict[str, Any]:
    player_id = _nullable_int(row.get("player_id"))
    display_name = _nullable_text(row.get("display_name"))
    if player_id is None or player_id <= 0 or not display_name:
        raise ProviderResponseError("NBA Stats returned a roster row without player identity.")
    roster_status = _classify_roster_status(row, start_year)
    return {
        "player_id": player_id,
        "display_name": display_name,
        "roster_status": roster_status,
        "is_active": roster_status == "active",
        "is_active_for_season": roster_status == "active",
        "season": season,
        "team_id": _nullable_int(row.get("team_id")),
        "team_name": _nullable_text(row.get("team_name")),
        "team_abbreviation": _nullable_text(row.get("team_abbreviation")),
        "_status_rank": {"active": 0, "inactive": 1, "historical": 2}[roster_status],
    }


def _deduplicate_roster_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    normalized = pd.DataFrame(records)
    if normalized.empty:
        return pd.DataFrame(columns=ROSTER_COLUMNS)
    return (
        normalized.sort_values(["player_id", "_status_rank"])
        .drop_duplicates("player_id", keep="first")
        .drop(columns=["_status_rank"])
        .sort_values("player_id")
        .reset_index(drop=True)
        .loc[:, ROSTER_COLUMNS]
    )


def normalize_player_roster(raw_frame: pd.DataFrame, season: str) -> pd.DataFrame:
    """Normalize one ``CommonAllPlayers`` response for a requested season.

    NBA Stats returns one long-lived row per player, including retired and
    historical players.  The season is supplied by the caller rather than
    inferred from the wall clock; ``roster_status`` therefore captures both
    the provider's active flag and whether the player's NBA tenure covers the
    requested season.
    """

    season = validate_canonical_season(season)
    if not isinstance(raw_frame, pd.DataFrame):
        raise ProviderResponseError("NBA Stats returned an invalid roster response.")

    frame = _canonicalize_roster_columns(raw_frame)
    start_year = int(season[:4])
    normalized = _deduplicate_roster_records(
        [_roster_record(row, season, start_year) for row in frame.to_dict(orient="records")]
    )
    if normalized.empty:
        raise ProviderResponseError(
            "NBA Stats returned an empty roster for the requested season."
        )
    return normalized


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

    def get_player_roster(self, *, season: str) -> pd.DataFrame:
        """Return the season-scoped canonical player roster."""


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
        roster_endpoint_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(settings=settings, roster_endpoint_factory=roster_endpoint_factory)
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
    "normalize_archetype_game_logs",
    "normalize_player_roster",
    "normalize_player_game_logs",
    "ROSTER_COLUMNS",
    "validate_canonical_season",
]
