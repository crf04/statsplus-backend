"""Application dependency assembly and request-time access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import current_app
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class ApplicationDependencies:
    """Runtime objects constructed once for one Flask application."""

    settings: RuntimeSettings
    engine: Engine
    redis_client: Any
    nba_stats_provider: Any
    pbp_stats_provider: Any
    dfs_line_providers: Any
    game_service: Any
    player_service: Any
    team_service: Any
    data_service: Any
    dfs_line_service: Any
    data_refresh_jobs_service: Any
    provider_health_service: Any
    nl_service: Any
    user_service: Any


def build_dependencies(settings: RuntimeSettings) -> ApplicationDependencies:
    """Construct the complete request dependency graph for one application."""

    from app.services.data_service import DataService
    from app.services.game_service import GameService
    from app.services.dfs_line_service import DFSLineService
    from app.services.nl_service import NLService
    from app.services.player_service import PlayerService
    from app.services.team_service import TeamService
    from app.services.user_service import UserService
    from app.services.job_service import build_data_refresh_job_service
    from app.services.provider_health_service import ProviderHealthService
    from app.providers.nba_stats import NBAStatsAdapter
    from app.providers.pbp_stats import PBPStatsAdapter
    from app.providers.dabble import DabbleAdapter
    from app.utils.cache_config import get_redis_client
    from app.utils.db import get_engine

    engine = get_engine(settings)
    redis_client = get_redis_client(settings) if settings.cache.enabled else None
    nba_stats_provider = NBAStatsAdapter(settings=settings)
    pbp_stats_provider = PBPStatsAdapter(settings=settings)
    dfs_line_providers = {"dabble": DabbleAdapter(settings=settings)}

    game_service = GameService(
        engine,
        redis_client=redis_client,
        settings=settings,
        nba_stats_adapter=nba_stats_provider,
    )
    player_service = PlayerService(
        engine,
        settings=settings,
        nba_stats_provider=nba_stats_provider,
    )
    team_service = TeamService(
        engine,
        settings=settings,
        nba_stats_provider=nba_stats_provider,
    )
    data_service = DataService(
        engine,
        settings=settings,
        pbp_provider=pbp_stats_provider,
        nba_stats_provider=nba_stats_provider,
    )
    dfs_line_service = DFSLineService(
        dfs_line_providers,
        max_fixtures_per_request=(
            settings.providers.dabble_max_fixtures_per_request
        ),
    )
    provider_health_service = ProviderHealthService(
        engine,
        settings=settings,
        nba_stats=nba_stats_provider,
        pbp_stats=pbp_stats_provider,
    )
    data_refresh_jobs_service = build_data_refresh_job_service(
        engine,
        settings,
        data_service=data_service,
        player_service=player_service,
    )

    return ApplicationDependencies(
        settings=settings,
        engine=engine,
        redis_client=redis_client,
        nba_stats_provider=nba_stats_provider,
        pbp_stats_provider=pbp_stats_provider,
        dfs_line_providers=dfs_line_providers,
        game_service=game_service,
        player_service=player_service,
        team_service=team_service,
        data_service=data_service,
        dfs_line_service=dfs_line_service,
        data_refresh_jobs_service=data_refresh_jobs_service,
        provider_health_service=provider_health_service,
        nl_service=NLService(engine, settings=settings),
        user_service=UserService(engine, settings=settings),
    )


def get_dependencies() -> ApplicationDependencies:
    """Return dependencies belonging to the active Flask application."""

    dependencies = current_app.extensions.get("dependencies")
    if dependencies is None:
        raise RuntimeError("Application dependencies have not been initialized")
    return dependencies


__all__ = ["ApplicationDependencies", "build_dependencies", "get_dependencies"]
