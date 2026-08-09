"""Synchronous, concurrency-bounded adapter for the NBA Stats provider.

Game-log handling runs as a synchronous Flask + threaded workload: requests are
served by Gunicorn worker threads and provider calls execute on the calling
thread through the blocking ``nba_api`` package.  This adapter is that seam.

Every live ``stats.nba.com`` call in the application flows through
:meth:`NBAStatsAdapter.run_endpoint`: game logs, opponent team/shot data, play
types, and player shot-location data.  Each call is wrapped in one explicit
bound, a process-shared ``threading.BoundedSemaphore`` sized by
``NBA_STATS_MAX_CONCURRENCY`` (``ProviderSettings.nba_stats_max_concurrency``),
so the number of in-flight calls can never exceed the configured limit across
all service/adapter instances in a worker process.

Each invocation also records one structured provider-telemetry event via
:mod:`app.utils.telemetry`.  The per-call ``timeout`` comes from
``ProviderSettings.nba_stats_timeout_seconds``; a provider timeout raises
``requests.exceptions.Timeout`` which the route maps to the documented 503
``provider_unavailable`` response.  When ``nba_api`` exposes the upstream HTTP
status on ``endpoint.nba_response`` it is captured on the event, and a non-2xx
status is classified as an ``http_error`` provider failure.

Callers that resolve the game-log cache can pass the matching ``cache_status``
(``CACHE_HIT``/``CACHE_MISS``/``CACHE_DISABLED``) so every event reflects the
cache behaviour behind the call;  responses served entirely from cache are
recorded through :meth:`NBAStatsAdapter.record_cache_hit` without touching the
provider.

Static ``nba_api`` players/teams lookups (``stats.static.players`` /
``stats.static.teams``) ship bundled datasets, not live HTTP calls; they are
not routed through this adapter.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable

import pandas as pd
import requests
from nba_api.stats import endpoints

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import ProviderUnavailableError
from app.utils.nba_api_config import get_nba_stats_timeout
from app.utils.telemetry import (
    CACHE_DISABLED,
    CACHE_HIT,
    NBA_STATS_OPERATIONS,
    PROVIDER_NBA_STATS,
    ProviderResponseError,
    provider_call,
    record_cached_provider_event,
)


# These are the columns consumed by ``GameService._fetch_game_logs_from_api``.
# The recorded fixture uses the smaller parser contract below because it is a
# raw provider fixture rather than the fully enriched live service frame.
GAME_LOG_REQUIRED_COLUMNS: tuple[str, ...] = (
    "GAME_ID",
    "GAME_DATE",
    "PLAYER_NAME",
    "TEAM_ID",
    "TEAM_ABBREVIATION",
    "MATCHUP",
    "MIN",
    "FGM",
    "FGA",
    "FG_PCT",
    "FG3M",
    "FG3A",
    "FTM",
    "FTA",
    "OREB",
    "DREB",
    "TOV",
    "STL",
    "BLK",
    "PF",
    "PTS",
    "REB",
    "AST",
    "PLUS_MINUS",
    "NBA_FANTASY_PTS",
)
RECORDED_GAME_LOG_REQUIRED_COLUMNS: tuple[str, ...] = (
    "GAME_ID",
    "GAME_DATE",
    "PLAYER_NAME",
    "MIN",
    "PTS",
    "REB",
    "AST",
)

_bound_lock = threading.Lock()
_shared_bounds: dict[int, threading.BoundedSemaphore] = {}


def _shared_concurrency_bound(limit: int) -> threading.BoundedSemaphore:
    """Return the one configured NBA gate for this worker process.

    The limit is the configuration identity: every app/service instance using
    the same production setting shares this gate, while tests that configure a
    different limit remain isolated.  A semaphore has no request state once
    its callers leave the context, so retaining it for the process lifetime is
    safe and avoids constructing one gate per service.
    """
    with _bound_lock:
        bound = _shared_bounds.get(limit)
        if bound is None:
            bound = threading.BoundedSemaphore(limit)
            _shared_bounds[limit] = bound
        return bound


def _validate_frame(
    frame: Any,
    *,
    required_columns: Iterable[str] = (),
    validator: Callable[[pd.DataFrame], Any] | None = None,
) -> pd.DataFrame:
    """Validate one NBA frame without retaining provider data in errors.

    An empty frame is a valid provider result when it still carries the
    endpoint's declared schema.  ``nba_api`` uses that shape for perfectly
    normal no-result queries (for example, a player with no games in a
    requested season).  Only a missing/non-DataFrame frame or a frame missing
    required columns is malformed.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ProviderResponseError(
            "NBA Stats returned an invalid data frame."
        )
    required = tuple(required_columns)
    if frame.empty and not required and validator is None:
        raise ProviderResponseError(
            "NBA Stats returned an empty data frame without a declared schema."
        )
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ProviderResponseError(
            "NBA Stats returned a data frame with an invalid schema."
        )
    if validator is not None:
        try:
            valid = validator(frame)
        except ProviderResponseError:
            raise
        except (AttributeError, ValueError, TypeError, KeyError, IndexError) as error:
            raise ProviderResponseError(
                "NBA Stats returned a data frame with an invalid schema."
            ) from error
        if valid is False:
            raise ProviderResponseError(
                "NBA Stats returned a data frame with an invalid schema."
            )
        if isinstance(valid, pd.DataFrame):
            frame = valid
    return frame


def parse_recorded_game_logs(payload: dict[str, Any]) -> pd.DataFrame:
    """Normalize a recorded ``stats.nba.com`` playergamelogs payload offline.

    The payload is fed through the same ``nba_api`` parsing path a live call
    uses (``PlayerGameLogs.load_response``), but without any network request
    or credential.  A payload that cannot produce the expected data set raises
    :class:`ProviderResponseError`, which providers classify as a ``malformed``
    provider failure.
    """
    import json

    from nba_api.stats.library.http import NBAStatsResponse

    with provider_call(
        PROVIDER_NBA_STATS,
        "player_game_logs_recorded",
        cache_status=CACHE_DISABLED,
    ):
        try:
            response = NBAStatsResponse(
                response=json.dumps(payload),
                status_code=200,
                url="https://stats.nba.com/stats/playergamelogs",
            )
            endpoint = endpoints.PlayerGameLogs(get_request=False)
            endpoint.nba_response = response
            endpoint.load_response()
            frame = endpoint.get_data_frames()[0]
            return _validate_frame(
                frame, required_columns=RECORDED_GAME_LOG_REQUIRED_COLUMNS
            )
        except ProviderResponseError:
            raise
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise ProviderResponseError(
                "The recorded NBA Stats response could not be parsed into game logs."
            ) from error


def parse_recorded_player_roster(
    payload: dict[str, Any], season: str
) -> pd.DataFrame:
    """Normalize a recorded ``CommonAllPlayers`` payload without network access."""

    import json

    from nba_api.stats.endpoints import commonallplayers
    from nba_api.stats.library.http import NBAStatsResponse

    from app.providers.nba_stats import normalize_player_roster

    with provider_call(
        PROVIDER_NBA_STATS,
        "player_roster_recorded",
        cache_status=CACHE_DISABLED,
    ):
        try:
            response = NBAStatsResponse(
                response=json.dumps(payload),
                status_code=200,
                url="https://stats.nba.com/stats/commonallplayers",
            )
            endpoint = commonallplayers.CommonAllPlayers(
                get_request=False, season=season
            )
            endpoint.nba_response = response
            endpoint.load_response()
            frame = endpoint.get_data_frames()[0]
            return normalize_player_roster(frame, season=season)
        except ProviderResponseError:
            raise
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise ProviderResponseError(
                "The recorded NBA Stats roster response could not be parsed."
            ) from error


def _response_status(endpoint: object) -> int | None:
    """Extract the upstream HTTP status ``nba_api`` recorded, if any.

    Different ``nba_api`` releases expose the status as a private
    ``_status_code`` field on ``endpoint.nba_response``; probe both spellings
    defensively so telemetry keeps working across versions.
    """
    response = getattr(endpoint, "nba_response", None)
    if response is None:
        return None
    for attr in ("status_code", "_status_code"):
        status = getattr(response, attr, None)
        if isinstance(status, int):
            return status
    return None


class NBAStatsAdapter:
    """Run NBA Stats provider calls under one explicit concurrency bound."""

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        endpoint_factory: Callable[..., object] | None = None,
        roster_endpoint_factory: Callable[..., object] | None = None,
    ):
        self.settings = settings or get_runtime_settings()
        self.timeout = get_nba_stats_timeout(self.settings)
        self._concurrency_limit = self.settings.providers.nba_stats_max_concurrency
        self._bound = _shared_concurrency_bound(self._concurrency_limit)
        self._last_status_code: int | None = None
        self._endpoint_factory = endpoint_factory
        self._roster_endpoint_factory = roster_endpoint_factory

    @property
    def max_concurrency(self) -> int:
        """The configured ``nba_stats_max_concurrency`` bound."""
        return self._concurrency_limit

    @property
    def last_status_code(self) -> int | None:
        """Return the most recent upstream status observed by this adapter."""
        return self._last_status_code

    def run_endpoint(
        self,
        operation: str,
        build: Callable[[float], object],
        *,
        frame_index: int = 0,
        cache_status: str = CACHE_DISABLED,
        required_columns: Iterable[str] = (),
        validator: Callable[[pd.DataFrame], Any] | None = None,
    ) -> pd.DataFrame | None:
        """Run one typed ``nba_api`` endpoint under the bound and telemetry.

        ``build`` receives the shared provider timeout and must return the
        endpoint instance (constructed so the blocking request has been made).
        Every response must contain a :class:`~pandas.DataFrame` with the
        required schema; an empty frame with that schema is a valid no-result
        response.
        callers may additionally provide required columns or a validator for
        endpoint-specific schemas.  Invalid provider frames are classified as
        ``malformed`` by the telemetry context rather than as application
        failures.  The upstream HTTP status is recorded on the provider event;
        a non-2xx response is classified as an ``http_error``.
        """
        if operation not in NBA_STATS_OPERATIONS:
            raise ValueError(
                f"Unsupported NBA Stats operation {operation!r}; "
                f"expected one of {sorted(NBA_STATS_OPERATIONS)}."
            )

        # Avoid exposing a previous call's status when a new request times
        # out before ``nba_api`` creates its response object.
        self._last_status_code = None
        with self._bound:
            with provider_call(
                PROVIDER_NBA_STATS,
                operation,
                cache_status=cache_status,
            ) as tracker:
                try:
                    endpoint = build(self.timeout)
                except ProviderResponseError:
                    raise
                except (
                    AttributeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                ) as error:
                    raise ProviderResponseError(
                        "NBA Stats returned a response with an invalid schema."
                    ) from error
                if endpoint is None:
                    raise ProviderResponseError(
                        "NBA Stats returned an invalid endpoint response."
                    )
                tracker.status_code = _response_status(endpoint)
                self._last_status_code = tracker.status_code
                if (
                    tracker.status_code is not None
                    and tracker.status_code >= 400
                ):
                    raise requests.exceptions.HTTPError(
                        f"{operation} responded {tracker.status_code}"
                    )
                try:
                    frames = endpoint.get_data_frames()
                    frame = frames[frame_index]
                except ProviderResponseError:
                    raise
                except (
                    AttributeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                ) as error:
                    raise ProviderResponseError(
                        "NBA Stats returned a response with an invalid schema."
                    ) from error
                return _validate_frame(
                    frame,
                    required_columns=required_columns,
                    validator=validator,
                )

    def record_cache_hit(self, operation: str) -> None:
        """Record an event for a game served without a provider call."""
        if operation not in NBA_STATS_OPERATIONS:
            raise ValueError(
                f"Unsupported NBA Stats operation {operation!r}; "
                f"expected one of {sorted(NBA_STATS_OPERATIONS)}."
            )
        record_cached_provider_event(PROVIDER_NBA_STATS, operation, CACHE_HIT)

    def get_player_game_logs(
        self,
        *,
        player_id: int,
        season: str,
        season_type: str = "Regular Season",
    ) -> pd.DataFrame:
        """Return canonical game logs through the shared instrumented seam."""
        from app.providers.nba_stats import normalize_player_game_logs

        factory = self._endpoint_factory or endpoints.playergamelogs.PlayerGameLogs
        try:
            frame = self.run_endpoint(
                "player_game_logs",
                lambda timeout: factory(
                    player_id_nullable=player_id,
                    season_nullable=season,
                    season_type_nullable=season_type,
                    timeout=timeout,
                ),
                required_columns=GAME_LOG_REQUIRED_COLUMNS,
                validator=normalize_player_game_logs,
            )
        except requests.exceptions.Timeout as error:
            raise ProviderUnavailableError(
                "The upstream stats provider timed out. Please try again shortly.",
                detail=error,
            ) from error
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(
                "The NBA Stats provider is unavailable.", detail=error
            ) from error
        return normalize_player_game_logs(frame)

    def get_archetype_game_logs(
        self,
        *,
        player_ids: Iterable[int],
        opponent_team_id: int,
        season: str,
        season_type: str = "Regular Season",
    ) -> pd.DataFrame:
        """Return normalized game logs filtered to a cluster's player IDs."""
        from app.providers.nba_stats import normalize_archetype_game_logs

        factory = self._endpoint_factory or endpoints.playergamelogs.PlayerGameLogs
        try:
            frame = self.run_endpoint(
                "player_gamelogs_against",
                lambda timeout: factory(
                    season_nullable=season,
                    season_type_nullable=season_type,
                    opp_team_id_nullable=opponent_team_id,
                    timeout=timeout,
                ),
                required_columns=GAME_LOG_REQUIRED_COLUMNS,
                validator=normalize_archetype_game_logs,
            )
        except requests.exceptions.Timeout as error:
            raise ProviderUnavailableError(
                "The upstream stats provider timed out. Please try again shortly.",
                detail=error,
            ) from error
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(
                "The NBA Stats provider is unavailable.", detail=error
            ) from error
        normalized = normalize_archetype_game_logs(frame)
        return normalized[normalized["PLAYER_ID"].isin(player_ids)].reset_index(drop=True)

    def get_player_roster(self, *, season: str) -> pd.DataFrame:
        """Return one explicit season's normalized CommonAllPlayers roster."""

        from app.providers.nba_stats import normalize_player_roster
        from app.providers.nba_stats import validate_canonical_season

        season = validate_canonical_season(season)
        factory = self._roster_endpoint_factory or endpoints.commonallplayers.CommonAllPlayers

        def normalize(frame: pd.DataFrame) -> pd.DataFrame:
            return normalize_player_roster(frame, season=season)

        try:
            frame = self.run_endpoint(
                "player_roster",
                lambda timeout: factory(
                    is_only_current_season=0,
                    season=season,
                    timeout=timeout,
                ),
                validator=normalize,
            )
        except requests.exceptions.Timeout as error:
            raise ProviderUnavailableError(
                "The upstream stats provider timed out. Please try again shortly.",
                detail=error,
            ) from error
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(
                "The NBA Stats provider is unavailable.", detail=error
            ) from error
        except ProviderResponseError as error:
            raise ProviderUnavailableError(
                "The NBA Stats roster response was malformed.", detail=error
            ) from error
        return frame

    def fetch_player_game_logs(
        self,
        player_id: int,
        season: str,
        *,
        cache_status: str = CACHE_DISABLED,
    ) -> pd.DataFrame:
        """Fetch one player's regular-season game logs for ``season``."""

        def build(timeout: float) -> object:
            return endpoints.playergamelogs.PlayerGameLogs(
                player_id_nullable=player_id,
                season_nullable=season,
                season_type_nullable="Regular Season",
                timeout=timeout,
            )

        return self.run_endpoint(
            "player_game_logs",
            build,
            cache_status=cache_status,
            required_columns=GAME_LOG_REQUIRED_COLUMNS,
        )

    def fetch_opponent_team_stats(
        self,
        date_from: str | None,
        *,
        cache_status: str = CACHE_DISABLED,
        per_mode_detailed: str = "Per48",
        league_id: str = "00",
    ) -> pd.DataFrame | None:
        """Fetch league opponent team stats from the cutoff ``date_from``."""

        def build(timeout: float) -> object:
            return endpoints.LeagueDashTeamStats(
                measure_type_detailed_defense="Opponent",
                per_mode_detailed=per_mode_detailed,
                date_from_nullable=date_from,
                league_id_nullable=league_id,
                timeout=timeout,
            )

        return self.run_endpoint(
            "league_opponent_team_stats",
            build,
            cache_status=cache_status,
            required_columns=("TEAM_ID", "TEAM_NAME"),
        )

    def fetch_opponent_shot_chart(
        self,
        general_range: str,
        date_from: str | None,
        *,
        cache_status: str = CACHE_DISABLED,
        per_mode_simple: str = "PerGame",
        league_id: str = "00",
    ) -> pd.DataFrame:
        """Fetch league opponent shot data (catch-and-shoot / pull-ups)."""

        def build(timeout: float) -> object:
            return endpoints.LeagueDashOppPtShot(
                general_range_nullable=general_range,
                date_from_nullable=date_from,
                per_mode_simple=per_mode_simple,
                league_id_nullable=league_id,
                timeout=timeout,
            )

        return self.run_endpoint(
            "league_opponent_shot_chart",
            build,
            cache_status=cache_status,
            required_columns=("TEAM_ID", "TEAM_NAME", "FG2M", "FG3M"),
        )

    def fetch_opponent_shooting_zone(
        self,
        date_from: str | None,
        *,
        cache_status: str = CACHE_DISABLED,
        per_mode_detailed: str = "PerGame",
        league_id: str = "00",
    ) -> pd.DataFrame:
        """Fetch opponent shot-location data through the shared NBA seam."""

        def build(timeout: float) -> object:
            return endpoints.LeagueDashTeamShotLocations(
                distance_range="By Zone",
                measure_type_simple="Opponent",
                per_mode_detailed=per_mode_detailed,
                date_from_nullable=date_from,
                league_id_nullable=league_id,
                timeout=timeout,
            )

        return self.run_endpoint(
            "league_opponent_shooting_zone",
            build,
            cache_status=cache_status,
            required_columns=("TEAM_ID", "TEAM_NAME"),
        )

    def fetch_synergy_play_types(
        self,
        play_type: str,
        *,
        player_or_team_abbreviation: str,
        type_grouping: str,
        league_id: str = "00",
    ) -> pd.DataFrame:
        """Fetch one typed Synergy play-type frame."""

        if player_or_team_abbreviation not in {"T", "P"}:
            raise ValueError("player_or_team_abbreviation must be 'T' or 'P'.")
        operation = (
            "synergy_team_play_types"
            if player_or_team_abbreviation == "T"
            else "synergy_player_play_types"
        )

        def build(timeout: float) -> object:
            return endpoints.SynergyPlayTypes(
                play_type_nullable=play_type,
                player_or_team_abbreviation=player_or_team_abbreviation,
                type_grouping_nullable=type_grouping,
                league_id_nullable=league_id,
                timeout=timeout,
            )

        required_columns = (
            ("TEAM_NAME", "GP", "PTS")
            if player_or_team_abbreviation == "T"
            else ("PLAYER_NAME", "TEAM_ABBREVIATION", "PTS")
        )
        return self.run_endpoint(
            operation,
            build,
            required_columns=required_columns,
        )

    def fetch_player_per36_stats(self) -> pd.DataFrame:
        """Fetch the player per-36 baseline used by archetype calculations."""

        return self.run_endpoint(
            "player_per36_stats",
            lambda timeout: endpoints.LeagueDashPlayerStats(
                measure_type_detailed_defense="Base",
                per_mode_detailed="Per36",
                timeout=timeout,
            ),
            required_columns=("PLAYER_ID",),
        )

    def fetch_player_shooting_zone(
        self,
        date_from: str | None = None,
        *,
        per_mode_detailed: str = "PerGame",
    ) -> pd.DataFrame:
        """Fetch player shooting-zone data for the refresh/profile tables."""

        return self.run_endpoint(
            "player_shooting_zone",
            lambda timeout: endpoints.LeagueDashPlayerShotLocations(
                distance_range="By Zone",
                per_mode_detailed=per_mode_detailed,
                date_from_nullable=date_from,
                timeout=timeout,
            ),
            required_columns=("PLAYER_ID", "PLAYER_NAME", "TEAM_ID"),
        )

    def fetch_player_shot_chart(self, player_id: int, team_id: int) -> pd.DataFrame:
        """Fetch one player's shot-chart profile."""

        return self.run_endpoint(
            "player_shot_chart",
            lambda timeout: endpoints.PlayerDashPtShots(
                player_id=player_id,
                team_id=team_id,
                per_mode_simple="PerGame",
                timeout=timeout,
            ),
            frame_index=1,
            required_columns=("SHOT_TYPE",),
        )

    def fetch_player_gamelogs_against(
        self,
        team_id: int,
        season: str,
    ) -> pd.DataFrame:
        """Fetch game logs for the current season against one opponent."""

        return self.run_endpoint(
            "player_gamelogs_against",
            lambda timeout: endpoints.playergamelogs.PlayerGameLogs(
                season_nullable=season,
                opp_team_id_nullable=team_id,
                timeout=timeout,
            ),
            required_columns=(
                "PLAYER_ID",
                "PLAYER_NAME",
                "GAME_DATE",
                "MIN",
                "FGM",
                "FGA",
                "FG3M",
                "FG3A",
                "FTM",
                "FTA",
                "PTS",
                "TOV",
            ),
        )

    def health_probe(self) -> pd.DataFrame:
        """Probe ``stats.nba.com`` and retain the upstream status."""

        return self.run_endpoint(
            "health_probe",
            lambda timeout: endpoints.LeagueDashTeamStats(
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="Per48",
                league_id_nullable="00",
                timeout=timeout,
            ),
            required_columns=("TEAM_ID", "TEAM_NAME"),
        )


__all__ = [
    "GAME_LOG_REQUIRED_COLUMNS",
    "NBAStatsAdapter",
    "parse_recorded_game_logs",
]
