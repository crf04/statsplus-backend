"""Compatibility import surface for the Statistic Catalog service seam."""

from app.services.statistic_catalog import (
    CanonicalStatistic,
    MatchState,
    StatisticCatalog,
    StatisticCatalogError,
    StatisticCatalogSchemaError,
    StatisticMapping,
    StatisticMatch,
    StatisticMatchState,
    StatisticResolution,
    StatisticResolver,
    StatisticUnit,
    SUPPORTED_STATISTIC_PROVIDERS,
    load_statistic_catalog,
)

__all__ = [
    "CanonicalStatistic",
    "MatchState",
    "StatisticCatalog",
    "StatisticCatalogError",
    "StatisticCatalogSchemaError",
    "StatisticMapping",
    "StatisticMatch",
    "StatisticMatchState",
    "StatisticResolution",
    "StatisticResolver",
    "StatisticUnit",
    "SUPPORTED_STATISTIC_PROVIDERS",
    "load_statistic_catalog",
]
