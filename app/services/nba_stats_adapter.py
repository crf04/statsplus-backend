"""Synchronous, concurrency-bounded adapter for the NBA Stats provider.

Game-log handling runs as a synchronous Flask + threaded workload: requests are
served by Gunicorn worker threads and provider calls execute on the calling
thread through the blocking ``nba_api`` package.  This adapter is that seam.

Every ``stats.nba.com`` call is wrapped in one explicit bound, a
``threading.BoundedSemaphore`` sized by ``NBA_STATS_MAX_CONCURRENCY``
(``ProviderSettings.nba_stats_max_concurrency``), so the number of in-flight
provider calls can never exceed the configured limit regardless of how many
request threads arrive.  Providing one adapter shared by a service instance
keeps the bound unambiguous.

Each invocation also records one structured provider-telemetry event via
:mod:`app.utils.telemetry`.  The per-call ``timeout`` comes from
``ProviderSettings.nba_stats_timeout_seconds``; a provider timeout raises
``requests.exceptions.Timeout`` which the route maps to the documented 503
``provider_unavailable`` response.

Callers that resolve the game-log cache can pass the matching ``cache_status``
(``CACHE_HIT``/``CACHE_MISS``/``CACHE_DISABLED``) so every event reflects the
cache behaviour behind the call;  responses served entirely from cache are
recorded through :meth:`NBAStatsAdapter.record_cache_hit` without touching the
provider.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import pandas as pd
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

    def _run(
        self,
        operation: str,
        fetch: Callable[[], object],
        *,
        cache_status: str = CACHE_MISS,
    ) -> object:
        """Execute one blocking provider call under the bound and telemetry."""
        with self._bound:
            with provider_call(
                PROVIDER_NBA_STATS, operation, cache_status=cache_status
            ):
                return fetch()

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

        def fetch() -> pd.DataFrame:
            return endpoints.playergamelogs.PlayerGameLogs(
                player_id_nullable=player_id,
                season_nullable=season,
                season_type_nullable="Regular Season",
                timeout=self.timeout,
            ).get_data_frames()[0]

        return self._run("player_game_logs", fetch, cache_status=cache_status)

    def fetch_player_next_game(
        self,
        player_id: int,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame | None:
        """Fetch the player's next scheduled game, if one exists."""

        def fetch() -> pd.DataFrame | None:
            frames = endpoints.PlayerNextNGames(
                number_of_games=1,
                player_id=player_id,
                timeout=self.timeout,
            ).get_data_frames()
            return frames[0] if frames else None

        return self._run("player_next_game", fetch, cache_status=cache_status)

    def fetch_opponent_team_stats(
        self,
        date_from: str | None,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame | None:
        """Fetch league opponent team stats from the cutoff ``date_from``."""

        def fetch() -> pd.DataFrame | None:
            return endpoints.LeagueDashTeamStats(
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="Per48",
                date_from_nullable=date_from,
                timeout=self.timeout,
            ).get_data_frames()[0]

        return self._run(
            "league_opponent_team_stats", fetch, cache_status=cache_status
        )

    def fetch_opponent_shot_chart(
        self,
        general_range: str,
        date_from: str | None,
        *,
        cache_status: str = CACHE_MISS,
    ) -> pd.DataFrame:
        """Fetch league opponent shot data (catch-and-shoot / pull-ups)."""

        def fetch() -> pd.DataFrame:
            return endpoints.LeagueDashOppPtShot(
                general_range_nullable=general_range,
                date_from_nullable=date_from,
                timeout=self.timeout,
            ).get_data_frames()[0]

        return self._run(
            "league_opponent_shot_chart", fetch, cache_status=cache_status
        )


__all__ = ["NBAStatsAdapter", "parse_recorded_game_logs"]