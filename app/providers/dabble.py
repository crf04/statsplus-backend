"""Read-only adapter for Dabble daily-fantasy player lines.

Dabble does not publish a supported developer API.  This adapter isolates its
current public mobile read surface behind :class:`DFSLineProvider`, validates
recorded payloads before exposing them, and never implements account or entry
placement operations.  The upstream feed is geo/bot gated, so callers receive
the standard provider-unavailable error when it cannot be reached.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import quote

import requests
from urllib3.util.retry import Retry

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import InvalidInputError, ProviderUnavailableError
from app.providers.dfs import canonical_stat_components
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_DABBLE,
    ProviderResponseError,
    increment_retry_count,
    provider_call,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")


class _DabbleRetry(Retry):
    """Count safe HTTP retries in the shared provider telemetry event."""

    def increment(
        self,
        method=None,
        url=None,
        response=None,
        error=None,
        _pool=None,
        _stacktrace=None,
    ):
        next_retry = super().increment(
            method, url, response, error, _pool, _stacktrace
        )
        increment_retry_count()
        logger.warning(
            "Dabble retry after status=%s",
            getattr(response, "status", "error"),
        )
        return next_retry


def _build_session(settings: RuntimeSettings) -> requests.Session:
    session = requests.Session()
    retry = _DabbleRetry(
        total=settings.providers.dabble_max_retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD", "OPTIONS"),
    )
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=settings.providers.dabble_pool_connections,
        pool_maxsize=settings.providers.dabble_pool_maxsize,
        max_retries=retry,
    )
    session.mount("https://", adapter)
    session.headers.update(DabbleAdapter.DEFAULT_HEADERS)
    return session


class DabbleAdapter:
    """Fetch and normalize public Dabble Pick'em lines."""

    BASE_URL = "https://api.dabble.com.au"
    PROVIDER_ID = "dabble"
    DEFAULT_HEADERS = {
        "Accept": "application/json",
        "Accept-Language": "en-AU,en;q=0.9",
        "User-Agent": "Dabble/1000041710 CFNetwork/3826.600.41.2.1 Darwin/24.6.0",
        "X-Device-ID": "00000000-0000-0000-0000-000000000000",
        "X-App-Version": "4.17.10+019ededb",
    }

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        session: requests.Session | Any | None = None,
    ) -> None:
        self.settings = settings or get_runtime_settings()
        self.session = session or _build_session(self.settings)

    @property
    def timeout(self) -> tuple[float, float]:
        return (
            self.settings.providers.dabble_connect_timeout_seconds,
            self.settings.providers.dabble_read_timeout_seconds,
        )

    def list_competitions(
        self, *, sport: str | None = None, sport_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Discover active competitions, optionally scoped to one sport."""

        resolved_sport = None
        if sport and sport_id:
            raise InvalidInputError("Use either sport or sport_id, not both.")
        if sport:
            sports = self._request_json("sports", "/sports", parser=self._parse_sports)
            resolved_sport = next(
                (row for row in sports if row["name"].casefold() == sport.casefold()),
                None,
            )
            if resolved_sport is None:
                return []
            sport_id = resolved_sport["id"]
        if sport_id:
            self._validated_id(sport_id, "sport_id")

        rows = self._request_json(
            "active_competitions",
            "/competitions/active",
            params={"sportId": sport_id} if sport_id else None,
            parser=self._parse_active_competitions,
        )
        if resolved_sport:
            for row in rows:
                row["sport"] = row["sport"] or resolved_sport["name"]
                row["sport_id"] = row["sport_id"] or resolved_sport["id"]
        return rows

    def fetch_lines(
        self,
        *,
        competition: str | None = None,
        competition_id: str | None = None,
        fixture_id: str | None = None,
        fixture_limit: int = 3,
        include_in_play: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch normalized player props for a fixture or competition."""

        if fixture_id:
            self._validated_id(fixture_id, "fixture_id")
            return self._fixture_lines(fixture_id)

        resolved_competition_id = competition_id
        if competition:
            if len(competition.strip()) > 120:
                raise InvalidInputError("competition is too long.")
            competitions = self._request_json(
                "competition_lookup",
                "/competitions",
                params={"name": competition.strip()},
                parser=self._parse_competition_lookup,
            )
            exact = next(
                (
                    row
                    for row in competitions
                    if row["name"].casefold() == competition.strip().casefold()
                ),
                None,
            )
            if exact is None:
                from app.errors import ResourceNotFoundError

                raise ResourceNotFoundError(
                    f"No active Dabble competition named {competition.strip()!r} was found."
                )
            resolved_competition_id = exact["id"]

        if not resolved_competition_id:
            raise InvalidInputError(
                "competition, competition_id, or fixture_id is required."
            )
        self._validated_id(resolved_competition_id, "competition_id")

        fixtures = self._request_json(
            "competition_fixtures",
            "/frontend-api/competitions/"
            f"{quote(resolved_competition_id, safe='')}/sport-fixtures",
            params={"includeInPlay": "true" if include_in_play else "false"},
            parser=self._parse_fixtures,
        )
        open_fixtures = [
            row
            for row in fixtures
            if row.get("id")
            and (include_in_play or str(row.get("status", "Open")) == "Open")
        ]
        open_fixtures.sort(key=lambda row: str(row.get("advertisedStart") or ""))

        lines: list[dict[str, Any]] = []
        for fixture in open_fixtures[:fixture_limit]:
            lines.extend(self._fixture_lines(str(fixture["id"])))
        return lines

    def _fixture_lines(self, fixture_id: str) -> list[dict[str, Any]]:
        return self._request_json(
            "fixture_details",
            "/frontend-api/sport-fixtures/details/"
            f"{quote(fixture_id, safe='')}",
            parser=self.parse_fixture_lines,
        )

    def _request_json(
        self,
        operation: str,
        path: str,
        *,
        parser: Callable[[Any], T],
        params: dict[str, Any] | None = None,
    ) -> T:
        """Perform one instrumented GET and parse it inside the provider seam."""

        try:
            with provider_call(
                PROVIDER_DABBLE, operation, cache_status=CACHE_DISABLED
            ) as tracker:
                response = self.session.get(
                    f"{self.BASE_URL}{path}", params=params, timeout=self.timeout
                )
                tracker.status_code = response.status_code
                response.raise_for_status()
                try:
                    payload = response.json()
                except (TypeError, ValueError) as error:
                    raise ProviderResponseError(
                        "Dabble returned a response that was not valid JSON."
                    ) from error
                return parser(payload)
        except ProviderResponseError as error:
            raise ProviderUnavailableError(
                "Dabble returned invalid line data.", detail=error
            ) from error
        except requests.exceptions.Timeout as error:
            raise ProviderUnavailableError(
                "Dabble timed out while fetching line data.", detail=error
            ) from error
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(
                "Dabble line data could not be reached.", detail=error
            ) from error

    @staticmethod
    def _parse_sports(payload: Any) -> list[dict[str, str]]:
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise ProviderResponseError("Dabble sports payload is malformed.")
        normalized = []
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("id") or not row.get("name"):
                raise ProviderResponseError("Dabble sports payload has malformed rows.")
            normalized.append({"id": str(row["id"]), "name": str(row["name"])})
        return normalized

    @classmethod
    def _parse_active_competitions(cls, payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        rows = data.get("activeCompetitions") if isinstance(data, Mapping) else None
        return cls._normalize_competitions(rows)

    @classmethod
    def _parse_competition_lookup(cls, payload: Any) -> list[dict[str, Any]]:
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        return cls._normalize_competitions(rows)

    @staticmethod
    def _normalize_competitions(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise ProviderResponseError("Dabble competitions payload is malformed.")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("id") or not row.get("name"):
                raise ProviderResponseError(
                    "Dabble competitions payload has malformed rows."
                )
            result.append(
                {
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "sport_id": str(row["sportId"]) if row.get("sportId") else None,
                    "sport": str(row["sportName"]) if row.get("sportName") else None,
                    "country": str(row["country"]) if row.get("country") else None,
                    "featured": bool(row.get("featured", False)),
                }
            )
        return result

    @staticmethod
    def _parse_fixtures(payload: Any) -> list[dict[str, Any]]:
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) for row in rows
        ):
            raise ProviderResponseError("Dabble fixtures payload is malformed.")
        if any(not row.get("id") for row in rows):
            raise ProviderResponseError("Dabble fixture is missing an id.")
        return [dict(row) for row in rows]

    @staticmethod
    def parse_fixture_lines(payload: Any) -> list[dict[str, Any]]:
        """Validate one recorded fixture-details payload and normalize its props."""

        detail = (
            payload.get("sportFixtureDetail")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(detail, Mapping) or not detail.get("id"):
            raise ProviderResponseError("Dabble fixture details are malformed.")
        props = detail.get("playerProps")
        if not isinstance(props, list):
            raise ProviderResponseError("Dabble playerProps must be a list.")

        lines: list[dict[str, Any]] = []
        for prop in props:
            if not isinstance(prop, Mapping):
                raise ProviderResponseError("Dabble playerProps contains a malformed row.")
            stats = prop.get("stats")
            value = prop.get("value")
            multiplier = prop.get("multiplier")
            if (
                not prop.get("playerName")
                or not prop.get("marketId")
                or not prop.get("selectionId")
                or not isinstance(stats, list)
                or not stats
                or any(not isinstance(stat, str) or not stat.strip() for stat in stats)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not isinstance(prop.get("lineType"), str)
                or not str(prop["lineType"]).strip()
                or (
                    multiplier is not None
                    and (
                        isinstance(multiplier, bool)
                        or not isinstance(multiplier, (int, float))
                        or not math.isfinite(float(multiplier))
                        or float(multiplier) <= 0
                    )
                )
            ):
                raise ProviderResponseError("Dabble playerProps has an invalid schema.")

            normalized_stats = canonical_stat_components(stats)
            lines.append(
                {
                    "provider": DabbleAdapter.PROVIDER_ID,
                    "fixture_id": str(detail["id"]),
                    "fixture": detail.get("name"),
                    "starts_at": detail.get("advertisedStart"),
                    "competition_id": detail.get("competitionId"),
                    "competition": detail.get("competitionName"),
                    "sport_id": detail.get("sportId"),
                    "sport": detail.get("sportName"),
                    "player_id": prop.get("playerId"),
                    "player_name": str(prop["playerName"]),
                    "team_id": prop.get("teamId"),
                    "team": prop.get("teamName"),
                    "team_abbreviation": prop.get("teamAbbreviation"),
                    "position": prop.get("position"),
                    "market_id": str(prop["marketId"]),
                    "selection_id": str(prop["selectionId"]),
                    "stats": normalized_stats,
                    "stat": "+".join(normalized_stats),
                    "line": float(value),
                    "direction": str(prop["lineType"]).strip().lower(),
                    "multiplier": (
                        float(multiplier) if multiplier is not None else None
                    ),
                    "multiplier_scope": (
                        "selection" if multiplier is not None else None
                    ),
                }
            )
        return lines

    @staticmethod
    def _validated_id(value: str, field: str) -> str:
        normalized = str(value).strip()
        if not _ID_PATTERN.fullmatch(normalized):
            raise InvalidInputError(f"{field} is invalid.")
        return normalized


__all__ = ["DabbleAdapter"]
