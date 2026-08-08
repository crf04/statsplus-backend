"""Adapter for the PBP Stats totals API.

The application only depends on :class:`PBPStatsProvider`.  The concrete
adapter keeps the provider URL, request parameters, shared HTTP session,
timeouts, retries, response validation, and provider error translation in one
place.  Callers receive a normalized ``pandas.DataFrame`` and never need to
know about the wire-format keys returned by PBP Stats.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import pandas as pd
import requests

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import ProviderUnavailableError
from app.utils.nba_api_config import get_shared_nba_session

logger = logging.getLogger(__name__)


@runtime_checkable
class PBPStatsProvider(Protocol):
    """The application-facing PBP Stats contract.

    Implementations return normalized totals for either players or opponents.
    ``health_check`` uses the same totals endpoint as refreshes, which prevents
    a health probe from claiming the provider is healthy while the refresh
    request shape is broken.
    """

    def get_totals(
        self,
        data_type: str = "player",
        *,
        season: str | None = None,
        season_type: str = "Regular Season",
    ) -> pd.DataFrame:
        """Fetch and normalize player or opponent totals."""

    def health_check(self) -> dict[str, Any]:
        """Check the provider using the totals endpoint."""


class PBPStatsAdapter:
    """Concrete PBP Stats adapter backed by the shared requests session."""

    BASE_URL = "https://api.pbpstats.com/get-totals/nba"
    PROVIDER_NAME = "PBP Stats"

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        session: requests.Session | Any | None = None,
    ) -> None:
        """Create an adapter with injectable settings and HTTP session.

        Production callers use the shared session from
        :func:`get_shared_nba_session`; tests can pass a fake session without
        making network calls.  The PBP connect/read timeouts remain separate
        from the NBA Stats timeout used by ``nba_api``.
        """

        self.settings = settings or get_runtime_settings()
        self.session = session or get_shared_nba_session(self.settings)

    @property
    def timeout(self) -> tuple[float, float]:
        """Return the PBP-specific connect and read timeout pair."""

        return (
            self.settings.providers.pbp_connect_timeout_seconds,
            self.settings.providers.pbp_read_timeout_seconds,
        )

    def get_totals(
        self,
        data_type: str = "player",
        *,
        season: str | None = None,
        season_type: str = "Regular Season",
    ) -> pd.DataFrame:
        """Fetch PBP totals and return a normalized dataframe.

        ``data_type`` is intentionally limited to ``player`` and ``opponent``
        (case-insensitive).  Invalid provider data is treated as provider
        unavailability rather than leaking ``KeyError``/``TypeError`` details
        through route handlers.
        """

        canonical_type = self._canonical_data_type(data_type)
        payload, _response = self._request_payload(
            canonical_type,
            season=season,
            season_type=season_type,
        )
        return self._normalize_payload(payload)

    def health_check(self) -> dict[str, Any]:
        """Check PBP Stats using the same request and response validation.

        A successful health check validates the actual totals payload instead
        of only checking that an unrelated host returned an HTTP response.
        Provider failures are raised as ``ProviderUnavailableError`` so the
        Flask error boundary can consistently return HTTP 503.
        """

        started = time.monotonic()
        payload, response = self._request_payload("player")
        self._normalize_payload(payload)
        duration_ms = round((time.monotonic() - started) * 1000, 2)

        return {
            "status": "healthy",
            "provider": self.PROVIDER_NAME,
            "endpoint": self.BASE_URL,
            "response_time_ms": duration_ms,
            "status_code": getattr(response, "status_code", None),
            "test_type": "totals",
            "using_session_pool": True,
        }

    def _request_payload(
        self,
        data_type: str,
        *,
        season: str | None = None,
        season_type: str = "Regular Season",
    ) -> tuple[Any, Any]:
        """Request one totals payload and translate all provider failures."""

        settings = self.settings
        params = {
            "Season": season or settings.nba.current_season,
            "SeasonType": season_type,
            "Type": "Player" if data_type == "player" else "Opponent",
        }
        started = time.monotonic()

        try:
            response = self.session.get(
                self.BASE_URL,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.Timeout as error:
            duration = time.monotonic() - started
            logger.warning(
                "PBP Stats request timed out after %.2fs for type=%s",
                duration,
                data_type,
            )
            raise ProviderUnavailableError(
                "PBP Stats timed out while fetching totals.", detail=error
            ) from error
        except requests.exceptions.RequestException as error:
            duration = time.monotonic() - started
            logger.warning(
                "PBP Stats request failed after %.2fs for type=%s: %s",
                duration,
                data_type,
                error,
            )
            raise ProviderUnavailableError(
                "PBP Stats could not be reached.", detail=error
            ) from error
        except (TypeError, ValueError, KeyError) as error:
            logger.warning("PBP Stats returned malformed JSON for type=%s", data_type)
            raise ProviderUnavailableError(
                "PBP Stats returned an invalid response.", detail=error
            ) from error
        except Exception as error:
            # A provider client can raise a non-requests exception while
            # decoding or handling a response.  Keep that implementation
            # detail behind the adapter's application error contract.
            logger.warning("PBP Stats request failed for type=%s: %s", data_type, error)
            raise ProviderUnavailableError(
                "PBP Stats could not be reached.", detail=error
            ) from error

        return payload, response

    @staticmethod
    def _canonical_data_type(data_type: str) -> str:
        """Validate and normalize the API's player/opponent selector."""

        canonical = str(data_type).strip().lower()
        if canonical not in {"player", "opponent"}:
            raise ProviderUnavailableError(
                "PBP Stats does not support the requested totals type.",
                detail=f"data_type={data_type!r}",
            )
        return canonical

    @classmethod
    def _normalize_payload(cls, payload: Any) -> pd.DataFrame:
        """Convert the PBP wire payload into a validated dataframe."""

        if not isinstance(payload, Mapping):
            raise ProviderUnavailableError(
                "PBP Stats returned an invalid response.",
                detail="payload must be an object",
            )

        rows = payload.get("multi_row_table_data")
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ProviderUnavailableError(
                "PBP Stats returned an invalid response.",
                detail="multi_row_table_data must be a list of objects",
            )

        try:
            # ``from_records`` makes the provider's row-oriented contract
            # explicit and preserves the existing PBP column names consumed by
            # the data processing service (for example, ``Name`` and
            # ``Assists``).
            frame = pd.DataFrame.from_records(rows)
            frame = frame.reset_index(drop=True)
        except (TypeError, ValueError) as error:
            raise ProviderUnavailableError(
                "PBP Stats returned an invalid response.", detail=error
            ) from error

        if any(not isinstance(column, str) or not column.strip() for column in frame.columns):
            raise ProviderUnavailableError(
                "PBP Stats returned an invalid response.",
                detail="totals rows contain an empty or non-string column name",
            )

        return frame


__all__ = ["PBPStatsAdapter", "PBPStatsProvider"]
