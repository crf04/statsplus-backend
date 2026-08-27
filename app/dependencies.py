"""Application dependency assembly and request-time access."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.config.settings import ConfigurationError, RuntimeSettings
from app.providers.registry import (
    DFSProviderRuntime,
    ProviderRegistryError,
    build_dfs_provider,
    dfs_provider_names,
)


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
    matchup_service: Any
    matchup_selection_service: Any
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
    player_diet_service: Any | None = None
    pbp_game_logs_provider: Any | None = None
    nba_live_data_provider: Any | None = None
    game_logs_source: Any | None = None
    collector_tokens: Any | None = None
    collection_control: Any | None = None
    observation_ingestion: Any | None = None
    publication_service: Any | None = None
    collection_operations: Any | None = None
    canonical_game_ledger_repository: Any | None = None
    ledger_materialization_service: Any | None = None
    ledger_backfill_service: Any | None = None
    ledger_matchup_materialization_service: Any | None = None
    publication_reader: Any | None = None
    projection_archive: Any | None = None
    projection_recorder: Any | None = None
    projection_player_pool_reader: Any | None = None
    projection_collection_coordinator: Any | None = None


def build_dependencies(
    settings: RuntimeSettings,
    *,
    statistic_catalog_path: str | Path | None = None,
    statistic_catalog_loader: Callable[[str | Path], Any] | None = None,
) -> ApplicationDependencies:
    """Construct the complete request dependency graph for one application."""

    from app.providers.nba_stats import NBAStatsAdapter
    from app.providers.nba_live_data import NBALiveDataBoxscoreAdapter
    from app.providers.pbp_game_logs import PBPGameLogAdapter
    from app.providers.pbp_stats import PBPStatsAdapter
    from app.providers.rotowire import RotoWireInjuryProvider
    from app.providers.dfs import NBAMarketQuery
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
    from app.services.player_diet import PlayerDietService
    from app.services.projection_archive import (
        LatestProjectionPlayerPoolReader,
        ProjectionArchive,
        ProjectionArchiveReadScope,
        ProjectionRecordingService,
        ProjectionSelectionPlayerPoolReader,
        require_projection_archive_schema,
    )
    from app.services.player_archetype_repository import PlayerArchetypeRepository
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from app.services.matchup import MatchupService
    from app.services.matchup_injuries import MatchupInjuryService
    from app.services.injury_snapshot_repository import InjurySnapshotRepository
    from app.services.matchup_selection import MatchupSelectionService
    from app.services.provider_health_service import ProviderHealthService
    from app.services.slate_service import SlateService
    from app.services.statistic_catalog import StatisticCatalog
    from app.services.team_service import TeamService
    from app.services.team_matchup_query import TeamMatchupQueryService
    from app.services.team_matchup_repository import TeamMatchupRepository
    from app.services.user_service import UserService
    from app.utils.cache_config import get_redis_client
    from app.utils.db import get_engine, is_demo_database_url
    from app.services.collection_control import (
        CollectorTokenService,
        CollectionControlService,
        ObservationIngestionService,
        PublicationService,
        CollectionOperationsService,
        EmailAlertAdapter,
    )

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
    demo_database = is_demo_database_url(settings.database.url)
    if demo_database and settings.features.projection_archive_read_enabled:
        raise ConfigurationError(
            "PROJECTION_ARCHIVE_READ_ENABLED cannot use the read-only demo database"
        )
    if settings.features.projection_archive_read_enabled:
        require_projection_archive_schema(engine)
    collector_tokens = collection_control = observation_ingestion = publication_service = collection_operations = None
    publication_reader = None
    write_fence = None
    l15_expectation_resolver = None
    publication_write_capability = None
    canonical_game_ledger_repository = ledger_materialization_service = ledger_backfill_service = None
    ledger_matchup_materialization_service = None
    if not demo_database:
        # The signing secret is deployment-only.  A process-local key keeps
        # local development credential-free; production should inject one.
        signing_secret = settings.auth.collector_signing_secret
        collector_tokens = CollectorTokenService(
            engine, environment=settings.environment, signing_secret=signing_secret
        )
        collection_control = CollectionControlService(
            engine, environment=settings.environment
        )
        from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
        l15_expectation_resolver = ActiveManifestLedgerGovernanceReader(engine)
        publication_service = PublicationService(
            engine,
            l15_expectation_resolver=l15_expectation_resolver,
        )
        from app.services.database_first_activation import DatabaseFirstPublicationReader
        from app.services.database_first_activation import LegacyWriteFence

        publication_reader = DatabaseFirstPublicationReader(engine)
        write_fence = LegacyWriteFence(engine)
        collection_operations = CollectionOperationsService(
            engine,
            publication_service=publication_service,
            collection_control=collection_control,
            collector_tokens=collector_tokens,
            alert_adapter=EmailAlertAdapter(),
            l15_expectation_resolver=l15_expectation_resolver,
        )
        publication_write_capability = (
            publication_service.governed_publication_write_capability()
        )
        if inspect(engine).has_table("publication_streams"):
            publication_service.register_default_streams()
        observation_ingestion = ObservationIngestionService(
            engine,
            publication_service=publication_service,
            collection_control=collection_control,
            min_event_catalog_games=collection_control.min_event_catalog_games,
            min_event_catalog_teams=collection_control.min_event_catalog_teams,
            min_athlete_catalog_identities=collection_control.min_athlete_catalog_identities,
        )
    injury_snapshot_repository = (
        None if demo_database else InjurySnapshotRepository(engine)
    )
    redis_client = get_redis_client(settings) if settings.cache.enabled else None
    nba_stats_provider = NBAStatsAdapter(settings=settings)
    pbp_stats_provider = PBPStatsAdapter(settings=settings)
    pbp_game_logs_provider = PBPGameLogAdapter(settings=settings)
    nba_live_data_provider = NBALiveDataBoxscoreAdapter(settings=settings)
    injury_provider = (
        RotoWireInjuryProvider(settings=settings)
        if settings.features.injury_report_enabled
        and settings.providers.rotowire_permission_granted
        and injury_snapshot_repository is not None
        else None
    )

    dfs_providers: dict[str, Any] = {}
    cached_dfs_providers: dict[str, Any] = {}
    dfs_snapshot_cache = ProviderSnapshotCacheCoordinator()
    dfs_runtime = DFSProviderRuntime(
        connect_timeout_seconds=settings.providers.dfs_provider_connect_timeout_seconds,
        read_timeout_seconds=settings.providers.dfs_provider_read_timeout_seconds,
        detail_concurrency=settings.providers.dfs_dabble_detail_concurrency,
    )
    for provider_name in settings.providers.dfs_enabled_providers:
        # The registry is the only authority on which providers exist and how
        # they are built, so a name it does not admit -- or a registration whose
        # adapter is not the shared snapshot seam -- stops this process here
        # rather than failing at the first collection run.
        try:
            provider = build_dfs_provider(provider_name, dfs_runtime)
        except ProviderRegistryError as error:
            raise ConfigurationError(str(error)) from error
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

    game_service = None
    player_service = PlayerService(
        engine,
        settings=settings,
        nba_stats_provider=nba_stats_provider,
        publication_reader=publication_reader,
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
        write_fence=write_fence,
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
    player_diet_service = None
    team_matchup_query_service = None
    if not demo_database:
        athlete_catalog_service = AthleteCatalogService(
            engine,
            settings=settings,
            nba_stats_provider=nba_stats_provider,
            write_fence=write_fence,
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
            write_fence=write_fence,
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
        player_diet_service = PlayerDietService(
            engine,
            athlete_catalog=athlete_catalog_service,
            nba_stats_provider=nba_stats_provider,
            pbp_stats_provider=pbp_stats_provider,
            write_fence=write_fence,
            publication_reader=publication_reader,
        )
        team_matchup_repository = TeamMatchupRepository(
            engine,
            write_fence=write_fence,
            publication_write_capability=publication_write_capability,
        )
        team_matchup_query_service = TeamMatchupQueryService(
            team_matchup_repository,
            publication_reader=publication_reader,
            l15_expectation_resolver=l15_expectation_resolver,
        )
        from app.services.canonical_game_ledger import (
            CanonicalGameLedgerRepository,
            LedgerSchemaUnavailable,
            record_schema_drift,
        )
        from app.services.ledger_backfill import (
            AcceptedObservationParticipantCatalog,
            CollectionObservationLedgerRecorder,
            LedgerBackfillService,
        )
        from app.services.ledger_materialization import (
            LedgerCorrectionQueue,
            LedgerMaterializationService,
        )
        from app.services.ledger_parity import (
            LedgerParityArtifactRepository,
            LegacyParityDiagnosticReader,
        )
        from app.services.ledger_matchup_materialization import (
            LedgerMatchupMaterializationService,
        )

        try:
            canonical_game_ledger_repository = CanonicalGameLedgerRepository(
                engine,
                correction_sink=LedgerCorrectionQueue(require_governance=True),
                schema_drift_sink=record_schema_drift,
            )
        except LedgerSchemaUnavailable:
            # Narrow route tests intentionally bind the app to minimal fixture
            # databases while disabling schema creation.  Ledger workers are
            # not part of those request seams; production/staging must still
            # fail startup when migration 032 has not been applied.
            if settings.environment != "testing":
                raise
        else:
            ledger_materialization_service = LedgerMaterializationService(
                canonical_game_ledger_repository,
                parity_repository=LedgerParityArtifactRepository(engine),
                parity_reader=LegacyParityDiagnosticReader(engine),
                publication_service=publication_service,
            )
            ledger_matchup_materialization_service = (
                LedgerMatchupMaterializationService(
                    canonical_game_ledger_repository,
                    team_matchup_repository,
                    publication_reader=publication_reader,
                    l15_expectation_resolver=l15_expectation_resolver,
                )
            )
            ledger_observation_recorder = CollectionObservationLedgerRecorder(engine)
            ledger_backfill_service = LedgerBackfillService(
                provider=pbp_game_logs_provider,
                fallback_provider=nba_live_data_provider,
                athlete_catalog=athlete_catalog_service,
                participant_catalog=AcceptedObservationParticipantCatalog(
                    engine, ledger_observation_recorder
                ),
                observation_recorder=ledger_observation_recorder,
                reconciliation_sink=lambda game_id, details: collection_control.record_identity_unresolved(
                    season=str(details.get("season", settings.nba.current_season)),
                    kind="ledger_athlete",
                    details={"game_id": game_id, **details},
                ),
                repository=canonical_game_ledger_repository,
                max_concurrency=1,
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
    matchup_injury_service = MatchupInjuryService(
        provider=injury_provider,
        snapshot_repository=injury_snapshot_repository,
        athlete_catalog=athlete_catalog_service,
        enabled=settings.features.injury_report_enabled,
        permission_granted=settings.providers.rotowire_permission_granted,
    )
    projection_archive = (
        None
        if demo_database
        else ProjectionArchive(
            engine,
            statistic_catalog,
            max_markets=settings.providers.projection_archive_max_markets,
        )
    )
    if projection_archive is not None:
        if athlete_mapping_repository is not None:
            athlete_mapping_repository.set_projection_replay(
                projection_archive.replay_athlete_decision
            )
        if event_mapping_repository is not None:
            event_mapping_repository.set_projection_replay(
                projection_archive.replay_event_decision
            )
    projection_query = NBAMarketQuery(season=settings.nba.current_season)
    projection_read_scopes = tuple(
        ProjectionArchiveReadScope(
            provider=provider,
            query=projection_query,
        )
        for provider in dfs_provider_names()
    )
    projection_read_scopes_by_provider = {
        scope.provider: scope for scope in projection_read_scopes
    }
    projection_record_scopes = tuple(
        projection_read_scopes_by_provider[provider]
        for provider in settings.providers.dfs_enabled_providers
    )
    projection_record_default_scope = projection_read_scopes_by_provider[
        settings.features.projection_archive_read_provider
    ]
    projection_recorder = (
        None
        if projection_archive is None
        else ProjectionRecordingService(
            projection_archive,
            projection_record_scopes,
            default_scope=projection_record_default_scope,
        )
    )
    projection_player_pool_reader = (
        LatestProjectionPlayerPoolReader(
            engine,
            projection_read_scopes,
            required_providers=settings.providers.dfs_enabled_providers,
            event_reader=event_catalog_service,
        )
        if settings.features.projection_archive_read_enabled
        else None
    )
    projection_collection_coordinator = None
    if projection_recorder is not None:
        from app.services.projection_collection import ProjectionCollectionCoordinator

        projection_collection_coordinator = ProjectionCollectionCoordinator(
            engine,
            board_service=dfs_board_service,
            recording_service=projection_recorder,
            event_reader=(
                event_catalog_service.get_events
                if event_catalog_service is not None
                else None
            ),
            season=settings.nba.current_season,
            settings=settings.projection_collection,
            providers=settings.providers.dfs_enabled_providers,
        )
        if collection_operations is not None:
            collection_operations.set_projection_collection(
                projection_collection_coordinator
            )
    # The database-only projection reader is the sole Slate/Matchup/Selection
    # source after the #110 cutover.  There is no request-time legacy fallback:
    # when the archive read gate is off the pool is simply absent, never a
    # provider-driven build.
    slate_player_pool = projection_player_pool_reader
    slate_service = SlateService(
        event_catalog_service,
        settings=settings,
        player_pool=slate_player_pool,
        injuries=matchup_injury_service,
    )
    from app.domain.freshness import time_window_timedelta

    player_game_log_repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=statistic_catalog,
        stats_surface_season=settings.nba.current_season,
        stats_surface_max_age=time_window_timedelta(
            settings.catalog.player_game_log_max_age_hours,
            unit_seconds=3600,
            field="PLAYER_GAME_LOG_MAX_AGE_HOURS",
        ),
        write_fence=write_fence,
        serve_stale=not demo_database,
        publication_reader=publication_reader,
    )
    from app.services.game_logs_source import (
        DatabaseFirstGameLogsSource,
        LivePBPGameLogsSource,
        StoredGameLogsSource,
    )

    live_game_logs_source = LivePBPGameLogsSource(
        pbp_game_logs_provider,
        event_catalog_service,
    )
    stored_game_logs_source = StoredGameLogsSource(
        player_game_log_repository,
    )
    game_logs_source = DatabaseFirstGameLogsSource(
        live_game_logs_source,
        stored_game_logs_source,
        player_game_log_repository,
    )
    from app.services.team_filter_rankings import (
        PlayerDietReader,
        TeamFilterRankingService,
    )

    game_service = GameService(
        engine,
        redis_client=redis_client,
        settings=settings,
        game_logs_source=game_logs_source,
        # Read-only Diet access over the service's own repository: the full
        # service owns a provider-backed refresh, so injecting it -- or a
        # wrapper around it -- would leave the adapters reachable from here.
        player_diets=(
            PlayerDietReader(player_diet_service.repository)
            if player_diet_service is not None
            else None
        ),
        team_matchups=team_matchup_query_service,
        team_filter_rankings=(
            TeamFilterRankingService(
                publication_reader,
                governance_resolver=l15_expectation_resolver,
            )
            if publication_reader is not None
            else None
        ),
    )
    matchup_player_pool_reader = projection_player_pool_reader
    selection_player_pool_reader = (
        ProjectionSelectionPlayerPoolReader(projection_player_pool_reader)
        if projection_player_pool_reader is not None
        else None
    )
    matchup_selection_service = MatchupSelectionService(
        event_catalog=event_catalog_service,
        player_pool=selection_player_pool_reader,
        player_logs=player_game_log_repository,
        archetypes=PlayerArchetypeRepository(engine),
        statistic_catalog=statistic_catalog,
        settings=settings,
        publication_reader=publication_reader,
        engine=engine,
    )
    matchup_service = MatchupService(
        event_catalog=event_catalog_service,
        player_pool=matchup_player_pool_reader,
        player_logs=player_game_log_repository,
        player_diets=player_diet_service,
        team_matchups=team_matchup_query_service,
        stats_freshness=stats_freshness_repository,
        settings=settings,
        injuries=matchup_injury_service,
        database_only=not demo_database,
        publication_reader=publication_reader,
        engine=engine,
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
        matchup_service=matchup_service,
        matchup_selection_service=matchup_selection_service,
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
        provider_health_service=provider_health_service,
        nl_service=NLService(engine, settings=settings),
        user_service=UserService(engine, settings=settings),
        dfs_snapshot_cache=dfs_snapshot_cache,
        statistic_catalog=statistic_catalog,
        comparison_board_service=comparison_board_service,
        dfs_board_response_service=dfs_board_response_service,
        player_diet_service=player_diet_service,
        pbp_game_logs_provider=pbp_game_logs_provider,
        nba_live_data_provider=nba_live_data_provider,
        game_logs_source=game_logs_source,
        collector_tokens=collector_tokens,
        collection_control=collection_control,
        observation_ingestion=observation_ingestion,
        publication_service=publication_service,
        collection_operations=collection_operations,
        canonical_game_ledger_repository=canonical_game_ledger_repository,
        ledger_materialization_service=ledger_materialization_service,
        ledger_backfill_service=ledger_backfill_service,
        ledger_matchup_materialization_service=ledger_matchup_materialization_service,
        publication_reader=publication_reader,
        projection_archive=projection_archive,
        projection_recorder=projection_recorder,
        projection_player_pool_reader=projection_player_pool_reader,
        projection_collection_coordinator=projection_collection_coordinator,
    )


def get_dependencies() -> ApplicationDependencies:
    """Return dependencies belonging to the active Flask application."""

    dependencies = current_app.extensions.get("dependencies")
    if dependencies is None:
        raise RuntimeError("Application dependencies have not been initialized")
    return dependencies


__all__ = ["ApplicationDependencies", "build_dependencies", "get_dependencies"]
