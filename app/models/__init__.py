"""
SQLAlchemy models for NBA Game Logs application.

This module sets up the SQLAlchemy declarative base and provides
the foundation for ORM models.
"""

import logging

from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

# Create the declarative base for all models
Base = declarative_base()


def get_session(engine: Engine | None = None):
    """
    Create a new SQLAlchemy session.

    Returns:
        Session: A new SQLAlchemy session bound to the application engine
    """
    # Import database configuration lazily.  ``app.config.settings`` imports
    # lightweight model metadata, so importing it at module load time here
    # would make the migration CLI circular (settings -> catalogs -> models
    # -> db -> settings).
    if engine is None:
        from app.utils.db import get_engine

        session_engine = get_engine()
    else:
        session_engine = engine
    Session = sessionmaker(bind=session_engine)
    return Session()


def create_all_tables():
    """
    Apply the current application schema migrations.

    The function name is retained for the app-factory compatibility seam;
    callers that need a repeatable workflow should use ``scripts/migrate.py``.
    """
    from app.config.settings import get_runtime_settings
    from app.utils.db import get_engine, is_demo_database_url

    settings = get_runtime_settings()
    if is_demo_database_url(settings.database.url):
        logger.info("Skipping application migrations for the read-only demo database")
        return

    engine = get_engine(settings)
    from app.migrations import run_migrations

    result = run_migrations(engine)
    logger.info(
        "Database tables are ready at schema version %s (applied: %s)",
        result.current_version,
        ", ".join(result.applied) or "none",
    )


# Import all models here to ensure they're registered with Base.metadata
from .app_setting import AppSetting  # noqa: E402
from .athlete_catalog import AthleteCatalog, AthleteCatalogFreshness  # noqa: E402
from .athlete_mapping import (  # noqa: E402
    AthleteMappingDecision,
    AthleteMappingDecisionCandidate,
    AthleteMappingDecisionContradiction,
    AthleteMappingLock,
    AthleteMappingRejection,
    ProviderAthleteMapping,
)
from .canonical_game_ledger import (  # noqa: E402
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerBackfillState,
    LedgerParityArtifact,
    LedgerPublication,
)
from .collection_control import (  # noqa: E402
    ActiveSeason,
    AuditEvent,
    BootstrapRequest,
    CatalogPublication,
    CollectionAlert,
    CollectionCycle,
    CollectionManifest,
    CollectionObservation,
    CollectorIdentity,
    CollectorLease,
    CollectorStatusTransition,
    CollectorTokenReplay,
    CollectorUsage,
    CompositionJob,
    CredentialDelivery,
    GovernedNotApplicable,
    OperatorJob,
    PublicationActivation,
    PublicationObservation,
    PublicationPointer,
    PublicationPointerHistory,
    PublicationRebuild,
    PublicationStream,
    PublicationVersion,
    ReconciliationItem,
    ValidationSummary,
)
from .event_catalog import EventCatalogEntry, EventCatalogRefresh  # noqa: E402
from .event_mapping import (  # noqa: E402
    EventMappingDecision,
    EventMappingDecisionCandidate,
    EventMappingDecisionContradiction,
    EventMappingLock,
    EventMappingRejection,
    ProviderEventMapping,
)
from .injury_snapshot import InjurySnapshot, InjurySourceSnapshot  # noqa: E402
from .job import DataRefreshJob  # noqa: E402
from .player_game_log import (  # noqa: E402
    PlayerGameLog,
    PlayerGameLogRefresh,
    PlayerGameLogSync,
    PublicationPlayerGameLog,
)
from .player_pool_snapshot import PlayerPoolSnapshot  # noqa: E402
from .player_season_stats import PlayerSeasonStats  # noqa: E402
from .player_tracking import PlayerGameTracking  # noqa: E402
from .projection_archive import (  # noqa: E402
    ClosingProjectionMembership,
    ClosingProjectionSet,
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProjectionRetentionRun,
    ProviderPoll,
)
from .projection_collection import (  # noqa: E402
    ProjectionCollectionLease,
    ProjectionCollectionProviderState,
)
from .report import Report  # noqa: E402
from .research_activity import AccountCohort, ResearchActivityDay  # noqa: E402
from .rotation import RotationAttempt, RotationGame, RotationPlayerGame, RotationStint  # noqa: E402
from .saved_filter_set import SavedFilterSet  # noqa: E402
from .stats_freshness import StatsRefresh  # noqa: E402
from .target import Target, TargetQualifier  # noqa: E402
from .team_matchup import (  # noqa: E402
    TeamMatchupFactRow,
    TeamMatchupSurfaceObservationRow,
)
from .usage_event import UsageEvent  # noqa: E402
from .user import User  # noqa: E402
from .user_attribution import UserAttribution  # noqa: E402

__all__ = [
    "AccountCohort",
    "ActiveSeason",
    "AppSetting",
    "AthleteCatalog",
    "AthleteCatalogFreshness",
    "AthleteMappingDecision",
    "AthleteMappingDecisionCandidate",
    "AthleteMappingDecisionContradiction",
    "AthleteMappingLock",
    "AthleteMappingRejection",
    "AuditEvent",
    "Base",
    "BootstrapRequest",
    "CanonicalGameLedgerGame",
    "CanonicalGameLedgerPlayerFact",
    "CanonicalGameLedgerTeamFact",
    "CatalogPublication",
    "ClosingProjectionMembership",
    "ClosingProjectionSet",
    "CollectionAlert",
    "CollectionCycle",
    "CollectionManifest",
    "CollectionObservation",
    "CollectorIdentity",
    "CollectorLease",
    "CollectorStatusTransition",
    "CollectorTokenReplay",
    "CollectorUsage",
    "CompositionJob",
    "CredentialDelivery",
    "DataRefreshJob",
    "EventCatalogEntry",
    "EventCatalogRefresh",
    "EventMappingDecision",
    "EventMappingDecisionCandidate",
    "EventMappingDecisionContradiction",
    "EventMappingLock",
    "EventMappingRejection",
    "GovernedNotApplicable",
    "InjurySnapshot",
    "InjurySourceSnapshot",
    "LatestPlayerProjection",
    "LedgerBackfillState",
    "LedgerParityArtifact",
    "LedgerPublication",
    "OperatorJob",
    "PlayerGameLog",
    "PlayerGameLogRefresh",
    "PlayerGameLogSync",
    "PlayerGameTracking",
    "PlayerPoolSnapshot",
    "PlayerSeasonStats",
    "ProjectionArchiveScopeLock",
    "ProjectionCollectionLease",
    "ProjectionCollectionProviderState",
    "ProjectionMaterializationGeneration",
    "ProjectionObservation",
    "ProjectionProviderSnapshot",
    "ProjectionRetentionRun",
    "ProviderAthleteMapping",
    "ProviderEventMapping",
    "ProviderPoll",
    "PublicationActivation",
    "PublicationObservation",
    "PublicationPlayerGameLog",
    "PublicationPointer",
    "PublicationPointerHistory",
    "PublicationRebuild",
    "PublicationStream",
    "PublicationVersion",
    "ReconciliationItem",
    "Report",
    "ResearchActivityDay",
    "RotationAttempt",
    "RotationGame",
    "RotationPlayerGame",
    "RotationStint",
    "SavedFilterSet",
    "StatsRefresh",
    "Target",
    "TargetQualifier",
    "TeamMatchupFactRow",
    "TeamMatchupSurfaceObservationRow",
    "UsageEvent",
    "User",
    "UserAttribution",
    "ValidationSummary",
    "create_all_tables",
    "get_session",
]
