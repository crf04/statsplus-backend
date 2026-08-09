"""Application service for refreshing and reading canonical NBA events."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from requests import exceptions as request_errors
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import ProviderUnavailableError
from app.providers.nba_stats import NBAStatsProvider
from app.services.event_catalog_repository import EventCatalogRepository
from app.services.nba_stats_adapter import validate_canonical_season
from app.utils.db import is_demo_database_url
from app.utils.telemetry import ProviderResponseError

DEFAULT_EVENT_CATALOG_MAX_AGE = timedelta(hours=72)


@dataclass(frozen=True, slots=True)
class EventCatalogRefreshResult:
    season: str
    event_count: int
    refreshed_at: str


@dataclass(frozen=True, slots=True)
class EventCatalogBatchResult:
    """Independent outcomes for an explicit multi-season refresh."""

    results: tuple[EventCatalogRefreshResult, ...]
    failures: dict[str, str]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class EventCatalogService:
    """Coordinate the typed provider seam, normalization, and repository."""

    def __init__(self, engine: Engine, provider: NBAStatsProvider | None = None,
                 settings: RuntimeSettings | None = None, *,
                 nba_stats_provider: NBAStatsProvider | None = None,
                 clock: Callable[[], datetime] | None = None,
                 max_age: timedelta | float | None = None, max_age_hours: float | None = None) -> None:
        self.provider = provider or nba_stats_provider
        if self.provider is None:
            raise ValueError("an NBA whole-season schedule provider is required")
        self.settings = settings or get_runtime_settings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.repository = EventCatalogRepository(engine)
        if is_demo_database_url(str(engine.url)):
            raise ValueError("The canonical event catalog requires a writable application database.")
        configured = getattr(
            getattr(self.settings, "catalog", None), "event_max_age_hours", 72.0
        )
        self.max_age = (timedelta(hours=float(max_age_hours)) if max_age_hours is not None else
                        max_age if isinstance(max_age, timedelta) else
                        timedelta(hours=float(max_age)) if max_age is not None else
                        timedelta(hours=float(configured)))
        if self.max_age.total_seconds() <= 0:
            raise ValueError("event catalog max age must be greater than zero")

    def refresh(self, seasons: str | Iterable[str], *, now: datetime | None = None) -> EventCatalogRefreshResult | EventCatalogBatchResult:
        requested = self._canonical_seasons(seasons)
        if len(requested) == 1 and isinstance(seasons, str):
            return self._refresh_one(requested[0], now=now)
        results: list[EventCatalogRefreshResult] = []
        failures: dict[str, str] = {}
        for season in requested:
            try:
                results.append(self._refresh_one(season, now=now))
            except Exception as error:
                failures[season] = (
                    error.public_message if isinstance(error, ProviderUnavailableError)
                    else "The event catalog refresh could not complete."
                )
        return EventCatalogBatchResult(tuple(results), failures)

    def _refresh_one(self, canonical: str, *, now: datetime | None) -> EventCatalogRefreshResult:
        refreshed_at = _utc(now or self._clock())
        try:
            raw = self.provider.fetch_whole_season_schedule(season=canonical)
            frame = self._as_frame(raw)
            count = self.repository.publish(canonical, frame, refreshed_at)
            return EventCatalogRefreshResult(canonical, count, refreshed_at.isoformat())
        except (ProviderUnavailableError, ProviderResponseError, request_errors.RequestException) as error:
            self.repository.record_failure(canonical, refreshed_at)
            if isinstance(error, ProviderResponseError):
                raise ProviderUnavailableError(
                    "The NBA Stats provider returned an unsupported schedule.", detail=error
                ) from error
            raise
        except Exception:
            self.repository.record_failure(canonical, refreshed_at)
            raise

    @staticmethod
    def _canonical_seasons(seasons: str | Iterable[str]) -> tuple[str, ...]:
        if isinstance(seasons, str):
            values = [seasons]
        else:
            values = list(seasons)
        if not values:
            raise ValueError("at least one explicit canonical NBA season is required")
        return tuple(sorted({validate_canonical_season(value) for value in values}))

    def get_events(self, season: str) -> list[dict[str, Any]]:
        return self.repository.list_events(validate_canonical_season(season))

    def get_freshness(self, season: str, *, now: datetime | None = None) -> dict[str, Any]:
        return self.repository.freshness(validate_canonical_season(season), _utc(now or self._clock()), self.max_age)

    def is_fresh(self, season: str, *, now: datetime | None = None) -> bool:
        return bool(self.get_freshness(season, now=now)["fresh"])

    @staticmethod
    def _as_frame(value: Any) -> pd.DataFrame:
        if isinstance(value, pd.DataFrame):
            return value
        if isinstance(value, Mapping):
            return pd.DataFrame([value])
        if isinstance(value, (list, tuple)):
            return pd.DataFrame(value)
        raise ProviderUnavailableError("The NBA schedule provider returned invalid canonical data.")


__all__ = ["DEFAULT_EVENT_CATALOG_MAX_AGE", "EventCatalogBatchResult", "EventCatalogRefreshResult", "EventCatalogService"]
