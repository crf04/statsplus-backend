"""Application dependency assembly and request-time access."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings
from app.dfs_catalog import DFS_DABBLE, DFS_PRIZEPICKS, DFS_UNDERDOG


@dataclass(frozen=True, slots=True)
class ApplicationDependencies:
    """Runtime objects constructed once for one Flask application."""

    settings: RuntimeSettings
    engine: Engine
    redis_client: Any
    nba_stats_provider: Any
    pbp_stats_provider: Any
    dfs_providers: dict[str, Any]
    dfs_board_service: Any
    game_service: Any
    slate_service: Any
    player_service: Any
    team_service: Any
    data_service: Any
    data_refresh_jobs_service: Any
    athlete_catalog_service: Any | None
    provider_health_service: Any
    nl_service: Any
    user_service: Any
    dfs_snapshot_cache: Any | None = None
    statistic_catalog: Any | None = None
    comparison_board_service: Any | None = None
    dfs_board_response_service: Any | None = None
    athlete_mapping_repository: Any | None = None
    athlete_resolver: Any | None = None
    event_catalog_service: Any | None = None
    event_mapping_repository: Any | None = None
    event_resolver: Any | None = None
    team_matchup_repository: Any | None = None
    team_matchup_query_service: Any | None = None
    team_matchup_refresh_service: Any | None = None


def build_dependencies(
    settings: RuntimeSettings,
    *,
    statistic_catalog_path: str | Path | None = None,
    statistic_catalog_loader: Callable[[str | Path], Any] | None = None,
) -> ApplicationDependencies:
    """Construct the complete request dependency graph for one application."""

    from app.providers.dabble import DabbleAdapter
    from app.providers.nba_stats import NBAStatsAdapter
    from app.providers.pbp_stats import PBPStatsAdapter
    from app.providers.prizepicks import PrizePicksAdapter
    from app.providers.underdog import UnderdogAdapter
    from app.services.athlete_catalog_service import AthleteCatalogService
    from app.services.comparison_board import ComparisonBoardService
    from app.services.data_service import DataService
    from app.services.stats_freshness_repository import StatsFreshnessRepository
    from app.services.dfs_board import DFSBoardService
    from app.services.dfs_board_response import DFSBoardResponseService
    from app.services.dfs_snapshot_cache import (
        ProviderSnapshotCache,
        ProviderSnapshotCacheCoordinator,
    )
    from app.services.game_service import GameService
    from app.services.job_service import build_data_refresh_job_service
    from app.services.nl_service import NLService
    from app.services.player_service import PlayerService
    from app.services.player_pool import PlayerPoolService
    from app.services.player_pool_snapshot_repository import PlayerPoolSnapshotRepository
    from app.services.provider_health_service import ProviderHealthService
    from app.services.slate_service import SlateService
    from app.services.statistic_catalog import StatisticCatalog
    from app.services.team_service import TeamService
    from app.services.user_service import UserService
    from app.utils.cache_config import get_redis_client
    from app.utils.db import get_engine

    # Load the reviewed statistic definitions before constructing providers.
    # This keeps schema failures at the app-factory boundary and avoids any
    # route-import or request-time catalog side effects.
    if statistic_catalog_loader is not None and statistic_catalog_path is None:
        statistic_catalog = statistic_catalog_loader(StatisticCatalog.DEFAULT_PATH)
    elif statistic_catalog_loader is not None:
        statistic_catalog = statistic_catalog_loader(statistic_catalog_path)
    elif statistic_catalog_path is not None:
        statistic_catalog = StatisticCatalog.load(statistic_catalog_path)
    else:
        statistic_catalog = StatisticCatalog.load_default()
    if not isinstance(statistic_catalog, StatisticCatalog):
        raise TypeError("statistic catalog loader must return a StatisticCatalog")

    engine = get_engine(settings)
    redis_client = get_redis_client(settings) if settings.cache.enabled else None
    nba_stats_provider = NBAStatsAdapter(settings=settings)
    pbp_stats_provider = PBPStatsAdapter(settings=settings)

    dfs_providers: dict[str, Any] = {}
    cached_dfs_providers: dict[str, Any] = {}
    dfs_snapshot_cache = ProviderSnapshotCacheCoordinator()
    dfs_timeout = (
        settings.providers.dfs_provider_connect_timeout_seconds,
        settings.providers.dfs_provider_read_timeout_seconds,
    )
    for provider_name in settings.providers.dfs_enabled_providers:
        if provider_name == DFS_DABBLE:
            provider = DabbleAdapter(
                connect_timeout_seconds=dfs_timeout[0],
                read_timeout_seconds=dfs_timeout[1],
                detail_concurrency=settings.providers.dfs_dabble_detail_concurrency,
            )
        elif provider_name == DFS_PRIZEPICKS:
            provider = PrizePicksAdapter(timeout=dfs_timeout)
        elif provider_name == DFS_UNDERDOG:
            provider = UnderdogAdapter(timeout=dfs_timeout)
        else:  # settings validation normally makes this unreachable
            raise ValueError(f"unsupported DFS provider {provider_name}")
        dfs_providers[provider_name] = provider
        cached_dfs_providers[provider_name] = ProviderSnapshotCache(
            provider,
            provider_name=provider_name,
            redis_client=redis_client,
            enabled=settings.cache.enabled and redis_client is not None,
            fresh_seconds=settings.providers.dfs_cache_fresh_seconds_for(provider_name),
            stale_if_error_seconds=settings.providers.dfs_cache_stale_if_error_seconds_for(
                provider_name
            ),
            coordinator=dfs_snapshot_cache,
        )

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
    stats_freshness_repository = StatsFreshnessRepository(engine)
    data_service = DataService(
        engine,
        settings=settings,
        pbp_provider=pbp_stats_provider,
        nba_stats_provider=nba_stats_provider,
        stats_freshness=stats_freshness_repository,
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
    athlete_catalog_service = None
    athlete_mapping_repository = None
    athlete_resolver = None
    event_catalog_service = None
    event_mapping_repository = None
    event_resolver = None
    team_matchup_repository = None
    team_matchup_query_service = None
    team_matchup_refresh_service = None
    from app.utils.db import is_demo_database_url

    if not is_demo_database_url(settings.database.url):
        athlete_catalog_service = AthleteCatalogService(
            engine,
            settings=settings,
            nba_stats_provider=nba_stats_provider,
        )
        from app.services.athlete_mapping_repository import AthleteMappingRepository
        from app.services.athlete_resolver import AthleteResolver
        from app.services.event_catalog_service import EventCatalogService
        from app.services.event_mapping_repository import EventMappingRepository
        from app.services.event_resolver import EventResolver

        athlete_mapping_repository = AthleteMappingRepository(engine)
        athlete_resolver = AthleteResolver(
            athlete_catalog_service,
            mapping_repository=athlete_mapping_repository,
        )
        event_catalog_service = EventCatalogService(
            engine,
            settings=settings,
            nba_stats_provider=nba_stats_provider,
        )
        # The repository rechecks a governed decision inside its own
        # transaction, so it is composed with the same configured match window
        # the resolver applies outside it.
        event_mapping_repository = EventMappingRepository(engine, settings=settings)
        event_resolver = EventResolver(
            event_catalog_service,
            mapping_repository=event_mapping_repository,
            settings=settings,
        )
        from app.services.team_matchup_query import TeamMatchupQueryService
        from app.services.team_matchup_refresh import TeamMatchupRefreshService
        from app.services.team_matchup_repository import TeamMatchupRepository

        team_matchup_repository = TeamMatchupRepository(engine)
        team_matchup_query_service = TeamMatchupQueryService(
            team_matchup_repository
        )
        team_matchup_refresh_service = TeamMatchupRefreshService(
            team_matchup_repository,
            event_catalog_service,
            nba_stats_provider,
            pbp_stats_provider,
        )

    dfs_board_service = DFSBoardService(
        provider_registry=cached_dfs_providers,
        max_concurrency=3,
        deadline_seconds=settings.providers.dfs_board_deadline_seconds,
        settings=settings,
        statistic_catalog=statistic_catalog,
        athlete_resolver=athlete_resolver,
        athlete_mapping_repository=athlete_mapping_repository,
        event_resolver=event_resolver,
        event_mapping_repository=event_mapping_repository,
    )
    comparison_board_service = ComparisonBoardService(
        dfs_board_service,
        athlete_catalog=athlete_catalog_service,
        event_catalog=event_catalog_service,
        settings=settings,
    )
    # The published board is assembled from the comparison board alone; it owns
    # no client of its own, so composing it here creates no new connection.
    dfs_board_response_service = DFSBoardResponseService(
        comparison_board_service,
        settings=settings,
    )
    player_pool_snapshot_repository = (
        None if is_demo_database_url(settings.database.url)
        else PlayerPoolSnapshotRepository(engine)
    )
    player_pool_service = PlayerPoolService(
        dfs_board_service,
        statistic_catalog,
        snapshot_repository=player_pool_snapshot_repository,
    )
    slate_service = SlateService(
        event_catalog_service,
        settings=settings,
        player_pool=player_pool_service,
    )

    return ApplicationDependencies(
        settings=settings,
        engine=engine,
        redis_client=redis_client,
        nba_stats_provider=nba_stats_provider,
        pbp_stats_provider=pbp_stats_provider,
        dfs_providers=dfs_providers,
        dfs_board_service=dfs_board_service,
        game_service=game_service,
        slate_service=slate_service,
        player_service=player_service,
        team_service=team_service,
        data_service=data_service,
        data_refresh_jobs_service=data_refresh_jobs_service,
        athlete_catalog_service=athlete_catalog_service,
        athlete_mapping_repository=athlete_mapping_repository,
        athlete_resolver=athlete_resolver,
        event_catalog_service=event_catalog_service,
        event_mapping_repository=event_mapping_repository,
        event_resolver=event_resolver,
        team_matchup_repository=team_matchup_repository,
        team_matchup_query_service=team_matchup_query_service,
        team_matchup_refresh_service=team_matchup_refresh_service,
        provider_health_service=provider_health_service,
        nl_service=NLService(engine, settings=settings),
        user_service=UserService(engine, settings=settings),
        dfs_snapshot_cache=dfs_snapshot_cache,
        statistic_catalog=statistic_catalog,
        comparison_board_service=comparison_board_service,
        dfs_board_response_service=dfs_board_response_service,
    )


def get_dependencies() -> ApplicationDependencies:
    """Return dependencies belonging to the active Flask application."""

    dependencies = current_app.extensions.get("dependencies")
    if dependencies is None:
        raise RuntimeError("Application dependencies have not been initialized")
    return dependencies


__all__ = ["ApplicationDependencies", "build_dependencies", "get_dependencies"]
