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

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import InvalidInputError
from app.models.catalogs import PBP_DATA_KINDS, PBPDataKind
from app.utils.nba_api_config import get_shared_nba_session
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_PBP_STATS,
    ProviderResponseError,
    provider_call,
)

PBP_TOTALS_URL = "https://api.pbpstats.com/get-totals/nba"
PBP_GAME_LOGS_URL = "https://api.pbpstats.com/get-game-logs/nba"
PBP_WOWY_URL = "https://api.pbpstats.com/get-wowy-stats/nba"
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
PBP_PLAYER_DIET_IDENTITY_COLUMNS: tuple[str, ...] = (
    "EntityId",
    "Name",
    "GamesPlayed",
    "Assists",
)
PBP_PLAYER_DIET_VOLUME_COLUMNS: tuple[str, ...] = (
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
PBP_PLAYER_DIET_COLUMNS = (
    *PBP_PLAYER_DIET_IDENTITY_COLUMNS,
    *PBP_PLAYER_DIET_VOLUME_COLUMNS,
)


@dataclass(frozen=True, slots=True)
class WowyPlayerRow:
    """One teammate's raw totals in one WOWY lineup state.

    ``player_id`` is the StatsPlus player id (the NBA person id); the PBP
    ``EntityId`` string is translated here so no caller sees the wire id.
    ``fga`` is ``FG2A + FG3A``.  A counter PBP omits (it sends ``null`` when a
    player never recorded one) is zero.
    """

    player_id: int
    name: str
    minutes: int
    possessions: int
    usage: float | None
    points: int
    rebounds: int
    assists: int
    fg3m: int
    fga: int
    fg3a: int = 0
    tov: int = 0
    fta: int = 0


def to_pbp_player_id(player_id: int) -> str:
    """Translate a StatsPlus player id to PBP's ``EntityId``.

    Both are NBA person ids (Jokic is 203999 on each), so the translation is
    the decimal string; it lives here so a future divergence has one home.
    """

    return str(int(player_id))


def from_pbp_player_id(entity_id: object) -> int:
    """Translate PBP's ``EntityId`` back to a StatsPlus player id."""

    return int(str(entity_id))


def _wowy_count(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderResponseError(f"PBP Stats WOWY payload has a non-numeric {key}.")
    return int(value)


def wowy_request_descriptor(
    *,
    team_id: int,
    season: str,
    season_type: str,
    on_player_ids: Sequence[int],
    off_player_ids: Sequence[int],
) -> dict[str, object]:
    """Return the exact non-secret PBP WOWY request sent on the wire.

    ``0ExactlyNOnFloor`` lists players of whom exactly N are on the floor:
    with N the size of the on-set, all of them together; ``0Exactly0OnFloor``
    lists the players required off it.  Several ids are comma separated
    (verified against the live endpoint 2026-10-16).
    """

    parameters: dict[str, object] = {
        "Season": season,
        "SeasonType": season_type,
        "TeamId": str(team_id),
        "Type": "Player",
    }
    if on_player_ids:
        # ``0ExactlyNOnFloor`` means exactly N of the listed players are on
        # the floor, so N = len(on) selects "all of them together"; a fixed 1
        # would select "exactly one of them".
        parameters[f"0Exactly{len(on_player_ids)}OnFloor"] = ",".join(
            to_pbp_player_id(pid) for pid in on_player_ids
        )
    if off_player_ids:
        parameters["0Exactly0OnFloor"] = ",".join(to_pbp_player_id(pid) for pid in off_player_ids)
    return {
        "adapter": "pbp_stats",
        "operation": "get_wowy_player",
        "endpoint": PBP_WOWY_URL,
        "parameters": parameters,
    }


def _date_key(value: object) -> str:
    text = str(value).strip()
    if len(text) < 10:
        raise ProviderResponseError("PBP Stats returned an invalid game date.")
    key = text[:10]
    try:
        parsed = datetime.strptime(key, "%m/%d/%Y").date() if "/" in key else date.fromisoformat(key)
    except ValueError as error:
        raise ProviderResponseError("PBP Stats returned an invalid game date.") from error
    return parsed.isoformat()


def totals_request_descriptor(
    data_type: PBPDataKind,
    *,
    season: str,
    season_type: str,
    team_id: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, object]:
    """Return the exact non-secret PBP totals request sent on the wire."""

    provider_season_type = (
        PBP_REGULAR_SEASON if season_type in {"Regular Season", PBP_REGULAR_SEASON} else season_type
    )
    parameters: dict[str, object] = {
        "Season": season,
        "SeasonType": provider_season_type,
        "Type": "Player" if data_type == "player" else "Opponent",
    }
    if team_id is not None:
        parameters["TeamId"] = str(team_id)
    if from_date is not None:
        parameters["FromDate"] = from_date
    if to_date is not None:
        parameters["ToDate"] = to_date
    return {
        "adapter": "pbp_stats",
        "operation": "get_totals_player" if data_type == "player" else "get_totals_opponent",
        "endpoint": PBP_TOTALS_URL,
        "parameters": parameters,
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
        self._last_transport_requests: dict[str, dict[str, object]] = {}

    def transport_request_descriptor(self, operation: str) -> dict[str, object] | None:
        descriptor = self._last_transport_requests.get(operation)
        return json.loads(json.dumps(descriptor)) if descriptor is not None else None

    @property
    def connect_timeout(self) -> float:
        return self.settings.providers.pbp_connect_timeout_seconds

    @property
    def read_timeout(self) -> float:
        return self.settings.providers.pbp_read_timeout_seconds

    @contextmanager
    def _request(
        self,
        operation: str,
        params: dict[str, str],
        *,
        url: str | None = None,
    ) -> Iterator[Any]:
        """Execute one instrumented PBP request and yield its response."""

        request_url = url or self.base_url
        self._last_transport_requests[operation] = {
            "adapter": "pbp_stats",
            "operation": operation,
            "endpoint": request_url,
            "parameters": json.loads(json.dumps(params, sort_keys=True)),
        }
        with provider_call(
            PROVIDER_PBP_STATS,
            operation,
            cache_status=CACHE_DISABLED,
        ) as tracker:
            response = self.session.get(
                request_url,
                params=params,
                timeout=(self.connect_timeout, self.read_timeout),
            )
            tracker.status_code = response.status_code
            response.raise_for_status()
            yield response

    def fetch_team_game_ids(
        self,
        team_id: int,
        season: str,
        *,
        season_type: str = "Regular Season",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[str, ...]:
        """Return exact team membership from PBP's independent game-log seam.

        ``get-totals`` proves only aggregate counts.  The game-log endpoint is
        queried separately with the team identity and bounded dates so the
        assist surface carries PBP-selected game IDs rather than IDs copied
        from NBA Stats or the Event Catalog.
        """

        if not isinstance(team_id, int) or isinstance(team_id, bool) or team_id <= 0:
            raise ValueError("team_id must be a positive integer")
        params = {
            "Season": season,
            "SeasonType": (
                PBP_REGULAR_SEASON if season_type in {"Regular Season", PBP_REGULAR_SEASON} else season_type
            ),
            "EntityType": "Team",
            "EntityId": str(team_id),
        }
        if date_from is not None:
            params["FromDate"] = date_from
        if date_to is not None:
            params["ToDate"] = date_to
        with self._request(
            "team_game_log",
            params,
            url=PBP_GAME_LOGS_URL,
        ) as response:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderResponseError(
                    "PBP Stats returned a response that was not valid JSON."
                ) from error
        rows = payload.get("multi_row_table_data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ProviderResponseError("PBP Stats team game-log payload has an invalid row list.")
        lower_bound = _date_key(date_from) if date_from is not None else None
        upper_bound = _date_key(date_to) if date_to is not None else None
        game_ids: list[str] = []
        for row in rows:
            game_id = row.get("GameId", row.get("GAME_ID"))
            played_on = row.get("Date", row.get("GAME_DATE"))
            if game_id is None or played_on is None:
                raise ProviderResponseError("PBP Stats team game-log payload is missing game identity.")
            game_id = str(game_id).strip()
            if not game_id:
                raise ProviderResponseError("PBP Stats team game-log payload has an empty game ID.")
            day = _date_key(played_on)
            if lower_bound is not None and day < lower_bound:
                continue
            if upper_bound is not None and day > upper_bound:
                continue
            game_ids.append(game_id)
        if len(game_ids) != len(set(game_ids)):
            raise ProviderResponseError("PBP Stats team game-log payload contains duplicate game IDs.")
        return tuple(sorted(game_ids))

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
                f"Unsupported PBP data type {data_type!r}. " f"Expected one of {sorted(PBP_DATA_KINDS)}."
            )
        descriptor = totals_request_descriptor(
            data_type,
            season=season or self.settings.nba.current_season,
            season_type=season_type,
            team_id=team_id,
            from_date=from_date,
            to_date=to_date,
        )
        params = dict(descriptor["parameters"])  # type: ignore[arg-type]
        operation = str(descriptor["operation"])

        with self._request(operation, params) as response:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderResponseError(
                    "PBP Stats returned a response that was not valid JSON."
                ) from error
            return type(self).parse_totals(payload, data_type=data_type)

    def fetch_wowy_rows(
        self,
        *,
        team_id: int,
        season: str,
        on_player_ids: Sequence[int] = (),
        off_player_ids: Sequence[int] = (),
        season_type: str = "Regular Season",
    ) -> tuple[WowyPlayerRow, ...]:
        """Fetch one team's WOWY rows for the given on/off-floor players."""

        descriptor = wowy_request_descriptor(
            team_id=team_id,
            season=season,
            season_type=season_type,
            on_player_ids=on_player_ids,
            off_player_ids=off_player_ids,
        )
        with self._request(
            str(descriptor["operation"]),
            dict(descriptor["parameters"]),  # type: ignore[arg-type]
            url=PBP_WOWY_URL,
        ) as response:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderResponseError(
                    "PBP Stats returned a response that was not valid JSON."
                ) from error
            return type(self).parse_wowy(payload)

    @staticmethod
    def parse_wowy(payload: Any) -> tuple[WowyPlayerRow, ...]:
        """Validate and normalize a recorded PBP WOWY payload."""

        rows = payload.get("multi_row_table_data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ProviderResponseError("PBP Stats WOWY payload is missing a list of rows.")
        parsed: list[WowyPlayerRow] = []
        for row in rows:
            try:
                player_id = from_pbp_player_id(row["EntityId"])
                name = str(row["Name"])
            except (KeyError, ValueError, TypeError) as error:
                raise ProviderResponseError(
                    "PBP Stats WOWY payload has an invalid player identity."
                ) from error
            usage = row.get("Usage")
            if usage is not None and (isinstance(usage, bool) or not isinstance(usage, (int, float))):
                raise ProviderResponseError("PBP Stats WOWY payload has a non-numeric Usage.")
            parsed.append(
                WowyPlayerRow(
                    player_id=player_id,
                    name=name,
                    minutes=_wowy_count(row, "Minutes"),
                    possessions=_wowy_count(row, "OffPoss"),
                    usage=None if usage is None else float(usage),
                    points=_wowy_count(row, "Points"),
                    rebounds=_wowy_count(row, "Rebounds"),
                    assists=_wowy_count(row, "Assists"),
                    fg3m=_wowy_count(row, "FG3M"),
                    fga=_wowy_count(row, "FG2A") + _wowy_count(row, "FG3A"),
                    fg3a=_wowy_count(row, "FG3A"),
                    tov=_wowy_count(row, "Turnovers"),
                    fta=_wowy_count(row, "FTA"),
                )
            )
        return tuple(parsed)

    def fetch_player_diet_totals(self, *, season: str) -> pd.DataFrame:
        """Fetch one league-wide player total set for assist-location Diets."""

        params = {
            "Season": season,
            "SeasonType": PBP_REGULAR_SEASON,
            "Type": "Player",
        }
        with self._request("get_totals_player_diet", params) as response:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderResponseError(
                    "PBP Stats returned a response that was not valid JSON."
                ) from error
            return type(self).parse_player_diet_totals(payload)

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
                f"Unsupported PBP data type {data_type!r}. " f"Expected one of {sorted(PBP_DATA_KINDS)}."
            )
        rows = payload.get("multi_row_table_data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderResponseError("PBP Stats totals payload is missing a list of rows.")
        if rows and not all(isinstance(row, dict) for row in rows):
            raise ProviderResponseError("PBP Stats totals payload contains malformed rows.")
        required_columns = PBP_REQUIRED_COLUMNS[data_type]
        if not rows:
            # An empty provider result carries no column names.  Materialize
            # the declared schema explicitly so a valid table is never
            # replaced by a schema-less DataFrame.
            return pd.DataFrame(columns=required_columns)
        # The PBP wire is sparse: optional counters a row never observed are
        # omitted, so row key sets legitimately differ (production 2025-26
        # opponent totals: 230 keys in the union, 228 in every row).  The
        # contract is that every row carries every required column.
        if any(any(column not in row for column in required_columns) for row in rows):
            raise ProviderResponseError("PBP Stats totals payload has an invalid schema.")
        return pd.DataFrame(rows)

    @staticmethod
    def parse_player_diet_totals(payload: Any) -> pd.DataFrame:
        """Project sparse PBP player rows to the exact Diet contract."""

        rows = payload.get("multi_row_table_data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderResponseError("PBP Stats player Diet payload is missing a list of rows.")
        if not all(isinstance(row, dict) for row in rows):
            raise ProviderResponseError("PBP Stats player Diet payload contains malformed rows.")
        if any(any(column not in row for column in PBP_PLAYER_DIET_IDENTITY_COLUMNS) for row in rows):
            raise ProviderResponseError("PBP Stats player Diet payload has an invalid schema.")
        return pd.DataFrame(
            [
                {
                    **{column: row[column] for column in PBP_PLAYER_DIET_IDENTITY_COLUMNS},
                    **{column: row.get(column) for column in PBP_PLAYER_DIET_VOLUME_COLUMNS},
                }
                for row in rows
            ],
            columns=PBP_PLAYER_DIET_COLUMNS,
        )


__all__ = [
    "PBP_GAME_LOGS_URL",
    "PBP_OPPONENT_REQUIRED_COLUMNS",
    "PBP_PLAYER_DIET_COLUMNS",
    "PBP_PLAYER_DIET_IDENTITY_COLUMNS",
    "PBP_PLAYER_DIET_VOLUME_COLUMNS",
    "PBP_PLAYER_REQUIRED_COLUMNS",
    "PBP_REGULAR_SEASON",
    "PBP_REQUIRED_COLUMNS",
    "PBP_TOTALS_URL",
    "PBP_WOWY_URL",
    "PBPTotalsAdapter",
    "WowyPlayerRow",
    "from_pbp_player_id",
    "to_pbp_player_id",
    "wowy_request_descriptor",
]
