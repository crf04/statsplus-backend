"""PBP Stats game-log provider boundary.

The application depends on :class:`PBPGameLogProvider`.  The concrete adapter
keeps the PBP game-log endpoints, request parameters, shared HTTP session,
timeouts, telemetry, response validation, and provider error translation in
one place.  Callers receive PBP-shaped observation rows through the production
parse seams; canonical game-log normalization lives beside the service
consumers so the same mapping serves both the live request path and durable
ingestion.

The wire contract mirrors the reviewed PBP Stats OpenAPI:

* ``GET /get-game-logs/{league}`` with ``Season``, ``SeasonType``,
  ``EntityType=Player``, and ``EntityId`` returns flat ``multi_row_table_data``
  rows whose identity block is ``EntityId``/``TeamId``/``Name``/``Date``/
  ``GameId``/``Team``/``Opponent`` and whose minutes are an ``MM:SS`` string.
* ``GET /get-game-stats`` with ``GameId`` and ``Type=Player`` returns nested
  ``stats.Home/Away.FullGame`` arrays of flat ``BoxscoreItem`` rows; game
  identity (``GameId``, ``Date``, ``Team``, ``Opponent``) is attached from the
  request and the response envelope so both seams share one observation shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import pandas as pd
import requests

from app.config.settings import RuntimeSettings
from app.services.pbp_stats_adapter import PBPTotalsAdapter as _InstrumentedPBPTotalsAdapter
from app.services.pbp_stats_adapter import PBP_REGULAR_SEASON
from app.utils.telemetry import (
    CACHE_DISABLED,
    PBP_STATS_OPERATIONS,
    PROVIDER_PBP_STATS,
    ProviderResponseError,
    provider_call,
    record_cached_provider_event,
)

PBP_GAME_LOGS_URL = "https://api.pbpstats.com/get-game-logs/nba"
PBP_GAME_STATS_URL = "https://api.pbpstats.com/get-game-stats"

#: The strict identity/minute columns every PBP game-log row must carry.
#: PBP omits observed-zero counting fields, so the remaining box-score columns
#: are sparse by contract and are zero-filled only during canonicalization.
PBP_GAME_LOG_REQUIRED_COLUMNS: tuple[str, ...] = (
    "EntityId",
    "Name",
    "GameId",
    "Date",
    "TeamId",
    "Minutes",
)
#: Matchup-tricode evidence retained beside the canonical Event Catalog join.
PBP_GAME_LOG_EVIDENCE_COLUMNS: tuple[str, ...] = ("Team", "Opponent")
PBP_GAME_LOG_COUNTING_COLUMNS: tuple[str, ...] = (
    "FG2M",
    "FG2A",
    "FG3M",
    "FG3A",
    "FGM",
    "FGA",
    "FtPoints",
    "FTA",
    "OffRebounds",
    "DefRebounds",
    "Assists",
    "Turnovers",
    "Steals",
    "Blocks",
    "Fouls",
    "PlusMinus",
    "Points",
)
PBP_GAME_LOG_COLUMNS: tuple[str, ...] = (
    *PBP_GAME_LOG_REQUIRED_COLUMNS,
    *PBP_GAME_LOG_EVIDENCE_COLUMNS,
    *PBP_GAME_LOG_COUNTING_COLUMNS,
)


class PBPGameLogProvider(Protocol):
    """The application-facing PBP game-log contract.

    Implementations return PBP-shaped observation rows and translate upstream
    failures into provider events and :class:`ProviderResponseError`, the same
    way the totals adapter does.
    """

    def fetch_player_game_logs(
        self,
        player_id: int,
        season: str,
        *,
        season_type: str = "Regular Season",
        cache_status: str = CACHE_DISABLED,
    ) -> pd.DataFrame:
        """Fetch one player's season game-log observations."""

    def fetch_game_player_logs(
        self,
        game_id: str,
        season: str,
        *,
        season_type: str = "Regular Season",
    ) -> pd.DataFrame:
        """Fetch one game's participating player observations."""

    def record_cache_hit(self, operation: str) -> None:
        """Record an event for a response served without a provider call."""


class PBPGameLogAdapter(_InstrumentedPBPTotalsAdapter):
    """Concrete PBP game-log adapter backed by the shared retrying session."""

    PROVIDER_NAME = "PBP Stats"
    game_logs_url = PBP_GAME_LOGS_URL
    game_stats_url = PBP_GAME_STATS_URL

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        session: requests.Session | Any | None = None,
    ) -> None:
        super().__init__(settings=settings, session=session)

    @contextmanager
    def _request_game_logs(
        self,
        operation: str,
        url: str,
        params: dict[str, str],
        *,
        cache_status: str = CACHE_DISABLED,
    ) -> Iterator[requests.Response]:
        """Execute one instrumented PBP game-log request."""
        with provider_call(
            PROVIDER_PBP_STATS,
            operation,
            cache_status=cache_status,
        ) as tracker:
            response = self.session.get(
                url,
                params=params,
                timeout=(self.connect_timeout, self.read_timeout),
            )
            tracker.status_code = response.status_code
            response.raise_for_status()
            yield response

    def fetch_player_game_logs(
        self,
        player_id: int,
        season: str,
        *,
        season_type: str = "Regular Season",
        cache_status: str = CACHE_DISABLED,
    ) -> pd.DataFrame:
        """Fetch and validate one player's season game-log observations."""
        params = {
            "Season": season,
            "SeasonType": _provider_season_type(season_type),
            "EntityType": "Player",
            "EntityId": str(player_id),
        }
        with self._request_game_logs(
            "player_game_logs",
            self.game_logs_url,
            params,
            cache_status=cache_status,
        ) as response:
            return type(self).parse_game_logs(_json_payload(response))

    def fetch_game_player_logs(
        self,
        game_id: str,
        season: str,
        *,
        season_type: str = "Regular Season",
    ) -> pd.DataFrame:
        """Fetch and validate one game's participating player observations."""
        del season, season_type
        params = {
            "GameId": str(game_id),
            "Type": "Player",
        }
        with self._request_game_logs(
            "game_player_stats",
            self.game_stats_url,
            params,
        ) as response:
            return type(self).parse_game_stats(
                _json_payload(response),
                game_id=str(game_id),
            )

    def record_cache_hit(self, operation: str) -> None:
        """Record a cache-hit event for a PBP game-log operation."""
        if operation not in PBP_STATS_OPERATIONS:
            raise ValueError(
                f"Unsupported PBP Stats operation {operation!r}; "
                f"expected one of {sorted(PBP_STATS_OPERATIONS)}."
            )
        record_cached_provider_event(PROVIDER_PBP_STATS, operation)

    @staticmethod
    def parse_game_logs(payload: Any) -> pd.DataFrame:
        """Validate and normalize a recorded ``/get-game-logs`` payload."""
        return _parse_game_log_rows(payload)

    @staticmethod
    def parse_game_stats(payload: Any, *, game_id: str) -> pd.DataFrame:
        """Validate and normalize a recorded ``/get-game-stats`` payload.

        The nested ``stats.Home/Away.FullGame`` player arrays are flattened and
        the game identity from the response envelope (``date``, home/away
        abbreviations) plus the requested ``game_id`` is attached so the rows
        share one observation vocabulary with game logs.
        """
        if not isinstance(payload, dict):
            raise ProviderResponseError(
                "PBP Stats game-stats payload is missing an object."
            )
        stats = payload.get("stats")
        if not isinstance(stats, dict) or not all(
            side in stats for side in ("Home", "Away")
        ):
            raise ProviderResponseError(
                "PBP Stats game-stats payload has an invalid stats shape."
            )
        game_date = payload.get("date")
        if not game_date:
            raise ProviderResponseError(
                "PBP Stats game-stats payload is missing a game date."
            )
        sides = {
            "Home": (
                payload.get("home_team_abbreviation"),
                payload.get("away_team_abbreviation"),
            ),
            "Away": (
                payload.get("away_team_abbreviation"),
                payload.get("home_team_abbreviation"),
            ),
        }
        rows: list[dict[str, Any]] = []
        for side, (team, opponent) in sides.items():
            period = stats[side].get("FullGame") if isinstance(stats[side], dict) else None
            if not isinstance(period, list):
                raise ProviderResponseError(
                    "PBP Stats game-stats payload has an invalid FullGame list."
                )
            if not all(isinstance(row, dict) for row in period):
                raise ProviderResponseError(
                    "PBP Stats game-stats payload contains malformed rows."
                )
            for row in period:
                row = dict(row)
                row["GameId"] = game_id
                row["Date"] = str(game_date)
                row["Team"] = team
                row["Opponent"] = opponent
                rows.append(row)
        return _project_game_log_rows(rows)


def _provider_season_type(season_type: str) -> str:
    return (
        PBP_REGULAR_SEASON
        if season_type in {"Regular Season", PBP_REGULAR_SEASON}
        else season_type
    )


def _json_payload(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise ProviderResponseError(
            "PBP Stats returned a response that was not valid JSON."
        ) from error


def _parse_game_log_rows(payload: Any) -> pd.DataFrame:
    """Validate one ``/get-game-logs`` wire payload into a sparse row frame.

    Every row must carry the strict identity and minutes columns; the counting
    columns are sparse and materialized as missing values so canonicalization
    can decide which observed omissions mean zero.
    """
    rows = payload.get("multi_row_table_data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ProviderResponseError(
            "PBP Stats game-log payload is missing a list of rows."
        )
    if rows and not all(isinstance(row, dict) for row in rows):
        raise ProviderResponseError(
            "PBP Stats game-log payload contains malformed rows."
        )
    if not rows:
        return pd.DataFrame(columns=PBP_GAME_LOG_COLUMNS)
    return _project_game_log_rows(rows)


def _project_game_log_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Project validated game-log rows onto the declared sparse vocabulary."""
    for row in rows:
        missing = [
            column
            for column in PBP_GAME_LOG_REQUIRED_COLUMNS
            if row.get(column) in (None, "")
        ]
        if missing:
            raise ProviderResponseError(
                "PBP Stats game-log payload has an invalid schema."
            )
    return pd.DataFrame(
        [
            {
                column: row.get(column)
                for column in PBP_GAME_LOG_COLUMNS
            }
            for row in rows
        ],
        columns=PBP_GAME_LOG_COLUMNS,
    )


__all__ = [
    "PBP_GAME_LOG_COLUMNS",
    "PBP_GAME_LOG_COUNTING_COLUMNS",
    "PBP_GAME_LOG_EVIDENCE_COLUMNS",
    "PBP_GAME_LOG_REQUIRED_COLUMNS",
    "PBP_GAME_LOGS_URL",
    "PBP_GAME_STATS_URL",
    "PBPGameLogAdapter",
    "PBPGameLogProvider",
]
