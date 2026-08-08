"""Synchronous, concurrency-bounded adapter for the NBA Stats provider.

Game-log handling runs as a synchronous Flask + threaded workload: requests are
served by Gunicorn worker threads and provider calls execute on the calling
thread through the blocking ``nba_api`` package.  This adapter is that seam.

Every live ``stats.nba.com`` call in the application flows through
:meth:`NBAStatsAdapter.run_endpoint`: game logs, next-game lookups, opponent
team/shot data, play types, and player shot-location data.  Each call is
wrapped in one explicit bound, a ``threading.BoundedSemaphore`` sized by
``NBA_STATS_MAX_CONCURRENCY`` (``ProviderSettings.nba_stats_max_concurrency``),
so the number of in-flight calls can never exceed the configured limit
regardless of how many request threads arrive.  Providing one adapter shared by
a service instance keeps the bound unambiguous within a worker.

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
from typing import Any, Callable

import pandas as pd
import requests
from nba_api.stats import endpoints

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.utils.nba_api_config import get_nba_stats_timeout
from app.utils.telemetry import (
    CACHE_HIT,
    CACHE_MISS,
    PROVIDER_NBA_STATS,
    ProviderResponseError,
    provider_call,
    record_cached_provider_event,
)


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

    try:
        response = NBAStatsResponse(
            response=json.dumps(payload),
            status_code=200,
            url="https://stats.nba.com/stats/playergamelogs",
        )
        endpoint = endpoints.PlayerGameLogs(get_request=False)
        endpoint.nba_response = response
        endpoint.load_response()
        return endpoint.get_data_frames()[0]
    except ProviderResponseError:
        raise
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise ProviderResponseError(
            "The recorded NBA Stats response could not be parsed into game logs."
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

    def __init__(self, settings: RuntimeSettings | None = None):
        self.settings = settings or get_runtime_settings()
        self.timeout = get_nba_stats_timeout(self.settings)
        self._bound = threading.BoundedSemaphore(
            self.settings.providers.nba_stats_max_concurrency
        )
        self._concurrency_limit = self.settings.providers.nba_stats_max_concurrency

    @property
    def max_concurrency(self) -> int:
        """The configured ``nba_stats_max_concurrency`` bound."""
        return self._concurrency_limit

    def run_endpoint(
        self,
        operation: str,
        build: Callable[[float], object],
        *,
        frame_index: int = 0,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame | None:
        """Run one typed ``nba_api`` endpoint under the bound and telemetry.

        ``build`` receives the shared provider timeout and must return the
        endpoint instance (constructed so the blocking request has been made).
        The primary frame (``None`` when the response carries no data sets) is
        returned.  The upstream HTTP status is recorded on the provider event;
        a non-2xx response is classified as an ``http_error``.
        """
        with self._bound:
            with provider_call(
                PROVIDER_NBA_STATS,
                operation,
                cache_status=cache_status,
            ) as tracker:
                endpoint = build(self.timeout)
                tracker.status_code = _response_status(endpoint)
                if (
                    tracker.status_code is not None
                    and tracker.status_code >= 400
                ):
                    raise requests.exceptions.HTTPError(
                        f"{operation} responded {tracker.status_code}"
                    )
                frames = endpoint.get_data_frames()
                if not frames:
                    return None
                return frames[frame_index]

    def record_cache_hit(self, operation: str) -> None:
        """Record an event for a game served without a provider call."""
        record_cached_provider_event(PROVIDER_NBA_STATS, operation, CACHE_HIT)

    def fetch_player_game_logs(
        self,
        player_id: int,
        season: str,
        *,
        cache_status: str = CACHE_MISS,
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
            "player_game_logs", build, cache_status=cache_status
        )

    def fetch_player_next_game(
        self,
        player_id: int,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame | None:
        """Fetch the player's next scheduled game, if one exists."""

        def build(timeout: float) -> object:
            return endpoints.PlayerNextNGames(
                number_of_games=1,
                player_id=player_id,
                timeout=timeout,
            )

        return self.run_endpoint(
            "player_next_game", build, cache_status=cache_status
        )

    def fetch_opponent_team_stats(
        self,
        date_from: str | None,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame | None:
        """Fetch league opponent team stats from the cutoff ``date_from``."""

        def build(timeout: float) -> object:
            return endpoints.LeagueDashTeamStats(
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="Per48",
                date_from_nullable=date_from,
                timeout=timeout,
            )

        return self.run_endpoint(
            "league_opponent_team_stats", build, cache_status=cache_status
        )

    def fetch_opponent_shot_chart(
        self,
        general_range: str,
        date_from: str | None,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame:
        """Fetch league opponent shot data (catch-and-shoot / pull-ups)."""

        def build(timeout: float) -> object:
            return endpoints.LeagueDashOppPtShot(
                general_range_nullable=general_range,
                date_from_nullable=date_from,
                timeout=timeout,
            )

        return self.run_endpoint(
            "league_opponent_shot_chart", build, cache_status=cache_status
        )


__all__ = ["NBAStatsAdapter", "parse_recorded_game_logs"]