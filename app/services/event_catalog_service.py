"""Application service for refreshing and reading canonical NBA events."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from requests import exceptions as request_errors
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.freshness import (
    exact_seconds,
    exact_timedelta,
    time_window_timedelta,
)
from app.domain.utc import assume_utc
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
            getattr(self.settings, "catalog", None), "event_max_age_hours", Decimal(72)
        )
        # A configured setting, a direct hours override, and a direct timedelta
        # all become the TTL through the one time-window authority, so the
        # duration this catalog gates on and reports is a duration every other
        # seam can read as a window too -- and an absurd override is one typed
        # domain error rather than an OverflowError from timedelta.
        field = "EVENT_CATALOG_MAX_AGE_HOURS"
        if max_age_hours is not None:
            self.max_age = time_window_timedelta(
                max_age_hours, unit_seconds=3600, field=field
            )
        elif isinstance(max_age, timedelta):
            self.max_age = exact_timedelta(exact_seconds(max_age), field=field)
        elif max_age is not None:
            self.max_age = time_window_timedelta(
                max_age, unit_seconds=3600, field=field
            )
        else:
            self.max_age = time_window_timedelta(
                configured, unit_seconds=3600, field=field
            )

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
        refreshed_at = assume_utc(now or self._clock())
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

    def count_events(self, season: str) -> int:
        return self.repository.count_events(validate_canonical_season(season))

    def get_events_between(
        self, season: str, starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        return self.repository.list_events_between(
            validate_canonical_season(season),
            assume_utc(starts_at),
            assume_utc(ends_at),
        )

    def get_freshness(self, season: str, *, now: datetime | None = None) -> dict[str, Any]:
        return self.repository.freshness(validate_canonical_season(season), assume_utc(now or self._clock()), self.max_age)

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
