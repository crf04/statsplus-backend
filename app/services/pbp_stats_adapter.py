"""Narrow adapter for the shared ``api.pbpstats.com`` request session.

``api.pbpstats.com`` calls use one shared ``requests.Session`` (built by
:mod:`app.utils.nba_api_config`) so connection pooling, keep-alive, timeouts,
and retries stay consistent across every PBP caller.  This adapter is the
single instrumentation seam for the PBP provider: every ``get-totals`` call is
wrapped in one structured :class:`ProviderEvent`, retries are observed through
``RetryWithLogging``, and responses that cannot be parsed into the expected
shape raise :class:`ProviderResponseError`, which is recorded as a
``malformed`` provider failure instead of an application error.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pandas as pd

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import InvalidInputError
from app.models.catalogs import PBPDataKind, PBP_DATA_KINDS
from app.utils.nba_api_config import get_shared_nba_session
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_PBP_STATS,
    ProviderResponseError,
    provider_call,
)

PBP_TOTALS_URL = "https://api.pbpstats.com/get-totals/nba"
PBP_REGULAR_SEASON = "Regular+Season"

# These are the columns consumed by the publication and assist-table
# transforms in ``DataService``.  Keep the provider contract at this seam so
# a malformed response cannot replace a valid table with a schema-less frame.
PBP_PLAYER_REQUIRED_COLUMNS: tuple[str, ...] = (
    "Name",
    "TwoPtAssists",
    "ThreePtAssists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
PBP_OPPONENT_REQUIRED_COLUMNS: tuple[str, ...] = (
    "Name",
    "Assists",
    "AssistPoints",
    "TwoPtAssists",
    "ThreePtAssists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
PBP_REQUIRED_COLUMNS: dict[PBPDataKind, tuple[str, ...]] = {
    "player": PBP_PLAYER_REQUIRED_COLUMNS,
    "opponent": PBP_OPPONENT_REQUIRED_COLUMNS,
}


class PBPTotalsAdapter:
    """Fetch and normalize PBP totals through the shared, retrying session."""

    base_url = PBP_TOTALS_URL

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        session: Any = None,
    ):
        self.settings = settings or get_runtime_settings()
        self.session = session or get_shared_nba_session(self.settings)

    @property
    def connect_timeout(self) -> float:
        return self.settings.providers.pbp_connect_timeout_seconds

    @property
    def read_timeout(self) -> float:
        return self.settings.providers.pbp_read_timeout_seconds

    @contextmanager
    def _request(self, operation: str, params: dict[str, str]) -> Iterator[Any]:
        """Execute one instrumented PBP request and yield its response."""

        with provider_call(
            PROVIDER_PBP_STATS,
            operation,
            cache_status=CACHE_DISABLED,
        ) as tracker:
            response = self.session.get(
                self.base_url,
                params=params,
                timeout=(self.connect_timeout, self.read_timeout),
            )
            tracker.status_code = response.status_code
            response.raise_for_status()
            yield response

    def fetch_totals_frame(
        self,
        data_type: PBPDataKind = "player",
        *,
        season: str | None = None,
        season_type: str = PBP_REGULAR_SEASON,
        team_id: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        """Fetch one PBP totals frame (``player`` or ``opponent``).

        Retries happen inside the shared session's urllib3 adapter; each
        retry increments the thread-safe retry counter read by the tracker.
        Timeouts, HTTP errors, invalid JSON, and malformed shapes are each
        recorded as the relevant provider outcome.
        """
        if data_type not in PBP_DATA_KINDS:
            raise InvalidInputError(
                f"Unsupported PBP data type {data_type!r}. "
                f"Expected one of {sorted(PBP_DATA_KINDS)}."
            )
        provider_season_type = (
            PBP_REGULAR_SEASON
            if season_type in {"Regular Season", PBP_REGULAR_SEASON}
            else season_type
        )
        params = {
            "Season": season or self.settings.nba.current_season,
            "SeasonType": provider_season_type,
            "Type": "Player" if data_type == "player" else "Opponent",
        }
        if team_id is not None:
            params["TeamId"] = str(team_id)
        if from_date is not None:
            params["FromDate"] = from_date
        if to_date is not None:
            params["ToDate"] = to_date
        operation = "get_totals_player" if data_type == "player" else "get_totals_opponent"

        with self._request(operation, params) as response:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderResponseError(
                    "PBP Stats returned a response that was not valid JSON."
                ) from error
            return type(self).parse_totals(payload, data_type=data_type)

    def health_probe(self) -> int:
        """Check ``api.pbpstats.com`` using its own timeout and telemetry seam."""
        with self._request(
            "health_probe",
            {
                "Season": self.settings.nba.current_season,
                "SeasonType": PBP_REGULAR_SEASON,
                "Type": "Player",
            },
        ) as response:
            return response.status_code

    @staticmethod
    def parse_totals(
        payload: Any,
        *,
        data_type: PBPDataKind = "player",
    ) -> pd.DataFrame:
        """Validate and normalize a recorded PBP totals payload.

        This is the production normalization seam: recorded fixtures and live
        calls both flow through it, so offline contract tests run the exact
        code a live response uses.
        """
        if data_type not in PBP_DATA_KINDS:
            raise InvalidInputError(
                f"Unsupported PBP data type {data_type!r}. "
                f"Expected one of {sorted(PBP_DATA_KINDS)}."
            )
        rows = payload.get("multi_row_table_data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderResponseError(
                "PBP Stats totals payload is missing a list of rows."
            )
        if rows and not all(isinstance(row, dict) for row in rows):
            raise ProviderResponseError(
                "PBP Stats totals payload contains malformed rows."
            )
        required_columns = PBP_REQUIRED_COLUMNS[data_type]
        if not rows:
            # An empty provider result carries no column names.  Materialize
            # the declared schema explicitly so a valid table is never
            # replaced by a schema-less DataFrame.
            return pd.DataFrame(columns=required_columns)
        missing = [column for column in required_columns if column not in rows[0]]
        if missing:
            raise ProviderResponseError(
                "PBP Stats totals payload has an invalid schema."
            )
        if any(set(row) != set(rows[0]) for row in rows[1:]):
            raise ProviderResponseError(
                "PBP Stats totals payload has inconsistent row schemas."
            )
        return pd.DataFrame(rows)


__all__ = [
    "PBP_TOTALS_URL",
    "PBP_REGULAR_SEASON",
    "PBP_PLAYER_REQUIRED_COLUMNS",
    "PBP_OPPONENT_REQUIRED_COLUMNS",
    "PBP_REQUIRED_COLUMNS",
    "PBPTotalsAdapter",
]
