"""
SQLAlchemy models for NBA Game Logs application.

This module sets up the SQLAlchemy declarative base and provides
the foundation for ORM models.
"""

from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker
import logging

logger = logging.getLogger(__name__)

# Create the declarative base for all models
Base = declarative_base()

def get_session(engine: Engine | None = None):
    """
    Create a new SQLAlchemy session.
    
    Returns:
        Session: A new SQLAlchemy session bound to the application engine
    """
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
from .user import User  # noqa: E402
from .job import DataRefreshJob  # noqa: E402
from .athlete_catalog import AthleteCatalog, AthleteCatalogFreshness  # noqa: E402
from .event_catalog import EventCatalogEntry, EventCatalogRefresh  # noqa: E402
from .stats_freshness import StatsRefresh  # noqa: E402
from .athlete_mapping import (  # noqa: E402
    AthleteMappingDecision,
    AthleteMappingDecisionCandidate,
    AthleteMappingDecisionContradiction,
    AthleteMappingLock,
    AthleteMappingRejection,
    ProviderAthleteMapping,
)
from .event_mapping import (  # noqa: E402
    EventMappingDecision,
    EventMappingDecisionCandidate,
    EventMappingDecisionContradiction,
    EventMappingLock,
    EventMappingRejection,
    ProviderEventMapping,
)
from .player_pool_snapshot import PlayerPoolSnapshot  # noqa: E402
from .player_game_log import (  # noqa: E402
    PlayerGameLog,
    PlayerGameLogRefresh,
    PlayerGameLogSync,
)
from .team_matchup import (  # noqa: E402
    TeamMatchupFactRow,
    TeamMatchupSurfaceObservationRow,
)
from .injury_snapshot import InjurySnapshot, InjurySourceSnapshot  # noqa: E402
from .collection_control import (  # noqa: E402
    ActiveSeason,
    BootstrapRequest,
    CatalogPublication,
    CollectionManifest,
    CollectorIdentity,
    CollectorStatusTransition,
    CollectionObservation,
    PublicationStream,
    PublicationVersion,
    PublicationObservation,
    PublicationPointer,
    CompositionJob,
    CollectorTokenReplay,
    CollectorLease,
    CollectionCycle,
    AuditEvent,
    ReconciliationItem,
    CollectionAlert,
    CollectorUsage,
    ValidationSummary,
    GovernedNotApplicable,
    OperatorJob,
    CredentialDelivery,
)

__all__ = [
    'Base',
    'get_session',
    'create_all_tables',
    'User',
    'DataRefreshJob',
    'AthleteCatalog',
    'AthleteCatalogFreshness',
    'EventCatalogEntry',
    'EventCatalogRefresh',
    'StatsRefresh',
    'ProviderAthleteMapping',
    'AthleteMappingDecision',
    'AthleteMappingDecisionCandidate',
    'AthleteMappingDecisionContradiction',
    'AthleteMappingLock',
    'AthleteMappingRejection',
    'ProviderEventMapping',
    'EventMappingDecision',
    'EventMappingDecisionCandidate',
    'EventMappingDecisionContradiction',
    'EventMappingLock',
    'EventMappingRejection',
    'PlayerPoolSnapshot',
    'PlayerGameLog',
    'PlayerGameLogRefresh',
    'PlayerGameLogSync',
    'TeamMatchupFactRow',
    'TeamMatchupSurfaceObservationRow',
    'InjurySnapshot',
    'InjurySourceSnapshot',
    'ActiveSeason',
    'BootstrapRequest',
    'CatalogPublication',
    'CollectionManifest',
    'CollectorIdentity', 'CollectorStatusTransition',
    'CollectionObservation',
    'PublicationStream',
    'PublicationVersion',
    'PublicationObservation',
    'PublicationPointer',
    'CompositionJob',
    'CollectorTokenReplay',
    'CollectorLease',
    'CollectionCycle',
    'AuditEvent',
    'ReconciliationItem',
    'CollectionAlert',
    'CollectorUsage',
    'ValidationSummary',
    'GovernedNotApplicable',
    'OperatorJob',
    'CredentialDelivery',
]
